#!/bin/sh
# Idempotent testrunnerapp deploy for FreeBSD/GhostBSD 14.x. Run: sudo sh freebsd_deploy.sh
# See freebsd_deploy_notes.md. Set RUNUSER to the owner of the venv.
set -e
RUNUSER=${RUNUSER:-${SUDO_USER:-user}}
HOME_DIR=$(getent passwd "$RUNUSER" | cut -d: -f6)
NFS=192.168.1.2:/bigpool/bughabitat

pkg update -f
pkg install -y tesseract tesseract-data qemu xdotool ImageMagick7 mtools \
  cdrkit-genisoimage git python313 py313-sqlite3 \
  pkgconf meson ninja cmake patchelf rust openblas \
  jpeg-turbo png freetype2 tiff webp lcms2 openjpeg libffi

# NFS + nullfs mounts
sysrc nfs_client_enable=YES rpcbind_enable=YES
service rpcbind start || true
service nfsclient start || true
mkdir -p /mnt/bughabitat /testsrc /testrunnerapp
grep -q "$NFS" /etc/fstab || cat >> /etc/fstab <<EOF
$NFS	/mnt/bughabitat	nfs	rw,nfsv3,tcp,nolockd,bg,late,noatime	0	0
/mnt/bughabitat/testsrc	/testsrc	nullfs	rw,late	0	0
/mnt/bughabitat/testrunnerapp	/testrunnerapp	nullfs	rw,late	0	0
EOF
mount | grep -q ' /mnt/bughabitat ' || mount /mnt/bughabitat
mount | grep -q ' /testsrc ' || mount /testsrc
mount | grep -q ' /testrunnerapp ' || mount /testrunnerapp

# Missing crt object in GhostBSD base (rust linking)
if [ ! -e /usr/lib/Scrt1.o ]; then
  cd /var/tmp
  fetch -o base.txz https://download.freebsd.org/releases/amd64/14.4-RELEASE/base.txz
  tar -xf base.txz ./usr/lib/Scrt1.o
  install -o root -g wheel -m 444 usr/lib/Scrt1.o /usr/lib/Scrt1.o
  rm -rf base.txz usr
fi

# venv (py>=3.12 required by the app)
su "$RUNUSER" -c "
  [ -d '$HOME_DIR/testrunner-venv' ] || python3.13 -m venv '$HOME_DIR/testrunner-venv'
  '$HOME_DIR/testrunner-venv/bin/pip' install Flask numpy pillow pexpect pytesseract \
    beautifulsoup4 coverage selenium vncdotool
"
echo "Done. Start: cd /testrunnerapp && $HOME_DIR/testrunner-venv/bin/python app.py"
