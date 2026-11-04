#!/usr/bin/env python3
"""
kvm_deploy_runners.py

Text-menu tool to seed a KVM template once (cloud image -> testrunnerapp and
its whole native toolchain baked straight into the guest OS, no container
runtime) and then stamp out N cheap linked clones from it, each with its own
RAM/vCPU and a perf-sampling flag on/off, instead of someone logging into
every guest by hand to install testrunnerapp.

testrunnerapp runs directly on the guest (a venv, launched from inside the
live xfce/xrdp desktop session -- see build_template_cloud_init), the same
way it would on a bare-metal/VM runner -- no Docker anywhere in this path.

Flow:
  1) pick a cloud image type and download it once (base image)
  2) build the staging template: boot the base image under cloud-init,
     apt-install the full xfce/xrdp/pipewire/build-toolchain package set,
     build xa/VICE/hatari/OpenCBM from source, NFS-mount the NAS's existing
     /testsrc and /testrunnerapp trees straight in (no git clone -- this tool
     deploys against a NAS that already has both checked out), set up the
     python venv and the Desktop/runme.sh launcher (testrunnerapp is started
     from inside the live xfce/xrdp session, not as a headless service -- it drives GUI
     emulator windows a dev needs to see over xrdp), then the guest shuts
     itself down -- the resulting qcow2 is the reusable template. This is
     the slow step (real compiles), which is exactly why it only happens
     once.
  3) deploy N linked clones: each clone is a qcow2 overlay backed by the
     template (cheap disk-wise -- the template is read-only after step 2 and
     clones only ever write their own small deltas), with its identity
     (hostname/machine-id/ssh host keys) reset via virt-sysprep instead of a
     second cloud-init pass, so clone boot is fast. Each clone gets its own
     RAM/vCPU and a disk-discard (overlay deleted on teardown) or disk-keep
     (overlay left for inspection) choice.

Networking is picked in step 2 (configure_settings): either libvirt's
'default' NAT network (DHCP, host-only reachability) or one of this host's
own bridge interfaces, passed straight to virt-install's --network. Plain
NAT is fine for template builds, but VMs that need to be reached from other
machines on the LAN (e.g. a nested-virt test server reaching in) need a real
host bridge -- NAT alone silently stranded them behind the host before this
was configurable. Note this is a different path than kvm_section.md's
nested-virt macvtap-into-NFS note (macvtap-into-NFS mounts inside a guest
hang on real I/O there): that issue was specifically about a macvtap network
device, and this tool's guest NFS mounts of /testsrc and /testrunnerapp ride
over whichever --network mode is picked above (NAT or a real host bridge),
never macvtap -- but if NFS-from-guest I/O ever looks wrong, that note is
the first place to check.

Run on the KVM host as a user in the libvirt group. Needs: qemu-kvm,
libvirt-daemon-system, virtinst, cloud-image-utils, genisoimage,
libguestfs-tools (virt-sysprep). virt-sysprep needs to read the host's
/boot/vmlinuz-* to build its appliance, which is root-only on Ubuntu, so
clone identity-reset runs under sudo -- expect a sudo password prompt when
deploying clones unless NOPASSWD sudo is configured.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE_FILE_NAME = "fleet_state.json"
DEFAULT_WORKDIR = Path.home() / "kvm-images"

IMAGE_PROFILES = {
    "ubuntu26": {
        "label": "Ubuntu 26.04 LTS (Resolute Raccoon) server cloud image",
        "url": "https://cloud-images.ubuntu.com/releases/26.04/release/"
               "ubuntu-26.04-server-cloudimg-amd64.img",
        # osinfo-db may not have a ubuntu26.04 entry, and which specific
        # point release IS recognized drifts by osinfo-db version/host (hit
        # "Unknown OS name 'ubuntu25.10'" on one box after it worked on
        # another) -- "generic" is always accepted and only affects
        # libvirt's default device tuning, not what actually boots
        # (confirmed in kvm_section.md).
        "os_variant": "generic",
    },
}

PERF_PACKAGES = ["linux-tools-common", "linux-tools-generic"]


# --------------------------------------------------------------------------
# small input helpers
# --------------------------------------------------------------------------

def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def ask_int(prompt, default=None):
    while True:
        raw = ask(prompt, str(default) if default is not None else None)
        try:
            return int(raw)
        except (TypeError, ValueError):
            print("  enter a whole number")


def ask_bool(prompt, default=True):
    d = "y" if default else "n"
    while True:
        raw = ask(f"{prompt} (y/n)", d).lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  enter y or n")


def ask_choice(prompt, choices, default=None):
    choice_str = "/".join(choices)
    d = default or choices[0]
    while True:
        raw = ask(f"{prompt} ({choice_str})", d)
        if raw in choices:
            return raw
        print(f"  pick one of: {choice_str}")


def pause():
    input("\n(press Enter to continue) ")


def ensure_qemu_traversable(path: Path):
    """virt-install's qemu process needs search (x) permission on every
    ancestor directory down to a clone's overlay file. $HOME defaults to
    0700/0750 on most distros, which blocks that even though the overlay
    directory itself is world-readable -- grant the minimal o+x bit on
    ancestors inside $HOME only, never touching anything above it."""
    home = Path.home().resolve()
    try:
        path.resolve().relative_to(home)
    except ValueError:
        return
    p = path.resolve()
    while True:
        try:
            mode = p.stat().st_mode
            if not (mode & 0o001):
                p.chmod(mode | 0o001)
        except FileNotFoundError:
            pass
        if p == home:
            break
        p = p.parent


def run(cmd, **kw):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def run_ok(cmd, **kw):
    try:
        run(cmd, **kw)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  !! command failed ({exc.returncode})")
        return False
    except FileNotFoundError as exc:
        print(f"  !! {exc} -- run '1) Host prerequisite check' first.")
        return False


def run_capture(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class Fleet:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / STATE_FILE_NAME
        self.data = self._load()

    def _load(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"settings": {}, "templates": {}, "vms": []}

    def save(self):
        self.state_path.write_text(json.dumps(self.data, indent=2))

    @property
    def settings(self):
        return self.data["settings"]

    @property
    def templates(self):
        return self.data["templates"]

    @property
    def vms(self):
        return self.data["vms"]

    def vm_by_name(self, name):
        for vm in self.vms:
            if vm["name"] == name:
                return vm
        return None


def default_settings():
    return {
        "guest_username": "user",
        "guest_password": "changeme",
        "ssh_pubkey_file": str(Path.home() / ".ssh/id_ed25519.pub"),
        # /testsrc and /testrunnerapp are NOT git clones -- this tool deploys
        # against a NAS that already has both trees on it; guests NFS-mount
        # them directly instead of pulling a fresh checkout.
        # IMPORTANT: this is the path as the NFS SERVER exports it (check
        # with `showmount -e <server>`), which is not necessarily the path
        # the host happens to locally mount that export at -- e.g. a host
        # that mounts the export at /mnt/bughabitat may find the server
        # itself only exports /bigpool/bughabitat; mounting /mnt/bughabitat
        # from a guest then fails with "access denied by server" because
        # that path was never exported at all.
        "nfs_server": "",
        "nfs_testsrc_export": "/bigpool/bughabitat/testsrc",
        "nfs_testrunnerapp_export": "/bigpool/bughabitat/testrunnerapp",
        "template_disk_size": "30G",
        "template_ram_mb": 4096,
        "template_vcpus": 4,
        # base/template images: big, essentially read-only after the one
        # build -- fine to live on the shared NFS export. Seed ISOs also
        # live here (not under $HOME): $HOME is usually 750 on Ubuntu, which
        # blocks the libvirt-qemu user from even traversing into it to read
        # a --disk cdrom path (see kvm_section.md's home-dir-permission
        # note) -- image_store_dir is world-traversable instead.
        "image_store_dir": "/mnt/bughabitat/testsrc/kvm_images",
        # clone overlays: small but take live guest I/O (testrunnerapp logs,
        # xrdp sessions) -- kept on local disk, not NFS, for speed and to
        # dodge qcow2-over-NFS locking quirks.
        "clone_store_dir": str(Path.home() / "kvm-images" / "clones"),
        "network_arg": "network=default",
    }


def detect_host_bridges():
    """Host bridge interfaces virt-install could attach VMs to directly,
    other than libvirt's own NAT bridge (virbr0, covered by the separate
    'network=default' NAT choice)."""
    out = run_capture(["ip", "-o", "link", "show", "type", "bridge"]).stdout
    names = []
    for line in out.splitlines():
        # e.g. "3: br0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ..."
        parts = line.split(":")
        if len(parts) < 2:
            continue
        name = parts[1].strip().split("@")[0]
        if name and name != "virbr0":
            names.append(name)
    return names


def pick_network_arg(current):
    bridges = detect_host_bridges()
    print("\nNetwork for VMs (passed to virt-install's --network):")
    print("  0) NAT -- libvirt 'default' network (host-only reachability)")
    for i, b in enumerate(bridges, 1):
        print(f"  {i}) host bridge: {b}  (VMs get real LAN addresses)")
    if not bridges:
        print("  (no other host bridges found -- only NAT is available)")
    default_idx = 0
    if current and current.startswith("bridge="):
        br = current.split("=", 1)[1]
        if br in bridges:
            default_idx = bridges.index(br) + 1
    idx = ask_int("Pick network", default_idx)
    if idx <= 0 or idx > len(bridges):
        return "network=default"
    return f"bridge={bridges[idx - 1]}"


# --------------------------------------------------------------------------
# step: host checks
# --------------------------------------------------------------------------

def host_checks():
    print("\n== Host prerequisite check ==")
    # check for the actual binaries this tool calls, not guessed package
    # names -- package names providing them vary across Ubuntu releases
    # (e.g. "qemu-kvm" is a no-op virtual package on some releases where
    # qemu-system-x86 already provides qemu-system-x86_64; virt-install's
    # package is literally called virt-install, not "virtinst").
    needed_bins = {
        "qemu-system-x86_64": "qemu-system-x86",
        "virsh": "libvirt-clients",
        "virt-install": "virt-install",
        "cloud-localds": "cloud-image-utils",
        "genisoimage": "genisoimage",
        "virt-sysprep": "guestfs-tools",
    }
    missing_bins = [b for b in needed_bins if not shutil.which(b)]
    if not Path("/etc/libvirt/libvirtd.conf").exists():
        missing_bins.append("__libvirtd__")
        needed_bins["__libvirtd__"] = "libvirt-daemon-system"

    if missing_bins:
        pkgs = sorted({needed_bins[b] for b in missing_bins})
        print("Missing tools:", ", ".join(missing_bins), "-> packages:", ", ".join(pkgs))
        if ask_bool("Install them now with apt (sudo)?", True):
            run_ok(["sudo", "apt-get", "update"])
            run_ok(["sudo", "apt-get", "install", "-y", *pkgs])
            still_missing = [b for b in needed_bins if b != "__libvirtd__" and not shutil.which(b)]
            if still_missing:
                print(f"!! still missing after install attempt: {', '.join(still_missing)} "
                      "-- package name may differ on this release, install by hand.")
    else:
        print("All required tools present.")

    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True).stdout.split()
    if "libvirt" not in groups and "kvm" not in groups:
        print("Current user is not in the libvirt/kvm group yet.")
        if ask_bool("Add current user to libvirt,kvm now (requires re-login after)?", True):
            run_ok(["sudo", "usermod", "-aG", "libvirt,kvm", os.environ.get("USER", "")])
            print("  -> log out/in (or newgrp) before deploying VMs.")
    else:
        print("User already in libvirt/kvm group.")

    if not Path("/dev/kvm").exists():
        print("!! /dev/kvm missing -- hardware virtualization is not available/exposed here.")
    else:
        print("/dev/kvm present.")

    vmx = subprocess.run(
        ["grep", "-cE", "(vmx|svm)", "/proc/cpuinfo"], capture_output=True, text=True
    ).stdout.strip()
    print(f"Hardware virtualization flags on this CPU: {vmx or '0'}")

    for p in (Path("/sys/module/kvm_intel/parameters/nested"),
              Path("/sys/module/kvm_amd/parameters/nested")):
        if p.exists():
            print(f"{p}: {p.read_text().strip()} (lets VMs on this host run nested VMs of "
                  "their own -- unrelated to whether this host itself is a VM; this tool "
                  "clones repos to local guest disk regardless, so it doesn't matter either way)")

    active = subprocess.run(
        ["systemctl", "is-active", "libvirtd"], capture_output=True, text=True
    ).stdout.strip()
    print(f"libvirtd: {active}")

    net = run_capture(["virsh", "net-list", "--all"])
    print(net.stdout)
    if "default" not in net.stdout or "active" not in net.stdout:
        print("!! libvirt's 'default' NAT network isn't active -- "
              "try: sudo virsh net-start default")
    pause()


# --------------------------------------------------------------------------
# step: fleet-wide settings
# --------------------------------------------------------------------------

def configure_settings(fleet: Fleet):
    s = fleet.settings
    if not s:
        s.update(default_settings())

    print("\n== Fleet-wide settings ==")
    s["guest_username"] = ask("Guest login username", s.get("guest_username", "user"))
    s["guest_password"] = ask("Guest login password (plaintext, rotate after first boot)",
                               s.get("guest_password", "changeme"))
    s["ssh_pubkey_file"] = ask("SSH public key file to authorize",
                                s.get("ssh_pubkey_file", str(Path.home() / ".ssh/id_ed25519.pub")))

    s["nfs_server"] = ask("NFS server for /testsrc and /testrunnerapp (hostname or IP)", s.get("nfs_server"))
    print("  (path as the SERVER exports it -- run `showmount -e <server>` if unsure; "
          "this is often NOT the same path the host locally mounts that export at)")
    s["nfs_testsrc_export"] = ask("NFS export path to mount at /testsrc",
                                   s.get("nfs_testsrc_export", "/bigpool/bughabitat/testsrc"))
    s["nfs_testrunnerapp_export"] = ask("NFS export path to mount at /testrunnerapp",
                                         s.get("nfs_testrunnerapp_export", "/bigpool/bughabitat/testrunnerapp"))

    s["template_disk_size"] = ask("Template disk size", s.get("template_disk_size", "30G"))
    s["template_ram_mb"] = ask_int(
        "RAM (MB) to use while building the template (VICE/hatari build from source -- give it room)",
        s.get("template_ram_mb", 4096))
    s["template_vcpus"] = ask_int("vCPUs to use while building the template", s.get("template_vcpus", 4))

    s["image_store_dir"] = ask("Directory for base/template images (big, read-mostly)",
                                s.get("image_store_dir"))
    s["clone_store_dir"] = ask("Directory for clone disk overlays (small, local disk recommended)",
                                s.get("clone_store_dir"))

    s["network_arg"] = pick_network_arg(s.get("network_arg"))

    Path(s["image_store_dir"]).mkdir(parents=True, exist_ok=True)
    Path(s["clone_store_dir"]).mkdir(parents=True, exist_ok=True)
    ensure_qemu_traversable(Path(s["clone_store_dir"]))

    fleet.save()
    print("Settings saved.")
    pause()


# --------------------------------------------------------------------------
# step: pick + fetch base cloud image
# --------------------------------------------------------------------------

def prepare_base_image(fleet: Fleet):
    s = fleet.settings
    if not s:
        print("Run 'Configure settings' first.")
        pause()
        return

    print("\n== Select cloud image type ==")
    keys = list(IMAGE_PROFILES.keys())
    for i, k in enumerate(keys, 1):
        print(f"  {i}) {k} -- {IMAGE_PROFILES[k]['label']}")
    idx = ask_int("Pick one", 1)
    key = keys[max(0, min(idx, len(keys)) - 1)]
    profile = IMAGE_PROFILES[key]

    img_path = Path(s["image_store_dir"]) / f"base-{key}.img"
    if img_path.exists():
        print(f"Base image already present: {img_path}")
        if not ask_bool("Re-download anyway?", False):
            fleet.templates.setdefault(key, {})["base_image"] = str(img_path)
            fleet.templates[key]["os_variant"] = profile["os_variant"]
            fleet.save()
            pause()
            return

    if not run_ok(["wget", "-O", str(img_path), profile["url"]]):
        print("!! download failed -- check the URL (release path may have moved).")
        pause()
        return

    fleet.templates.setdefault(key, {})["base_image"] = str(img_path)
    fleet.templates[key]["os_variant"] = profile["os_variant"]
    fleet.save()
    print(f"Saved base image to {img_path}")
    pause()


# --------------------------------------------------------------------------
# step: build the staging template (provision once, then shut down)
# --------------------------------------------------------------------------

# package list is the Dockerfile's apt-get install list, minus docker-layer
# plumbing (apt cache mounts, the final "remove light-locker" line stays
# below as its own runcmd). --no-install-recommends isn't available through
# cloud-init's packages: module -- harmless, just a bit more disk.
APP_PACKAGES = [
    "xfce4", "xfce4-clipman-plugin", "xfce4-cpugraph-plugin", "xfce4-netload-plugin",
    "xserver-xorg-legacy", "xdg-utils", "dbus-x11", "xfce4-screenshooter",
    "xfce4-taskmanager", "xfce4-terminal", "xfce4-xkb-plugin", "xorgxrdp", "xrdp",
    "xserver-xorg-video-qxl",
    # Without this, Xorg has no driver for ANY input device (keyboard or
    # mouse) -- confirmed via Xorg.0.log: "No input driver specified,
    # ignoring this device" for every /dev/input node. Keyboard input still
    # seemed to work (raw console passthrough), masking the bug until the
    # mouse was tested in the xfce session.
    "xserver-xorg-input-libinput",
    "xvfb", "openssh-server", "sudo", "wget", "curl", "bzip2", "python3", "python3-pip",
    "python3-venv", "build-essential", "software-properties-common",
    "xterm", "git", "vim", "libzbar0t64", "linux-headers-generic", "libusb-1.0-0-dev",
    "nfs-common",
    "pkg-config", "libncurses-dev", "cc65", "libsdl2-dev", "zlib1g-dev", "xdotool",
    "imagemagick", "pipewire", "pipewire-pulse", "wireplumber",
    "pipewire-audio-client-libraries", "dbus-user-session", "alsa-utils",
    "pulseaudio-utils", "autoconf", "automake", "libtool", "libpipewire-0.3-dev",
    # 86Box binaries dropped into build_store on these guests are built with
    # OpenAL sound support and dynamically link libopenal.so.1 -- without
    # the runtime lib installed on the guest itself, 86Box fails before its
    # window even appears ("error while loading shared libraries:
    # libopenal.so.1: cannot open shared object file"). This bit us once in
    # the 86box docker image for the same underlying reason (AppImage
    # excludes libs assumed to be present on the host) -- here there's no
    # AppImage/vendoring step at all, so the guest just needs the package.
    "libopenal1",
    "libspa-0.2-dev", "cmake", "texinfo", "libgtk-3-dev", "libglew-dev",
    "libevdev-dev", "libpulse-dev", "libasound2-dev", "libcurl4-openssl-dev",
    "libreadline-dev", "libxaw7-dev", "flex", "bison", "libpng-dev",
    "libxrandr-dev", "libxinerama-dev", "libxi-dev", "tar", "dos2unix", "file",
    *PERF_PACKAGES,
]


def _yaml_block(script_text: str) -> str:
    """One runcmd list entry holding a whole multi-line shell script, so
    state (cd, env vars, set -e) carries across lines like a real script."""
    indented = "\n".join(f"    {line}" for line in script_text.strip("\n").splitlines())
    return f"  - |\n{indented}"


def build_template_cloud_init(fleet: Fleet, seed_dir: Path, vm_name: str):
    s = fleet.settings
    pubkey = Path(s["ssh_pubkey_file"]).expanduser().read_text().strip()
    user = s["guest_username"]

    (seed_dir / "meta-data").write_text(f"instance-id: {vm_name}\nlocal-hostname: {vm_name}\n")

    blocks = []

    # cloud-init's runcmd module concatenates every block below into ONE
    # bash script (confirmed: /var/lib/cloud/instance/scripts/runcmd holds
    # the whole thing) -- `set -e` therefore applies across ALL of them, not
    # just the block it's written in. Without this trap, a failure in any
    # earlier block (e.g. an NFS mount denied by the server) aborts the
    # script right there and the guest just sits "running" forever, since
    # the shutdown line at the very end is never reached -- wait_for_shutoff
    # then times out instead of reporting a failed build. The EXIT trap
    # guarantees a shutdown either way, and records which, so build_template
    # can tell a finished template from a half-built one before trusting it.
    blocks.append(_yaml_block("""
trap 'echo "exit:$?" > /var/lib/cloud-init-build-status; shutdown -h now' EXIT
set -e
echo 'kernel.perf_event_paranoid=-1' > /etc/sysctl.d/99-perf.conf
echo 'kernel.kptr_restrict=0' >> /etc/sysctl.d/99-perf.conf
sysctl --system
apt-get remove -y light-locker xscreensaver || true
"""))

    blocks.append(_yaml_block("""
set -e
add-apt-repository -y ppa:vriviere/mintelf
apt-get update
apt-get install -y --no-install-recommends cross-mintelf-essential
"""))

    blocks.append(_yaml_block("""
set -e
wget -O /tmp/firefox.tar.xz "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US" --no-check-certificate
tar xJf /tmp/firefox.tar.xz -C /opt
ln -sf /opt/firefox/firefox /usr/local/bin/firefox
rm -f /tmp/firefox.tar.xz
"""))

    blocks.append(_yaml_block(f"""
set -e
mkdir -p /var/run/dbus
sed -i "s/console/anybody/g" /etc/X11/Xwrapper.config
sed -i 's/rdpsnd=false/rdpsnd=true/' /etc/xrdp/xrdp.ini
# cloud-init's users: module already created {user}'s homedir from
# /etc/skel BEFORE this runcmd script runs, so appending to
# /etc/skel/.Xsession here never reaches the actual login user -- write
# it directly into their homedir instead.
echo "xfce4-session" >> /etc/skel/.Xsession
echo "xfce4-session" >> /home/{user}/.Xsession
chown {user}:{user} /home/{user}/.Xsession
cat <<'GLEOF' > /etc/profile.d/99-xrdp-software-gl.sh
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
GLEOF
chmod 0644 /etc/profile.d/99-xrdp-software-gl.sh
# Autoboot straight into xfce4 on the guest's REAL console (visible over
# SPICE/qxl in virt-viewer, and what a plain `startx` would also use) --
# lightdm with autologin, left on the system's DEFAULT /etc/X11/xorg.conf
# (auto-detected qxl/modesetting driver).
#
# An earlier version of this script copied xrdp's own driver-specific
# config (/etc/X11/xrdp/xorg.conf, an RDP-only virtual-framebuffer driver
# with no real VT/console binding) over that system default, repointed
# sesman.ini's XorgCommand at it too (stripping the "xrdp/" prefix off the
# "xrdp/xorg.conf" param that normally keeps it scoped to sessions sesman
# itself spawns), and ran a hand-rolled "shared Xorg :10" service in place
# of the packaged display manager. Every local X session -- lightdm's
# console session and plain `startx` alike -- ended up loading that
# RDP-only driver, which never shows anything on a real VT: console GUI
# never appeared after boot, and `startx` failed with "Couldn't get a file
# descriptor referring to the console" (nothing to bind -- confirmed on
# hmm309). Leaving /etc/X11/xorg.conf and sesman.ini at their packaged
# defaults keeps the two sessions on separate configs the way xrdp expects:
# lightdm autologs into a normal VT-based xfce4 session with the real
# driver (what shows up locally/over SPICE and what `startx` would start
# too), while xrdp-sesman spawns its own session per RDP client using its
# own private xorg.conf -- no more clash, no custom service needed.
mkdir -p /etc/lightdm/lightdm.conf.d
cat <<LIGHTDMEOF > /etc/lightdm/lightdm.conf.d/50-autologin.conf
[Seat:*]
autologin-user={user}
autologin-user-timeout=0
autologin-session=xfce
LIGHTDMEOF
systemctl enable lightdm
# lightdm's console autologin and an xrdp login both run xfce4-session as
# the SAME {user}, and PAM hands both the identical systemd-managed per-UID
# $XDG_RUNTIME_DIR/bus -- so they fight over the single org.xfce.SessionManager
# D-Bus name, and whichever starts second (the RDP one, always) loses and
# exits silently within ~1s. Confirmed via /var/log/xrdp-sesman.log: "Window
# manager exited quickly (1 secs)" -- the symptom is an RDP login that's
# accepted and then immediately disconnects. Give the RDP session its own
# private runtime dir/bus, keyed by display number, before it reaches
# /etc/X11/Xsession.d/75dbus_dbus-launch (which only starts a fresh bus when
# it doesn't already see DBUS_SESSION_BUS_ADDRESS set).
cat <<'STARTWMEOF' > /etc/xrdp/startwm.sh
#!/bin/sh
# xrdp X session start script (c) 2015, 2017, 2021 mirabilos
# published under The MirOS Licence

if [ -n "$DISPLAY" ]; then
	RDP_RUNTIME_DIR="/run/user/$(id -u)-rdp${{DISPLAY}}"
	mkdir -p "$RDP_RUNTIME_DIR"
	chmod 700 "$RDP_RUNTIME_DIR"
	export XDG_RUNTIME_DIR="$RDP_RUNTIME_DIR"
	unset DBUS_SESSION_BUS_ADDRESS
fi

# Rely on /etc/pam.d/xrdp-sesman using pam_env to load both
# /etc/environment and /etc/default/locale to initialise the
# locale and the user environment properly.

if test -r /etc/profile; then
	. /etc/profile
fi

if test -r ~/.profile; then
	. ~/.profile
fi

test -x /etc/X11/Xsession && exec /etc/X11/Xsession
exec /bin/sh /etc/X11/Xsession
STARTWMEOF
chmod 755 /etc/xrdp/startwm.sh
"""))

    blocks.append(_yaml_block("""
set -e
cd /tmp
wget -O xa-2.4.1.tar.gz https://www.floodgap.com/retrotech/xa/dists/xa-2.4.1.tar.gz
tar -xzf xa-2.4.1.tar.gz -C /tmp
cd /tmp/xa-2.4.1
make
cp xa /usr/local/bin
cd /
rm -rf /tmp/xa-2.4.1 /tmp/xa-2.4.1.tar.gz
"""))

    # VICE, Hatari, OpenCBM and the pipewire-module build (further below)
    # are independent of each other (separate install prefixes, no shared
    # state), so they run as background jobs instead of one-after-another
    # -- splitting this VM's vcpus between them keeps cores busy during
    # each build's non-parallel stretches (download/extract/configure/link)
    # instead of leaving most of a big, deliberately-oversized build VM
    # idle. set -e in this script does NOT apply inside a backgrounded
    # subshell, so each job's exit status is captured explicitly via
    # `wait "$pid"` below -- a failed job must still fail this whole step,
    # the same as it would have as its own sequential block.
    blocks.append(_yaml_block("""
set -e
CORES=$(nproc)
NUM_JOBS=4
JOB_CORES=$(( CORES / NUM_JOBS )); [ "$JOB_CORES" -lt 1 ] && JOB_CORES=1
mkdir -p /var/log/build-parallel

(
set -e
export CFLAGS="-O3" CXXFLAGS="-O3" LDFLAGS=""
VICE_URL=$(python3 -c "import urllib.request, re; html = urllib.request.urlopen('https://vice-emu.sourceforge.io/index.html').read().decode('utf-8'); print(re.search(r'href=\\"(https://sourceforge.net/projects/vice-emu/files/releases/vice-[0-9.]+\\\\.tar\\\\.gz/download)\\"', html).group(1))")
curl -L -o /tmp/vice.tar.gz "$VICE_URL"
tar -xzf /tmp/vice.tar.gz -C /tmp
cd /tmp/vice-*
./configure --prefix=/usr --infodir=/usr/share/info --mandir=/usr/share/man \\
    --enable-html-docs --enable-native-gtk3ui --enable-x64 --disable-parsid \\
    --enable-hardsid --with-resid --enable-arch=no --enable-x64-image --with-png \\
    --with-jpeg --enable-lame --disable-ffmpeg --enable-rs232 --enable-ipv6 --with-oss
make -j"$JOB_CORES"
make install
mkdir -p /usr/share/vice
cp -a data/* /usr/share/vice/
rm -rf /tmp/vice*
) > /var/log/build-parallel/vice.log 2>&1 &
pid_vice=$!

(
set -e
HATARI_TAG=$(python3 -c "import urllib.request, json; print(json.load(urllib.request.urlopen('https://framagit.org/api/v4/projects/hatari%2Fhatari/repository/tags?per_page=1'))[0]['name'])")
curl -L -o /tmp/hatari.tar.bz2 "https://framagit.org/hatari/hatari/-/archive/${HATARI_TAG}/hatari-${HATARI_TAG}.tar.bz2"
mkdir -p /tmp/hatari
tar -xjf /tmp/hatari.tar.bz2 -C /tmp/hatari --strip-components=1
cd /tmp/hatari
./configure --prefix=/usr
make -j"$JOB_CORES"
make install
cd /
rm -rf /tmp/hatari /tmp/hatari.tar.bz2
) > /var/log/build-parallel/hatari.log 2>&1 &
pid_hatari=$!

(
set -e
cd /root
git clone https://github.com/OpenCBM/OpenCBM.git
cd OpenCBM
make -f LINUX/Makefile USB_LIBS="$(pkg-config --libs libusb-1.0)" USB_CFLAGS="$(pkg-config --cflags libusb-1.0)" opencbm
make -f LINUX/Makefile install
make -f LINUX/Makefile ldconfig
) > /var/log/build-parallel/opencbm.log 2>&1 &
pid_opencbm=$!

(
set -e
git clone https://github.com/neutrinolabs/pipewire-module-xrdp.git /tmp/pipewire-module
cd /tmp/pipewire-module
./bootstrap
./configure --with-module-dir=/usr/lib/x86_64-linux-gnu/pipewire-0.3
make -j"$JOB_CORES"
make install
ldconfig
cd /
rm -rf /tmp/pipewire-module
) > /var/log/build-parallel/pipewire.log 2>&1 &
pid_pipewire=$!

fail=0
for pair in "vice:$pid_vice" "hatari:$pid_hatari" "opencbm:$pid_opencbm" "pipewire:$pid_pipewire"; do
    name=${pair%%:*}; pid=${pair##*:}
    if ! wait "$pid"; then
        echo "!! parallel build job '$name' failed -- see /var/log/build-parallel/$name.log" >&2
        fail=1
    fi
done
[ "$fail" -eq 0 ] || exit 1
"""))

    # /testsrc and /testrunnerapp are not git clones -- this guest NFS-mounts
    # the same NAS trees the host already has, so every clone and the host
    # see one shared, live source tree (no stale checkouts to keep in sync).
    # x-systemd.automount + nofail: a plain "defaults,_netdev" mount races
    # network-online.target at boot -- cloud-init's rendered netplan config
    # for the NIC has no "optional: false", so netplan's generator never
    # enables systemd-networkd-wait-online for it (confirmed via `systemctl
    # status systemd-networkd-wait-online.service`: "start condition unmet,
    # ConditionPathIsSymbolicLink ... was not met"). network-online.target
    # therefore fires before DHCP actually finishes, and the eager mount
    # fails with "mount.nfs: Network is unreachable" -- the guest boots fine
    # (xfce/xrdp come up) but /testsrc and /testrunnerapp are just
    # empty, which looks like the template "isn't booting". Automount defers
    # the real mount to first access (well after boot, once a user actually
    # opens a shell/runme.sh), so it never races the network coming up.
    blocks.append(_yaml_block(f"""
set -e
mkdir -p /testsrc /testrunnerapp
echo "{s['nfs_server']}:{s['nfs_testsrc_export']} /testsrc nfs _netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=60,x-systemd.mount-timeout=30 0 0" >> /etc/fstab
echo "{s['nfs_server']}:{s['nfs_testrunnerapp_export']} /testrunnerapp nfs _netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=60,x-systemd.mount-timeout=30 0 0" >> /etc/fstab
systemctl daemon-reload
mount /testsrc
mount /testrunnerapp
"""))

    blocks.append(_yaml_block(f"""
set -e
python3 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install -r /testsrc/requirements.txt
chgrp -R users /opt/venv
chmod -R g+rwX /opt/venv
find /opt/venv -type d -exec chmod g+s {{}} \\;
"""))

    # NOT a supervisor-managed headless service: testrunnerapp drives GUI
    # emulator windows (86Box/VICE/Hatari/etc.) that a dev needs to see and
    # click into over xrdp to troubleshoot a paused test. A systemd/
    # supervisor unit has no DISPLAY/XAUTHORITY -- anything it launched
    # would render nowhere a dev could reach. So the app is launched only
    # from inside the live xfce desktop session (Desktop/runme.sh, held
    # open in an xterm) -- that part is intentional, not a leftover to "fix"
    # into a background service.
    #
    # It no longer needs to wait on a human to actually log in, though:
    # lightdm now autologs {user} into that xfce session on boot (see the
    # autoboot fix above), so the DISPLAY/XAUTHORITY this needs already
    # exists the moment the guest comes up. An XFCE autostart entry (same
    # mechanism the docker image's entrypoint.sh used for its own RDP-primed
    # session) launches runme.sh the moment that session is created, same as
    # the docker containers do -- no per-host manual double-click needed. It
    # still runs in a held-open xterm, so a dev can close that window (or
    # just kill the python process inside it) to stop it same as before.
    blocks.append(_yaml_block("""
set -e
mkdir -p /var/log
touch /var/log/flaskapp.out.log /var/log/flaskapp.err.log
"""))

    # convenience desktop scripts, same content/paths as the former
    # entrypoint.sh wrote inside the container, minus the per-boot dynamic
    # useradd dance (cloud-init's users: module already made this user).
    blocks.append(_yaml_block(f"""
set -e
mkdir -p /home/{user}/Desktop /app
cat <<'RUNEOF' > /home/{user}/Desktop/runme.sh
#!/bin/bash
xfce4-terminal --hold --command="bash -c 'source /opt/venv/bin/activate && python3 /testrunnerapp/app.py'"
RUNEOF
cat <<'SOUNDEOF' > /home/{user}/Desktop/sound_en.sh
#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1001
export PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native
( /app/startaudio.sh >$HOME/pipewire_output.log 2>&1 & ) && disown
xfce4-terminal --hold --command="tail -f $HOME/pipewire_output.log"
SOUNDEOF
cat <<'LOGEOF' > /home/{user}/Desktop/soundlog.sh
#!/bin/bash
xfce4-terminal --hold --command="tail -f $HOME/pipewire_output.log"
LOGEOF
cat <<'AUDIOEOF' > /app/startaudio.sh
export XDG_RUNTIME_DIR=/run/user/1001
export PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native
mkdir -p $XDG_RUNTIME_DIR
chown $USER:$USER $XDG_RUNTIME_DIR
chmod 700 $XDG_RUNTIME_DIR
pkill -9 -f "pipewire|wireplumber|pw-cli" || true
rm -f /run/user/1001/pulse/native /run/user/1001/pipewire-0.lock
sleep 2
LOG_DIR="/home/{user}/pwxrdplogs"
mkdir -p "$LOG_DIR"
pipewire > "$LOG_DIR/pipewire.log" 2>&1 &
pipewire-pulse > "$LOG_DIR/pipewire-pulse.log" 2>&1 &
wireplumber > "$LOG_DIR/wireplumber.log" 2>&1 &
sleep 1
pw-cli -m load-module libpipewire-module-xrdp sink.stream.props={{node.name=xrdp-sink}} source.stream.props={{node.name=xrdp-source}} >/dev/null &
sleep 1
pactl set-default-sink xrdp-sink
pactl set-default-source xrdp-source
AUDIOEOF
chmod +x /home/{user}/Desktop/*.sh /app/startaudio.sh
mkdir -p /home/{user}/.config/autostart
cat <<'AUTOSTARTEOF' > /home/{user}/.config/autostart/testrunner.desktop
[Desktop Entry]
Type=Application
Name=Test Runner
Exec=/home/{user}/Desktop/runme.sh
X-GNOME-Autostart-enabled=true
AUTOSTARTEOF
# /testsrc and /testrunnerapp are the shared NFS-mounted NAS trees -- do NOT
# chown them here, that would recursively rewrite ownership on the NAS out
# from under every other clone (and the host) mounting the same export.
chown -R {user}:{user} /home/{user}/Desktop /home/{user}/.config
"""))

    # no explicit shutdown here -- the EXIT trap in the first block already
    # shuts the guest down once this script ends, on success or failure.

    package_lines = "\n".join(f"  - {p}" for p in APP_PACKAGES)
    runcmd_text = "\n".join(blocks)

    user_data = f"""#cloud-config
hostname: {vm_name}
users:
  - name: {user}
    groups: [sudo, users]
    shell: /bin/bash
    lock_passwd: false
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - {pubkey}

chpasswd:
  expire: false
  list: |
    {user}:{s['guest_password']}

ssh_pwauth: true

package_update: true
packages:
{package_lines}

runcmd:
{runcmd_text}
"""
    (seed_dir / "user-data").write_text(user_data)
    seed_img = seed_dir / f"seed-{vm_name}.img"
    run(["cloud-localds", str(seed_img), str(seed_dir / "user-data"), str(seed_dir / "meta-data")])
    return seed_img


def wait_for_shutoff(domain: str, timeout=2400, poll=10):
    print(f"Waiting for {domain} to shut itself down (provisioning runs inside)...")
    start = time.time()
    while time.time() - start < timeout:
        res = run_capture(["virsh", "domstate", domain])
        state = res.stdout.strip()
        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] domstate={state or res.stderr.strip()}")
        if state == "shut off":
            return True
        if res.returncode != 0:
            # domain already gone
            return True
        time.sleep(poll)
    return False


def validate_nfs_paths(s):
    """Build verification, step 0: before sinking 30-60 min into a build,
    confirm the SERVER actually exports these paths and they look like the
    real testsrc (populated pyhelpers/) and testrunnerapp (app.py) trees.

    Deliberately does NOT just stat() these paths on this host's own
    filesystem -- this host may happen to have an identically-named local
    directory mounted at some unrelated local mountpoint (that's exactly how
    the first version of this check passed while the guest-side mount still
    failed with "access denied by server": nfs_testsrc_export is the path as
    the SERVER exports it, not wherever this host locally mounts it). So this
    asks the server itself via showmount, then mounts read-only from there to
    check content, instead of trusting a same-named directory that happens to
    already exist locally.
    """
    problems = []
    server = s.get("nfs_server", "").strip()
    if not server:
        return ["nfs_server is not set."]

    res = run_capture(["showmount", "-e", server])
    if res.returncode != 0:
        return [f"couldn't query exports from {server} via showmount "
                f"({(res.stderr or res.stdout).strip()}). Is nfs-common installed "
                "here and the server reachable?"]

    exported = [line.split()[0] for line in res.stdout.splitlines()[1:] if line.split()]

    def covered(export_path):
        return any(export_path == e or export_path.startswith(e.rstrip("/") + "/") for e in exported)

    checks = [
        ("nfs_testsrc_export", "testsrc", "pyhelpers", "dir"),
        ("nfs_testrunnerapp_export", "testrunnerapp", "app.py", "file"),
    ]
    for key, label, marker_name, kind in checks:
        export_path = s[key]
        if not covered(export_path):
            problems.append(
                f"{label} export ({export_path}) is not under anything {server} actually exports "
                f"(showmount -e {server} lists: {', '.join(exported) or '(nothing)'}) -- this must be "
                "the path as the SERVER exports it, not wherever this host happens to locally mount it.")
            continue

        tmp_mount = Path(tempfile.mkdtemp(prefix=f"nfs-validate-{label}-"))
        mounted = run_ok(["sudo", "mount", "-t", "nfs", "-o", "ro", f"{server}:{export_path}", str(tmp_mount)])
        try:
            if not mounted:
                problems.append(f"{label} export ({export_path}) is listed by showmount but mounting it here failed.")
                continue
            marker = tmp_mount / marker_name
            if kind == "dir":
                if not marker.is_dir() or not any(marker.iterdir()):
                    problems.append(
                        f"{label} export ({export_path}) is missing a populated {marker_name}/ "
                        "-- doesn't look like the real testsrc tree (no cluster/node info).")
            elif not marker.is_file():
                problems.append(f"{label} export ({export_path}) is missing {marker_name}.")
        finally:
            run_capture(["sudo", "umount", str(tmp_mount)])
            tmp_mount.rmdir()

    return problems


def build_template(fleet: Fleet):
    s = fleet.settings
    print("\n== Build staging template ==")
    keys = [k for k, v in fleet.templates.items() if v.get("base_image")]
    if not keys:
        print("No base image downloaded yet -- run 'Select/prepare base image' first.")
        pause()
        return
    for i, k in enumerate(keys, 1):
        # "template_image" being set in fleet.json is just a pointer from the
        # last successful build -- a build that got interrupted or whose file
        # was later deleted/moved leaves this pointer behind. Trust the disk,
        # not the metadata.
        has_template = "template_image" in fleet.templates[k] and Path(fleet.templates[k]["template_image"]).exists()
        tag = " (template already built)" if has_template else ""
        print(f"  {i}) {k}{tag}")
    idx = ask_int("Build template for which image profile", 1)
    key = keys[max(0, min(idx, len(keys)) - 1)]
    tmpl = fleet.templates[key]

    if tmpl.get("template_image"):
        if not Path(tmpl["template_image"]).exists():
            print(f"!! fleet.json points at {tmpl['template_image']} for a prior build, "
                  "but that file no longer exists on disk -- building fresh.")
        elif not ask_bool(
            f"Template for {key} already exists at {tmpl['template_image']} -- rebuild?", False
        ):
            pause()
            return

    problems = validate_nfs_paths(s)
    if problems:
        print("\n!! NFS path validation failed -- refusing to start the build:")
        for p in problems:
            print(f"   - {p}")
        print("   Fix the nfs_* settings (menu option 2) or the NAS export, then retry.")
        pause()
        return

    build_name = f"template-build-{key}"
    run_capture(["virsh", "destroy", build_name])
    run_capture(["virsh", "undefine", build_name])

    # qemu-img create below truncates/overwrites template_path in place --
    # clear the fleet's "template built" pointer *before* that happens, so a
    # rebuild that fails partway (e.g. a missing tool) leaves fleet.json
    # honestly reporting "no template" instead of pointing at a disk file
    # that got blown away but never got finished.
    tmpl.pop("template_image", None)
    fleet.save()

    template_path = Path(s["image_store_dir"]) / f"template-{key}.qcow2"

    # seed ISO must NOT live under $HOME: $HOME is typically 750 on Ubuntu,
    # which blocks the libvirt-qemu user from even traversing into it to
    # open a --disk cdrom path (hit this exact failure testing on
    # dockerbuilder -- matches kvm_section.md's home-dir-permission note).
    # image_store_dir is on the shared, world-traversable NFS export.
    seed_dir = Path(s["image_store_dir"]) / f"seed-{build_name}"
    seed_dir.mkdir(parents=True, exist_ok=True)

    try:
        run([
            "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
            "-b", tmpl["base_image"], str(template_path), s["template_disk_size"],
        ])

        seed_img = build_template_cloud_init(fleet, seed_dir, build_name)

        run([
            "virt-install",
            "--name", build_name,
            "--memory", str(s["template_ram_mb"]),
            "--vcpus", str(s["template_vcpus"]),
            # without this the clone-deploy path's --cpu host[-passthrough]
            # (see deploy_clones) has no counterpart here, and the guest
            # gets libvirt's conservative default CPU model/topology instead
            # of one that actually matches the vcpu count asked for above.
            "--cpu", "host-passthrough",
            "--import",
            "--disk", f"path={template_path},format=qcow2",
            "--disk", f"path={seed_img},device=cdrom",
            "--os-variant", tmpl["os_variant"],
            "--network", s.get("network_arg", "network=default"),
            # real video device + spice, so a user can actually open the
            # guest's own display (virt-viewer/remote-viewer) and log in --
            # not just xrdp into the xfce session from inside the guest OS.
            "--video", "qxl",
            "--graphics", "spice,listen=0.0.0.0",
            "--noautoconsole",
        ])

        ok = wait_for_shutoff(build_name)
        if not ok:
            print("!! timed out waiting for the template build to finish/shut down.")
            print("   It may still be working -- check 'virsh console "
                  f"{build_name}' or 'virsh list --all', then re-run this step")
            print("   once it reaches 'shut off' (don't undefine it out from under yourself).")
            pause()
            return

        # "shut off" alone doesn't mean the build finished -- the guest's
        # EXIT trap (see build_template_cloud_init) shuts down on failure
        # too, so it can shut off early mid-build. Read the status marker it
        # wrote right before shutting down, without booting the disk, to
        # tell the two apart before trusting this template.
        # needs root + LIBGUESTFS_BACKEND=direct, same as virt_sysprep_reset below --
        # supermin's appliance has to read the host's /boot/vmlinuz-* (0600
        # root:root on Ubuntu); without both of these it fails with
        # "supermin exited with error status 1" before it ever touches the disk.
        status = run_capture([
            "sudo", "env", "LIBGUESTFS_BACKEND=direct",
            "virt-cat", "-a", str(template_path), "/var/lib/cloud-init-build-status",
        ])
        if status.returncode != 0 or "exit:0" not in status.stdout:
            print("!! template build shut down but did NOT finish successfully:")
            print(f"   status marker: {status.stdout.strip() or '(unreadable -- ' + status.stderr.strip() + ')'}")
            print("   Not marking this template as built. Inspect the disk (e.g. "
                  f"'sudo env LIBGUESTFS_BACKEND=direct virt-cat -a {template_path} /var/log/cloud-init-output.log') for the real error, fix it, and rebuild.")
            pause()
            return

        # keep the disk, drop the transient build domain + its cdrom seed
        run_ok(["virsh", "undefine", build_name])
        for f in seed_dir.iterdir():
            f.unlink()
        seed_dir.rmdir()

        tmpl["template_image"] = str(template_path)
        fleet.save()
        print(f"Template ready: {template_path}")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"!! template build failed: {exc}")
        if isinstance(exc, FileNotFoundError):
            print("   a required tool is missing -- run '1) Host prerequisite check' first.")
        print(f"   leftover domain/seed (if any): {build_name}, {seed_dir}")
    pause()


# --------------------------------------------------------------------------
# step: deploy N linked clones
# --------------------------------------------------------------------------

def virt_sysprep_reset(overlay_path: Path, hostname: str) -> bool:
    # needs root: virt-sysprep's supermin appliance has to read the host's
    # /boot/vmlinuz-*, which is 0600 root:root on Ubuntu. If NOPASSWD sudo
    # isn't set up, this is where the user gets prompted for their password.
    cmd = [
        "sudo", "env", "LIBGUESTFS_BACKEND=direct",
        "virt-sysprep", "-a", str(overlay_path), "--hostname", hostname,
    ]
    if not run_ok(cmd):
        return False
    # virt-sysprep's default ops delete /etc/ssh/ssh_host_* expecting a fresh
    # boot to regenerate them -- but clones boot via --import with no
    # cloud-init datasource attached, so nothing regenerates them and sshd
    # refuses to start ("no hostkeys available"). Regenerate them offline here
    # instead, so clone boot stays fast with no second cloud-init pass.
    return run_ok([
        "sudo", "env", "LIBGUESTFS_BACKEND=direct",
        "virt-customize", "-a", str(overlay_path), "--run-command", "ssh-keygen -A",
    ])


def domifaddr(name: str, retries=10, delay=2):
    for _ in range(retries):
        res = run_capture(["virsh", "domifaddr", name])
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[-1]:
                return parts[-1].split("/")[0]
        time.sleep(delay)
    return None


def deploy_clones(fleet: Fleet):
    s = fleet.settings
    keys = [k for k, v in fleet.templates.items() if v.get("template_image")]
    if not keys:
        print("No template built yet -- run 'Build staging template' first.")
        pause()
        return

    print("\n== Deploy linked clone(s) ==")
    for i, k in enumerate(keys, 1):
        print(f"  {i}) {k}")
    idx = ask_int("Clone from which template", 1)
    key = keys[max(0, min(idx, len(keys)) - 1)]
    tmpl = fleet.templates[key]

    count = ask_int("How many clones to deploy", 1)
    name_prefix = ask("VM name prefix", f"{key}-run")
    start_index = sum(1 for vm in fleet.vms if vm["template_key"] == key) + 1

    uniform = ask_bool("Use the same RAM/vCPU/perf/disk-mode for all of them?", count > 1)
    if uniform:
        ram_mb = ask_int("RAM per clone (MB)", 4096)
        vcpus = ask_int("vCPUs per clone", 2)
        perf = ask_bool("Enable perf sampling flags (host-passthrough CPU) on these clones?", False)
        disk_mode = ask_choice("Disk mode", ["discard", "keep"], "discard")

    new_vms = []
    for i in range(count):
        idx_n = start_index + i
        name = f"{name_prefix}{idx_n:02d}"
        if not uniform:
            print(f"\n-- clone {i + 1}/{count}: {name} --")
            ram_mb_i = ask_int("  RAM (MB)", 4096)
            vcpus_i = ask_int("  vCPUs", 2)
            perf_i = ask_bool("  Enable perf sampling flags (host-passthrough CPU)?", False)
            disk_mode_i = ask_choice("  Disk mode", ["discard", "keep"], "discard")
        else:
            ram_mb_i, vcpus_i, perf_i, disk_mode_i = ram_mb, vcpus, perf, disk_mode

        new_vms.append({
            "name": name,
            "template_key": key,
            "ram_mb": ram_mb_i,
            "vcpus": vcpus_i,
            "perf_enabled": perf_i,
            "disk_mode": disk_mode_i,
            "status": "pending",
        })

    print("\nAbout to deploy:")
    for vm in new_vms:
        perf_tag = "perf=ON (host-passthrough)" if vm["perf_enabled"] else "perf=off"
        print(f"  {vm['name']:<16} ram={vm['ram_mb']}MB vcpus={vm['vcpus']} "
              f"{perf_tag} disk={vm['disk_mode']}")
    if not ask_bool("Proceed?", True):
        print("Cancelled.")
        pause()
        return

    ensure_qemu_traversable(Path(s["clone_store_dir"]))
    for vm in new_vms:
        print(f"\n--- building {vm['name']} ---")
        try:
            overlay_path = Path(s["clone_store_dir"]) / f"{vm['name']}.qcow2"
            run([
                "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                "-b", tmpl["template_image"], str(overlay_path),
            ])
            if not virt_sysprep_reset(overlay_path, vm["name"]):
                raise RuntimeError("virt-sysprep failed")

            cmd = [
                "virt-install",
                "--name", vm["name"],
                "--memory", str(vm["ram_mb"]),
                "--vcpus", str(vm["vcpus"]),
                "--import",
                "--disk", f"path={overlay_path},format=qcow2",
                "--os-variant", tmpl["os_variant"],
                "--network", s.get("network_arg", "network=default"),
                # real video device + spice -- see the matching comment in
                # build_template; every deployed clone needs its own
                # connectable display, same as the template.
                "--video", "qxl",
                "--graphics", "spice,listen=0.0.0.0",
                "--noautoconsole",
            ]
            if vm["perf_enabled"]:
                cmd += ["--cpu", "host-passthrough"]
            else:
                cmd += ["--cpu", "host"]
            run(cmd)

            vm["overlay_path"] = str(overlay_path)
            vm["status"] = "launched"
            fleet.vms.append(vm)
            fleet.save()

            ip = domifaddr(vm["name"])
            if ip:
                vm["ip"] = ip
                fleet.save()
                print(f"{vm['name']} launched, DHCP IP {ip}")
            else:
                print(f"{vm['name']} launched, DHCP lease not visible yet "
                      "(check 'virsh domifaddr' shortly)")
        except (subprocess.CalledProcessError, RuntimeError, FileNotFoundError) as exc:
            print(f"!! {vm['name']} failed: {exc}")
            if isinstance(exc, FileNotFoundError):
                print("   a required tool is missing -- run '1) Host prerequisite check' first.")
            vm["status"] = "failed"
            fleet.vms.append(vm)
            fleet.save()

    pause()


# --------------------------------------------------------------------------
# list / destroy
# --------------------------------------------------------------------------

def virsh_domain_names():
    """Domain names virsh currently knows about (any state), or None if
    virsh itself couldn't be queried (don't prune blindly in that case)."""
    res = run_capture(["virsh", "list", "--all", "--name"])
    if res.returncode != 0:
        return None
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def prune_ghost_vms(fleet: Fleet):
    """Drop fleet-state entries for launched clones virsh no longer knows
    about (destroyed/undefined outside this tool), keeping fleet state
    in sync with what 'virsh list --all' actually reports."""
    known = virsh_domain_names()
    if known is None:
        return
    ghosts = [vm for vm in fleet.vms
              if vm.get("status") == "launched" and vm["name"] not in known]
    if not ghosts:
        return
    for vm in ghosts:
        print(f"  (pruning ghost entry: {vm['name']} -- no longer defined in virsh)")
        fleet.vms.remove(vm)
    fleet.save()


def list_instances(fleet: Fleet):
    prune_ghost_vms(fleet)
    print("\n== Deployed clones (fleet state) ==")
    if not fleet.vms:
        print("(none yet)")
    for vm in fleet.vms:
        perf_tag = "perf=ON" if vm.get("perf_enabled") else "perf=off"
        print(f"  {vm['name']:<16} ip={vm.get('ip','?'):<15} "
              f"ram={vm.get('ram_mb','?')}MB vcpus={vm.get('vcpus','?')} "
              f"{perf_tag} disk={vm.get('disk_mode','?'):<7} status={vm.get('status','?')}")
    print("\n== virsh view ==")
    run_ok(["virsh", "list", "--all"])
    pause()


def destroy_instance(fleet: Fleet):
    for vm in fleet.vms:
        print(f"  - {vm['name']} (disk={vm.get('disk_mode','?')})")
    name = ask("VM name to destroy (blank to cancel)")
    if not name:
        return
    vm = fleet.vm_by_name(name)
    if not vm:
        print("Not in fleet state (will still try virsh, defaulting to keep-disk).")
    if not ask_bool(f"Really destroy {name}?", False):
        return

    run_ok(["virsh", "destroy", name])
    disk_mode = vm.get("disk_mode", "keep") if vm else "keep"
    if disk_mode == "discard":
        run_ok(["virsh", "undefine", name, "--remove-all-storage"])
    else:
        run_ok(["virsh", "undefine", name])
        if vm and vm.get("overlay_path"):
            print(f"  kept overlay disk at {vm['overlay_path']}")

    if vm:
        fleet.vms.remove(vm)
        fleet.save()
    print(f"{name} torn down.")
    pause()


# --------------------------------------------------------------------------
# main menu
# --------------------------------------------------------------------------

MENU = """
==========================================
 KVM testrunner fleet deployer (template + linked clones)
 workdir: {workdir}
==========================================
 1) Host prerequisite check
 2) Configure fleet-wide settings
 3) Select / prepare base cloud image
 4) Build staging template (testrunnerapp + toolchain baked in, no docker)
 5) Deploy N linked clones (RAM/vCPU/perf/disk-mode per clone)
 6) List deployed clones
 7) Destroy a clone
 8) Exit
"""


def main():
    workdir = Path(os.environ.get("KVM_FLEET_WORKDIR", DEFAULT_WORKDIR))
    fleet = Fleet(workdir)
    if not fleet.settings:
        fleet.settings.update(default_settings())
        fleet.save()

    while True:
        print(MENU.format(workdir=fleet.workdir))
        choice = ask("Choose", "5")
        if choice == "1":
            host_checks()
        elif choice == "2":
            configure_settings(fleet)
        elif choice == "3":
            prepare_base_image(fleet)
        elif choice == "4":
            build_template(fleet)
        elif choice == "5":
            deploy_clones(fleet)
        elif choice == "6":
            list_instances(fleet)
        elif choice == "7":
            destroy_instance(fleet)
        elif choice == "8":
            print("bye")
            sys.exit(0)
        else:
            print("pick 1-8")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
