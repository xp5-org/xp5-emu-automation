# OpenIndiana (illumos) runner deploy notes

Tested on OpenIndiana Hipster 2026.04 KVM guest, 192.168.1.163. Guest sees the NFS
share at /mnt/bughabitat (testsrc/, testrunnerapp/). Root = `su - root` (no sudo).
GNU tools: put /usr/gnu/bin first in PATH. `pkg` searches are slow, run in background.

Host is OpenIndiana (illumos), not OpenSolaris; hostname `openindiana`, 6 vCPUs, ZFS-snapshotted
so it can be reverted. The boot message "no MCA support on many cores" is harmless (the KVM
CPU model doesn't expose Machine Check Architecture).

## 0. Access
- Log in as `kvmuser` (uid 100, gid 1, home /export/home/kvmuser). Add the agent key:
      mkdir -p ~/.ssh && chmod 700 ~/.ssh; cat agentkey.pub >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
  then `ssh -i ~/.ssh/agentkey kvmuser@192.168.1.163`. kvmuser has NO sudo.
- Root over ssh with a password is refused; use `su - root` from kvmuser. su needs a tty, so
  from scripts use `ssh -tt` or a python pty wrapper that answers the "Password:" prompt.
- Long jobs: `nohup ... > log 2>&1 < /dev/null &`, otherwise the ssh session hangs until the
  job exits. `pkg list -a` hides versions blocked by incorporations; use `-af` to see all.

## 1. Share + paths (root)
    ln -s /mnt/bughabitat/testsrc /testsrc
    ln -s /mnt/bughabitat/testrunnerapp /testrunnerapp
app.py hardcodes /testsrc and /testrunnerapp, there is no path setting.

## 2. Packages (root)
    pkg install gcc-14 cmake ninja pkg-config git libslirp gettext ccache runtime/python-314
    pkg install x11/server/xvfb developer/build/gnu-make
Also needed for the 86Box build: Qt6 (6.10), SDL2, OpenAL, Vulkan headers (see step 5).
gnu-make gives `gmake` (Sun make can't read GNU Makefiles). No xdotool/tesseract in IPS.

## 3. Runner user = uid 1001 (root)
    useradd -u 1001 -g 1 -d /export/home/trunner -m -s /bin/bash trunner
Why: the NFS export squashes every write to uid 1001. As any other uid, file creates
work but chmod/utime fail ("Not owner"), which breaks shutil.copytree in test_start_86box.

## 4. Python venv (as kvmuser or trunner-readable path)
    python3.14 -m venv ~/venv && ~/venv/bin/python -m ensurepip
    ~/venv/bin/pip install flask pillow pytesseract beautifulsoup4 numpy pexpect
(from /testsrc/requirements.txt). vncdotool fails (cryptography needs Rust); only the
QEMU helper imports it, lazily, so skip it. pygame builds from source (slow).

## 5. Build 86Box from source (the part that bites)
Source: /mnt/bughabitat/win_scripts_old/86box_src_nullmodem/86Box (/mnt/bughabitat/86box_testsrc
is empty). Copy it to local disk (`cp -r ... ~/b/src`, drop its `build/` dir; ~650MB) and build
out-of-tree in ~/b/build; don't build on NFS. Vulkan headers aren't in IPS and Qt6 here has no
Vulkan, but the source needs vulkan/vulkan.h: `git clone --depth 1
https://github.com/KhronosGroup/Vulkan-Headers ~/b/vk` (guest has internet; slow).
Not in IPS at all: rtmidi, fluidsynth, libserialport, libevdev, alsa, jack, sndio, munt, so build
with RTMIDI/FLUIDSYNTH/MUNT/SOUNDCANVAS off (libserialport only warns). Defaults that fail:
SDL3 (use -DSDL2=ON), Qt5 (use -DUSE_QT6=ON), OpenAL's cmake needs POLICY_VERSION_MINIMUM=3.5.
Source patches needed (in the scratch copy only, nothing upstream changed):
- snd_imfc.cpp: `floor(1 << 16)` is ambiguous -> `floor((double) (1 << 16))`
- qt_vulkanwindowrenderer.hpp: wrap `#include <QVulkanWindow>` in `#if QT_CONFIG(vulkan)`
  (include <QtGui/qtguiglobal.h> first); the header is otherwise already guarded.
compat.h (`-include`d for C and C++) does: include stdarg.h stdio.h math.h ucontext.h
floatingpoint.h sys/types.h sys/socket.h termios.h; `#define SO_REUSEPORT SO_REUSEADDR` if
missing; a static inline `cfmakeraw()` (missing on illumos); then `#undef CR0 CR1 CR2 CR3 CS SS
DS ES FS GS`. Why: illumos headers lack va_list in 86box.h/log.h, and termios.h/regset.h define
those names as numeric macros that break 86Box's `CR0` union member and its cs/ds/... macros.
Needs `-fPIC -fno-plt`. Why: illumos gcc has no -pie, and a non-PIC exe gets a PLT stub
as the "address" of Qt library functions, so every `connect(x, &Class::signal, ...)` is
invalid ("signal not found"). Result: renderer never created, black window, and a
segfault (null rendererWindow) on the first mouse event. Verify after linking:
    elfdump -s src/86Box | grep QActionGroup9triggeredEP7QAction   # value must be 0x0
cmake flags used (Release, Qt6 on; dynarec/rtmidi/fluidsynth/munt/discord/vnc off):
    -DCMAKE_C_FLAGS="-fPIC -fno-plt -include <compat.h>"
    -DCMAKE_CXX_FLAGS="-fPIC -fno-plt -include <compat.h> -I<vk>/include"
    -DSDL2=ON -DSDL2_DIR=/usr/lib/amd64/cmake/SDL2 -DVulkan_INCLUDE_DIRS=<vk>/include
    -DCMAKE_PREFIX_PATH="/usr/lib/qt/6.10/lib/amd64/cmake;/usr/lib/amd64/cmake/SDL2"
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DQT=ON -DUSE_QT6=ON -DOPENAL=ON
If the link picks the 32-bit /usr/lib/libopenal.so, sed build.ninja to /usr/lib/amd64/.
compat.h (illumos shim) and vk/ headers live in ~kvmuser/b/. Build: `ninja -j12 86Box`.

## 6. Where the code looks for 86Box (root)
    ln -s <path>/86Box /usr/local/bin/86box        # default binary name is "86box" on PATH
    ln -s <ROM dir> /opt/86box/roms                # code passes -R /opt/86box/roms
ROMs: copy the /opt/86box/roms tree from a Linux runner (~2200 files).

## 7. xdotool (no IPS package)
    git clone https://github.com/jordansissel/xdotool && cd xdotool
    gmake PREFIX=$HOME/xdo-inst CFLAGS="-D__EXTENSIONS__ -D_XOPEN_SOURCE=700" && gmake install PREFIX=$HOME/xdo-inst
    ln -s $HOME/xdo-inst/bin/xdotool /usr/local/bin/xdotool     (root)
Needed for window detection, focus and typing. Without it start_86box times out.

## 8. Display: run in the user's desktop session (like linux/mac/bsd)
Desktop is MATE via lightdm on :0. Let the runner user draw on it (run as kvmuser in the
session, or root with kvmuser's XAUTHORITY):
    DISPLAY=:0 XAUTHORITY=/export/home/kvmuser/.Xauthority xhost +SI:localuser:trunner
Resets on X restart. Xvfb (:99) works as a headless fallback but nothing is visible.

## 9. hostflags
DONE (2026-09-29): hostflags.py now has an OPENSOLARIS arch type/flag (and FREEBSD). detect()
returns ["OPENSOLARIS", "LINUX_VIRT"] for sys.platform "sunos5", so posix/LINUX_VIRT tests still
run here and OPENSOLARIS-only ones (the build job) are accepted. Do NOT set
TESTRUNNER_HOST_FLAGS any more: it overrides detection (source shows as "env" in the Hosts
table). ~kvmuser/start_app.sh still exports it; the Desktop scripts (step 10) do not.

## 10. Start app.py (port 8080)
Desktop scripts (2026-09-29), ~kvmuser/Desktop/start_testrunner.sh and stop_testrunner.sh: run
as whoever runs them (kvmuser), log ~/testrunner.log, pid in ~/testrunner.pid, match the
process by `pgrep -f "^/export/home/kvmuser/venv/bin/python app.py"`. Neither can touch an app
started as trunner (different uid), so stop that one once as root/trunner first. They do NOT
check /api/v1/runs/current for a live run before stopping -- check it yourself first
(`curl localhost:8080/api/v1/runs/current`, "busy": false) or you kill someone's test.
Running as kvmuser (uid 100) instead of trunner (1001) has a cost, see step 12.
The older trunner launcher:
Launcher (~kvmuser/start_app.sh), run as `su trunner -c /export/home/kvmuser/start_app.sh`:
    export PATH=/usr/local/bin:/usr/gnu/bin:/usr/bin:/usr/sbin:/sbin
    export TESTRUNNER_HOST_FLAGS=LINUX_VIRT DISPLAY=:0 HOME=/export/home/trunner
    cd /testrunnerapp && nohup ~kvmuser/venv/bin/python app.py > ~trunner/app.log 2>&1 &
No autostart yet. Start/stop tests through the API, not by hand:
    curl -XPOST localhost:8080/api/v1/runs -H 'Content-Type: application/json' \
         -d '{"test":"86box-486.86box-boot"}'      # add "mode":"step" for step-by-step + /adhoc
    curl -XPOST localhost:8080/api/v1/runs/current/stop

## 11. Known gaps / gotchas
- tesseract not built: every test_ocrwordsearch step fails until it is (leptonica + tesseract
  from source). BIOS renders fine; verify by screenshot (import -window <id>).
- Qt disables MIT-SHM (SysV shm limits) and OpenAL has no /dev/audio: harmless so far.
- The 86box wrapper for debugging (extra logs + core dumps) is optional; plain symlink works.
- Debug a hang: `pstack <pid>`, dtrace (`dtrace -p`), core via `mdb core` (::status, $C).
- Qt6 here has no Vulkan, so the Vulkan renderer is compiled out (OpenGL and software work).
- A full 86Box build is ~950 ninja steps; the first pass fails on a handful of files (see the
  patches in step 5), so use `ninja -k 0` to see every error in one run. More vCPUs speed it up.
- Python 3.9 is the system default; python3.14 is installed by runtime/python-314 (3.12 and 3.13
  are also in IPS). Anything needing 3.12+ (the runner) must use the venv.
- css_ofdm_audiomodem/__testlist__86box_rsftest import error is a repo parent-name mismatch.

## 12. Runner-side changes and the build job (2026-09-29)
Why the Instances tab was missing this host: pyhelpers/nodestatus.py had no SunOS branch, so
it fell into the macOS branch, `sysctl`/`vm_stat` raised FileNotFoundError every cycle and
/testrunnerapp/nodestatus/openindiana.json was never written (cluster/workers/openindiana.json
was, which made it look half-alive). Fixed: OS name from the first line of /etc/release (there
is no /etc/os-release), CPU% from summed `kstat -p -m cpu -n sys` cpu_ticks_*, memory from
`kstat -p -m unix -n system_pages` physmem/freemem x pagesize. FreeBSD/GhostBSD hit the same
macOS fall-through class of bug (check nodestatus.py's current state before relying on that).

buildhelper.py: added platform "opensolaris" (PLATFORMS, RUN_PLATFORMS, host_platform() for
sunos, detect_platform() for ELF EI_OSABI 6 = Solaris; the test_detail.html dropdowns list it).
Without it an illumos ELF was filed as "linux" and offered to Linux runners.

buildhelper._snapshot now uses shutil.copyfile, not copy2. copy2's trailing utime() raises
"Not owner" when the runner uid (kvmuser, 100) differs from the uid the NFS export squashes to
(1001): the bytes are copied, the rename never happens, a full-size 86Box.partial is left in
build_store/<md5>/ and the build silently never registers (no build_object row, no message).
Same root cause as step 3. Cleaner fix: run the runner as uid 1001 (trunner, or renumber
kvmuser -- needs root; 1001 is already trunner). Until then other NFS writes as uid 100 that
chmod/utime can still hit "Not owner".

Build job: sourcedir/86box_null_modem/__testlist__86box_localonly_build_openindiana.py (clone of
the FreeBSD one), host_flag OPENSOLARIS, build dir /var/tmp/86box_build_openindiana, output
{build_dir}/src/86Box. Trigger:
    curl -XPOST 192.168.1.163:8080/api/v1/runs -H 'Content-Type: application/json' \
         -d '{"test":"86box-null-modem.86box-localonly-build-openindiana"}'
Restart app.py after testlist edits. Clean build ~3.5 min on 60 CPUs (build -j = nproc; the
host reports 60 CPUs now, not 6). Verified: 5/5 steps pass, build_object id 12 platform
opensolaris, md5 90912d46..., 36485728 bytes. Not verified: running the binary in a test.
It builds the repo fork xp5_86box_serialcomport_repo/86Box DIRECTLY, which differs from the
manual recipe in step 5:
- Qt5 works (USE_QT6=off): configure needs -DQt5_DIR, -DQt5LinguistTools_DIR under
  /usr/lib/qt/5.15/lib/amd64/cmake (not on cmake's default path). No source patches were
  needed for this tree (the snd_imfc.cpp / qt_vulkanwindowrenderer.hpp patches in step 5 apply
  to the older win_scripts_old copy).
- SDL2=ON, DYNAREC/NEW_DYNAREC/RTMIDI/FLUIDSYNTH/MUNT/DISCORD off, POLICY_VERSION_MINIMUM=3.5.
- OpenAL: -DOpenAL_DIR=/usr/lib/amd64/cmake/OpenAL. The default /usr/lib/cmake/OpenAL is the
  32-bit config; setting OPENAL_LIBRARY alone does NOT help (the imported target wins), the
  link fails "wrong ELF class". No more sed on build.ninja.
- Link needs -lsocket -lnsl (CMAKE_CXX_STANDARD_LIBRARIES); libc has no socket()/bind()/send().
- Same compat header, kept in the repo now: sourcedir/86box_null_modem/illumos_compat.h (a copy
  of ~kvmuser/b/compat.h). Same -fPIC -fno-plt reasoning as step 5.
- The Vulkan-Headers checkout is still ~kvmuser/b/vk/include (testlist key "vulkan_include");
  the step fails with a clear MISSING message if it is absent.
- Host prep: sourcedir/86box_null_modem/openindiana_build_prereqs.sh (package names checked
  against `pkg list`, script itself not run). libserialport / rtmidi / fluidsynth / libsamplerate
  are not installed; only libserialport warns.

Missing / not done:
- ROMs, xdotool and tesseract status: see steps 6, 7, 11 (tesseract still absent).
- No autostart; `kvmuser` has no sudo (an attempted `sudo -u trunner` is logged as an
  incident by sudo -- don't retry, use `su - root`).
- This host is .163. .166 is the FreeBSD box; don't mix them up when testing.
