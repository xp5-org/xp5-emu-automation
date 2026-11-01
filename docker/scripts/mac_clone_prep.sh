#!/bin/bash
# Run on a freshly cloned macOS VM, after booting it with a NEW MAC address set on
# the QEMU side (macOS cannot change its own virtual NIC MAC; the hypervisor owns it).
#
#   sudo ./mac_clone_prep.sh NEW-HOSTNAME [--reboot]
#
# Renames the host, regenerates SSH host keys, renews DHCP, restarts the testrunner,
# and prints the identity values so you can confirm the clone is distinct.
set -u
[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"

NEW="${1:-}"; REBOOT="${2:-}"
if [ -z "$NEW" ] || ! echo "$NEW" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9-]{0,62}$'; then
  echo "usage: sudo $0 NEW-HOSTNAME [--reboot]   (letters, digits, hyphen)"; exit 2
fi
USERNAME="${SUDO_USER:-qemu}"
UID_=$(id -u "$USERNAME")
IF=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}'); IF=${IF:-en0}

echo "== before: $(scutil --get LocalHostName)  $(ifconfig $IF | awk '/ether/{print $2}')  $(ipconfig getifaddr $IF)"

echo "== hostname -> $NEW"
scutil --set ComputerName "$NEW"
scutil --set LocalHostName "$NEW"
scutil --set HostName "$NEW"
dscacheutil -flushcache; killall -HUP mDNSResponder 2>/dev/null

echo "== regenerating SSH host keys"
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A >/dev/null
launchctl kickstart -k system/com.openssh.sshd 2>/dev/null

echo "== renewing DHCP on $IF"
ipconfig set "$IF" DHCP
sleep 5

echo "== restarting testrunner"
launchctl kickstart -k "gui/$UID_/com.testrunner.app" 2>&1 | head -1

echo
echo "== identity now (a clone must differ from its source in MAC; check UUID/serial too)"
echo "hostname : $(scutil --get LocalHostName)"
echo "MAC ($IF): $(ifconfig $IF | awk '/ether/{print $2}')"
echo "IP       : $(ipconfig getifaddr $IF)"
ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID|IOPlatformSerialNumber/{print $2": "$4}'
echo "ssh key  : $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"

if [ "$REBOOT" = "--reboot" ]; then echo "rebooting..."; shutdown -r now; fi
