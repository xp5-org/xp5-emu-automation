# macOS testrunner deploy (validated on qemu@192.168.1.160, macOS 15.8.1 arm64, 2026-09-28)

Supplements mac_deploy_notes.odt (steps 1-4 there: ssh, homebrew, xcode, sleep settings). Do NOT use its fstab/automount NFS step -- /Volumes is wiped on every boot, so it breaks after a reboot. Use step 2 below instead.

1. Deps (no sudo needed): `brew install qemu tesseract python@3.12`
   Non-interactive ssh has no brew on PATH: use /opt/homebrew/bin/brew.
2. NFS mount via root LaunchDaemon (replaces fstab/automount; leave /etc/fstab without a bughabitat line):
   - /usr/local/libexec/mountbughabitat.sh : mkdir -p /Volumes/bughabitat; retry `mount_nfs -o vers=3,resvport,locallocks,rw 192.168.1.2:/bigpool/bughabitat /Volumes/bughabitat`
   - /Library/LaunchDaemons/com.testrunner.mountbughabitat.plist (root:wheel 644, RunAtLoad, log /var/log/mountbughabitat.log)
   - `sudo launchctl bootstrap system /Library/LaunchDaemons/com.testrunner.mountbughabitat.plist`
   - `locallocks` is required or qemu-img fails "Failed to lock byte 100".
3. Root symlinks. `/` is read-only on macOS 15, so `ln -s` fails; use synthetic.conf (TAB-separated), then no reboot needed:
   ```
   printf 'testsrc\tVolumes/bughabitat/testsrc\ntestrunnerapp\tVolumes/bughabitat/testrunnerapp\n' | sudo tee -a /etc/synthetic.conf
   sudo /System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util -t
   ```
4. venv on Homebrew python: `/opt/homebrew/bin/python3.12 -m venv ~/testrunnerapp-venv && ~/testrunnerapp-venv/bin/pip install -r /testsrc/requirements.txt`
   (Xcode's python3.9 works too but is a poor TCC target.)
5. LaunchAgent ~/Library/LaunchAgents/com.testrunner.app.plist: runs venv python on /testrunnerapp/app.py, WorkingDirectory /testrunnerapp, EnvironmentVariables.PATH=/opt/homebrew/bin:..., RunAtLoad+KeepAlive, log ~/testrunnerapp.log.
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.testrunner.app.plist`
6. FIRST START: macOS shows a "Python would like to access files on a network volume" dialog on the CONSOLE display (not ssh). Until someone clicks Allow (VNC/console) the process hangs at 0% CPU in startup. One-time.
7. Verify: `curl localhost:8080/` -> 200; app is on port 8080 (bare, not docker).

Gotchas: launchd must have a GUI session (auto-login) for gui/<uid> agents. Restart after testlist edits: `launchctl kickstart -k gui/$(id -u)/com.testrunner.app`. Env changes to the plist need bootout+bootstrap.
Not done here: `sudo pmset -a disablesleep 1`, screensaver idleTime 0, autologin (from the odt).

## 86Box tests (added 2026-09-28)
8. `brew install qt@5` -- the build_store 86Box binary links /opt/homebrew/opt/qt@5 (deprecated, disabled upstream 2027-05-19, still bottled; keg-only so the qtbase conflict warning is harmless). Symptom if missing: "Timeout waiting for the 86Box window" + `dyld: Library not loaded ... QtNetwork`. Check any build with: `otool -L <86Box> | grep /opt/homebrew` and confirm each path exists.
9. ROMs: copy from a Linux runner: `tar cf - -C /opt/86box roms | ssh qemu@HOST 'mkdir -p ~/Library/Application\ Support/86Box && tar xf - -C ~/Library/Application\ Support/86Box'` (1618 files, ~148M). Missing ROMs = hidden modal dialog, window never appears.
10. Grant Screen Recording + Accessibility to python3.12 (Cellar path) in System Settings on the console -- needed for 86Box screenshots/input; the process must request access itself (happens on first 86Box start), then toggle it on.
11. pyobjc (NOT in requirements.txt): `~/testrunnerapp-venv/bin/pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa`, then restart the app. Without it box86helpers sets Quartz=None and silently does nothing on macOS: window wait times out even though 86Box is visibly running, no Screen Recording/Accessibility prompts appear, and no keystrokes are sent.

## Desktop helpers (added 2026-09-28)
12. ~/Desktop/testrunner_start.sh (kickstart if loaded else bootstrap; warns if NFS unmounted; waits for :8080 -> 200) and ~/Desktop/testrunner_stop.sh (bootout + pkill 86Box). Both chmod +x. Finder double-click opens .sh in an editor: run from Terminal, or rename to .command.

## Cloning this VM (added 2026-09-28)
13. Set a NEW MAC on the QEMU side before first boot of the clone (`mac=` on the netdev/NIC device). macOS cannot change or regenerate a virtual NIC's MAC itself; identical MACs = same DHCP lease = IP conflict with the source VM. Hardware UUID/serial (VirtualMac2,1) also come from the hypervisor config -- confirm they differ (the script prints them).
14. On the clone run `sudo /Volumes/bughabitat/mac_clone_prep.sh NEW-HOSTNAME [--reboot]`: sets ComputerName/LocalHostName/HostName, regenerates SSH host keys, renews DHCP, restarts com.testrunner.app, prints MAC/IP/UUID/serial/key fingerprint. The clone already carries the mount daemon, symlinks, venv, LaunchAgent and the granted TCC permissions.
15. Then re-check: `curl localhost:8080/`; if the clone's DHCP-reserved IP is used elsewhere (node lists, ssh known_hosts), update those.
