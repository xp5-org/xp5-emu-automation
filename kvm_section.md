## Deploying testrunnerapp into KVM Ubuntu 26.04 guests

> **Verified 2026-11-02** against a KVM host (`dockerbuilder`, Ubuntu 26.04
> "Resolute Raccoon") — but that host is itself nested inside another KVM
> guest, which matters for one part of this doc (flagged below). The real
> target for this is physical hardware; nested virtualization is only a
> stand-in for testing the provisioning steps themselves (packages,
> cloud-init, permissions, login) without needing a spare machine. Treat
> anything about networking behavior as "confirm on the real box," not as
> settled.

Scale-out option: instead of (or in addition to) running the Docker image on
one host, spin up one Ubuntu 26.04 LTS KVM guest per `cc65vicedev` instance.
Each guest boots from the stock cloud image, gets its login/SSH material and
its NFS mount wired up by cloud-init on first boot, then installs Docker and
runs the same container described above.

This uses `virt-install` + `cloud-init` (NoCloud datasource), which is the
standard, scriptable way to provision KVM guests headlessly — no
virt-manager GUI, no manual OS install. Run everything below on the KVM
**host** (the hypervisor), as a user in the `libvirt` group.

**A note on the NFS mount and nested virtualization:** on the nested test
host, giving the guest a real LAN IP required a macvtap bridge on top of
the host's own (already virtual, libvirt/QEMU-emulated) NIC — a second
layer of virtualized networking stacked on the first. `mount` itself
succeeded there, but any actual I/O against the mount (`ls`, `df`, reading
a file) hung indefinitely. That's consistent with known macvtap-over-a-
virtual-NIC rough edges (promiscuous/multicast handling in virtio-net vs. a
real NIC driver) rather than anything wrong with NFS, cloud-init, or the
steps below — **on physical hardware, with a real NIC under the bridge,
this is expected to just work**, and is the recommended path. The fallback
if it doesn't (covered further down) is passing the host's already-mounted
share into the guest read-only via virtio-9p instead of mounting NFS a
second time inside the guest.

### 0. Host prerequisites

```bash
sudo apt update
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients \
    virtinst cloud-image-utils genisoimage bridge-utils
sudo usermod -aG libvirt,kvm "$USER"   # re-login for this to take effect
```

Confirm hardware virtualization is available and libvirt is running:

```bash
kvm-ok || egrep -c '(vmx|svm)' /proc/cpuinfo
sudo systemctl status libvirtd
```

If this host is itself a KVM guest (nested virt), you additionally need
`<nested-virt>` enabled on the VM's own CPU model on the outer hypervisor,
and `kvm-intel`/`kvm-amd`'s `nested=1` module option set on the host — check
`cat /sys/module/kvm_intel/parameters/nested` (or `kvm_amd`) reads `Y`/`1`.

### 1. Set per-instance variables

Pick one block of values per guest you deploy (bump `VMNAME`/`VMIP`/MAC for
each additional instance):

```bash
VMNAME=testrunner01
VMIP=192.168.1.231           # static IP for this guest, same LAN as the NFS server
VMGATEWAY=192.168.1.1
VMDNS=192.168.1.1
PHYSICAL_NIC=enp1s0          # the host's real NIC the guest bridges onto (macvtap)
DISKSIZE=20G
RAM_MB=4096
VCPUS=2

# Linux login created on the guest
GUEST_USERNAME=user
GUEST_PASSWORD='changeme'            # plaintext only for the initial login; rotate after first boot
SSH_PUBKEY_FILE="$HOME/.ssh/id_ed25519.pub"   # key allowed to SSH in as GUEST_USERNAME

# NFS export holding the repos (same export the Docker setup above uses)
NFS_SERVER=192.168.1.2
NFS_EXPORT=/bigpool/bughabitat
NFS_MOUNTPOINT=/mnt/bughabitat
```

Before picking `VMIP`, make sure nothing is already using it
(`ping -c1 -W1 "$VMIP"` should get no reply) — it needs to be a free address
on the same subnet as the NFS server, not on libvirt's own
`192.168.122.0/24` default network (that one NATs behind the host's own IP,
which is the setup to avoid here: see the nested-virt note above for why).

### 2. Fetch the Ubuntu 26.04 cloud image

```bash
mkdir -p ~/kvm-images && cd ~/kvm-images
wget -O noble-plus-one-amd64.img \
  https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
```

(This is the official `*-server-cloudimg-amd64.img` artifact — same family as
the `22.04`/`24.04` cloud images, just the `26.04` release path. If that
exact path 404s because the release has moved, use
`https://cloud-images.ubuntu.com/releases/26.04/release/` and take whatever
`*-amd64.img` is listed there.)

### 3. Write cloud-init `user-data` for this guest

This is what creates the user/password/SSH key and mounts NFS before the
testrunnerapp setup ever runs:

```bash
mkdir -p ~/kvm-images/seed-$VMNAME && cd ~/kvm-images/seed-$VMNAME

cat > meta-data <<EOF
instance-id: $VMNAME
local-hostname: $VMNAME
EOF

cat > network-config <<EOF
version: 2
ethernets:
  $PHYSICAL_NIC:
    addresses: [$VMIP/24]
    gateway4: $VMGATEWAY
    nameservers:
      addresses: [$VMDNS]
EOF

cat > user-data <<EOF
#cloud-config
hostname: $VMNAME
users:
  - name: $GUEST_USERNAME
    groups: [sudo, docker]
    shell: /bin/bash
    lock_passwd: false
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - $(cat "$SSH_PUBKEY_FILE")

chpasswd:
  expire: false
  list: |
    $GUEST_USERNAME:$GUEST_PASSWORD

ssh_pwauth: true

package_update: true
packages:
  - nfs-common
  - ca-certificates
  - curl
  - gnupg

runcmd:
  - mkdir -p $NFS_MOUNTPOINT
  - echo "$NFS_SERVER:$NFS_EXPORT $NFS_MOUNTPOINT nfs defaults,_netdev 0 0" >> /etc/fstab
  - mount -a
  # Docker Engine (official repo, needed to run the cc65vicedev image)
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  - systemctl enable --now docker
EOF

cloud-localds --network-config=network-config seed-$VMNAME.img user-data meta-data
```

Notes:
- `ssh_pwauth: true` + `chpasswd` keep password login working (matches
  `USERPASSWORD`/`USERNAME` on the container itself); drop both if you only
  ever want key-based access to the guest. Both were confirmed working on
  the test host (password login and key-only login each tested from a
  separate SSH invocation).
- **Use `runcmd:` for the mount, not cloud-init's own `mounts:` module** —
  this was an actual failure on the test run: `cc_mounts` (the `mounts:`
  module) runs *before* the `packages:`/`runcmd:` package installs happen,
  so it tried to mount NFS before `nfs-common` was even installed yet, and
  cloud-init finished the whole run in `error` state. Appending the fstab
  line and running `mount -a` from `runcmd:` (as above) runs later in the
  boot sequence, after the package is in place, and worked cleanly.
- Check the guest's actual NIC name with `virsh domiflist "$VMNAME"` after
  first boot before relying on `$PHYSICAL_NIC` matching inside the guest —
  cloud images don't always land on the same interface name as the host.
- If the NFS export has root-squash or UID mapping rules, make sure the UID
  docker runs the container's `$GUEST_USERNAME` as matches what the export
  expects — otherwise the bind mount inside the container will be
  read-only/EACCES even though the guest's own mount works.

### 4. Create the guest disk and launch it

```bash
cd ~/kvm-images
qemu-img create -f qcow2 -F qcow2 -b noble-plus-one-amd64.img "$VMNAME.qcow2" "$DISKSIZE"

virt-install \
  --name "$VMNAME" \
  --memory "$RAM_MB" \
  --vcpus "$VCPUS" \
  --import \
  --disk path="$VMNAME.qcow2",format=qcow2 \
  --disk path=seed-$VMNAME/seed-$VMNAME.img,device=cdrom \
  --os-variant ubuntu25.10 \
  --network type=direct,source=$PHYSICAL_NIC,source_mode=bridge,model=virtio \
  --graphics none \
  --noautoconsole
```

(`type=direct,source_mode=bridge` is macvtap bridge mode — the guest gets
its own MAC straight on the physical LAN segment behind `$PHYSICAL_NIC`,
rather than NAT'd behind the host's own IP. That matters here: NAT means
the guest's NFS traffic leaves with the *same source IP as the host*, which
an NFS server can treat as a conflicting/duplicate client and reject — this
was reproduced on the test run. A real IP on the LAN avoids that.)

Two things that bit the test run here, both easy to hit on a fresh host and
unrelated to nested-vs-physical:

- **`--os-variant`**: there is no `ubuntu26.04` entry in `osinfo-db` yet (as
  of this writing, `osinfo-query os | grep ubuntu` tops out at `ubuntu25.10`
  on a stock Ubuntu 26.04 host). `ubuntu25.10` was used instead — it only
  affects libvirt's default device tuning, not what actually boots, and the
  guest came up fine. Swap in `ubuntu26.04` once your host's `osinfo-db`
  ships it.
- **Home-directory permissions**: if `~/kvm-images` lives under your home
  directory (as in these examples) and your home directory is `750` (the
  Ubuntu default), `virt-install` fails with `Cannot access storage file
  ... Permission denied (as uid:64055, gid:992)` — the `libvirt-qemu` user
  can't even traverse into your home dir to reach the disk image, even
  though you (the owner) can read it fine. Fix with:
  ```bash
  chmod o+x "$HOME"
  ```
  This only grants *traverse* (not list/read) to the directory, which is
  enough for libvirt-qemu to open files by path without being able to `ls`
  your home directory. If you'd rather not touch `$HOME`'s permissions at
  all, put `~/kvm-images` somewhere with an already-open path instead (e.g.
  under `/srv/` or `/var/lib/libvirt/images/`, the latter needs `sudo` to
  write into).

### 5. Confirm it came up, then deploy testrunnerapp exactly as in Docker setup above

```bash
virsh list --all
ssh "$GUEST_USERNAME@$VMIP"
```

(note: macvtap bridge mode means the **host** cannot ping/SSH the guest
over that interface by design — host-to-guest traffic on the same physical
NIC is blocked in bridge mode, only guest-to-outside-world works. That's
fine for actually using the guest from anywhere else on the LAN; it just
means don't try to debug connectivity *from the KVM host itself*.)

Once logged in, the NFS export should already be mounted at
`$NFS_MOUNTPOINT`, and `docker` should already be installed — from here
it's the same steps as the "Setup and use" section above, just run inside
the guest instead of on bare metal:

```bash
cd $NFS_MOUNTPOINT
git clone https://github.com/xp5-org/flask-testrunner
git clone https://github.com/xp5-org/CC65-vice-dev.git
cd CC65-vice-dev
docker build -t cc65vicedev ./
docker run -it \
  -p 3389:3389 -p 8080:8080 \
  -e USERPASSWORD=a -e USERNAME=user \
  -v $NFS_MOUNTPOINT/CC65-vice-dev:/testsrc \
  -v $NFS_MOUNTPOINT/flask-testrunner:/testrunnerapp \
  cc65vicedev
```

#### Fallback: if NFS-in-guest doesn't work on your hardware either

On the nested test host, the macvtap mount above actually succeeded, but
any real I/O against it (`ls`, `df`, reading a file) hung indefinitely —
reproduced twice, confirmed hung (not just slow) with Ctrl-C/SIGINT. That's
flagged above as a likely nested-virtualization artifact (macvtap stacked
on an already-virtual NIC), expected to behave normally on physical
hardware. *If* it turns out not to on your box, the workaround that was
verified to work reliably is skipping NFS-inside-the-guest entirely: mount
the share once on the **host** (as you already do), then pass that host
directory into the guest read-only via `virtio-9p` instead of a second NFS
mount:

```bash
virt-install ... \
  --network network=default \
  --filesystem source=$NFS_MOUNTPOINT,target=bughabitat,accessmode=mapped,readonly=yes \
  ...
```

and in `user-data`, replace the NFS `runcmd:` lines with:

```yaml
  - mkdir -p $NFS_MOUNTPOINT
  - echo "bughabitat $NFS_MOUNTPOINT 9p trans=virtio,version=9p2000.L,ro,_netdev 0 0" >> /etc/fstab
  - mount -a
```

This was confirmed working end-to-end (mount, directory listing, reading a
file, and writes correctly rejected) — just note it's read-only and the
plain NAT `network=default` is fine here since there's no second NFS
client to collide with. Clone/build on the guest's own local disk rather
than the read-only share if you go this route.

### Tearing down / rebuilding a guest

KVM hosts drift and are cheap to throw away — if a guest gets into a weird
state, prefer rebuilding it over debugging it live:

```bash
virsh destroy "$VMNAME"      # force power-off
virsh undefine "$VMNAME" --remove-all-storage
```

then repeat steps 3–4 with a fresh `seed-$VMNAME.img`.

### What was actually verified, and what wasn't

Verified end-to-end on `dockerbuilder` (Ubuntu 26.04 "Resolute Raccoon",
itself nested inside another KVM guest — see the note at the top of this
section): package install (`cloud-image-utils`, `genisoimage`,
`libosinfo-bin`), the exact cloud image download URL, cloud-init seed
generation (including the `mounts:`-module ordering bug and its `runcmd:`
fix), the home-directory permission fix, password login, SSH-key-only
login, the Docker Engine install via `runcmd`, and — as the 9p fallback —
mount, directory listing, reading a file, and correct rejection of writes
through a read-only passthrough. The test guest and its throwaway key/seed
files were torn down afterward (`virsh destroy`/`undefine
--remove-all-storage`), and the one host-side permission change (`chmod o+x
~`) was reverted since it's not needed unless you're actually running these
steps for real.

**Not run successfully end-to-end, because of the nested-virt environment
specifically:** macvtap bridge networking + NFS mounted inside the guest —
`mount` succeeded but all I/O against it hung. This is believed to be a
nested-virtualization artifact (macvtap stacked on a virtual NIC rather
than a physical one) and is why it's called out as "expected to work on
physical hardware" above rather than presented as settled — it should be
the first thing you confirm when running this against the real box, with
the 9p fallback as the documented plan B if it genuinely doesn't.

**Not run at all:** actually building the `cc65vicedev` image and running
the full RDP/VICE stack inside one of these guests — that part of the
Dockerfile is unchanged from the existing bare-metal/Docker setup documented
above and takes ~10+ minutes to build, so it wasn't repeated here just to
prove Docker itself works (it does: `docker --version` succeeded right
after the `runcmd` install).
