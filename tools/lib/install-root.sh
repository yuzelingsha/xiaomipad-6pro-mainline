#!/bin/sh
# SPDX-License-Identifier: MIT
# Device-side installer, intended only for the dedicated read-only RAM image.
set -eu
die() { printf 'liuqin-install: %s\n' "$*" >&2; exit 1; }
[ "$#" = 7 ] || [ "$#" = 8 ] ||
	die 'usage: install-root.sh BOOT_ID ROOTFS_URL SHA256 BYTES ERASE-LIUQIN-USERDATA ROOT_PARTNAME HOME_PARTNAME [ENABLE-USB-RESCUE]'
[ "$5" = ERASE-LIUQIN-USERDATA ] || die 'explicit data-erasure acknowledgement required'
root_name=$6
home_name=$7
rescue=${8:-}
case $rescue in ''|ENABLE-USB-RESCUE) ;; *) die 'unsupported rescue option' ;; esac
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM boot identity changed'
[ "$(cat /etc/liuqin-installer 2>/dev/null)" = liuqin ] || die 'not the installer RAM image'
ln -sf /proc/self/fd/0 /dev/stdin
# The host checks Fastboot product/serial; boot_id binds this operation to it.
# Partition numbers and offsets are not pinned: liuqin capacity variants place
# the tail partitions differently, and the layout step may have just created
# these two.  PARTNAME is the identity, read from sysfs rather than from
# /dev/disk/by-partlabel, which is built once at boot from a fixed name list
# and therefore cannot describe a table the layout step has changed.
find_part() {
	found=
	for info in /sys/class/block/sd*/uevent; do
		/bin/busybox grep -qx "PARTNAME=$1" "$info" || continue
		[ -z "$found" ] || die "duplicate partition name: $1"
		node=${info%/uevent}
		found=/dev/${node##*/}
	done
	[ -n "$found" ] && [ -b "$found" ] ||
		die "partition is missing: $1 —— 该设备不是受支持的小米平板 6 Pro（liuqin），或分区步骤未完成"
	printf '%s' "$found"
}
root=$(find_part "$root_name")
home=$(find_part "$home_name")
[ "$root" != "$home" ] || die 'the system and home partitions must differ'
node=${root##*/}
size=$(cat "/sys/class/block/$node/size")
case $size in ''|*[!0-9]*) die 'system partition size is unavailable' ;; esac
[ "$size" -ge 33554432 ] || die 'the system partition is smaller than 16 GiB —— 不支持该布局'
home_size=$(cat "/sys/class/block/${home##*/}/size")
case $home_size in ''|*[!0-9]*) die 'home partition size is unavailable' ;; esac
[ "$home_size" -ge 16777216 ] || die 'the home partition is smaller than 8 GiB —— 不支持该布局'
parent_path=$(/bin/busybox readlink -f "/sys/class/block/$node/.." 2>/dev/null || true)
[ -n "$parent_path" ] || die 'the system partition parent disk is unavailable'
parent=/dev/${parent_path##*/}
[ -b "$parent" ] || die 'the system partition parent disk is missing'
for target in "$root" "$home"; do
	device_number=$(cat "/sys/class/block/${target##*/}/dev")
	awk -v device="$device_number" '$3 == device {found=1} END {exit !found}' /proc/self/mountinfo &&
		die 'a target partition is mounted (including through a device alias)'
	[ "$(/bin/busybox blockdev --getro "$target")" = 1 ] || die 'a target partition is not initially read-only'
done
battery=
for supply in /sys/class/power_supply/*; do
	[ "$(cat "$supply/type" 2>/dev/null)" = Battery ] || continue
	battery=$(cat "$supply/capacity")
	break
done
case $battery in ''|*[!0-9]*) die 'battery level is unavailable' ;; esac
[ "$battery" -ge 30 ] || die 'charge the tablet to at least 30 percent before installation'
case $3 in *[!0-9a-f]*|'') die 'invalid rootfs hash' ;; esac
[ "${#3}" = 64 ] || die 'invalid rootfs hash length'
case $4 in ''|*[!0-9]*) die 'invalid archive size' ;; esac
available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
[ "$4" -gt 0 ] && [ "$(( $4 + 536870912 ))" -lt "$((available * 1024))" ] ||
	die 'insufficient RAM to stage this archive safely'
archive=/mnt/install-download/rootfs.tar.gz
mounted=false
home_mounted=false
opened=false
persist_mounted=false
download_mounted=false
cleanup() {
	sync
	if [ "$home_mounted" = true ]; then
		umount /mnt/install-home || return 1
		home_mounted=false
	fi
	if [ "$mounted" = true ]; then
		umount /mnt/install || return 1
		mounted=false
	fi
	if [ "$opened" = true ]; then
		/bin/busybox blockdev --setro "$home"
		/bin/busybox blockdev --setro "$root"
		/bin/busybox blockdev --setro "$parent"
		opened=false
	fi
	if [ "$persist_mounted" = true ]; then
		umount /run/persist || return 1
		persist_mounted=false
	fi
	if [ "$download_mounted" = true ]; then
		umount /mnt/install-download || return 1
		download_mounted=false
	fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
# A default /run tmpfs is too small for some complete desktop archives.
mkdir -p /mnt/install-download /run/persist /mnt/install /mnt/install-home
mount -t tmpfs -o "size=$(( $4 + 16777216 ))" tmpfs /mnt/install-download
download_mounted=true
/bin/busybox wget -O "$archive" "$2"
[ "$(stat -c %s "$archive")" = "$4" ] || die 'downloaded archive size mismatch'
printf '%s  %s\n' "$3" "$archive" | /bin/busybox sha256sum -c -
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM identity changed before formatting'
[ -b /dev/disk/by-partlabel/persist ] || die 'persist is missing'
mount -t ext4 -o ro,noload /dev/disk/by-partlabel/persist /run/persist
persist_mounted=true
for item in wlan/wlan_mac.bin bluetooth/.bt_nv.bin audio/crus_calr.bin; do
	[ -f "/run/persist/$item" ] || die 'factory data is incomplete'
done
[ -d /run/persist/sensors/registry/registry ] || die 'factory sensor registry is missing'
/bin/busybox blockdev --setrw "$parent"
opened=true
/bin/busybox blockdev --setrw "$root"
/bin/busybox blockdev --setrw "$home"
/usr/sbin/mkfs.ext4 -F -L LIUQIN_ROOT -m 0 "$root"
/usr/sbin/mkfs.ext4 -F -L LIUQIN_HOME -m 0 "$home"
mount -t ext4 "$root" /mnt/install
mounted=true
mkdir /mnt/install/native-root
/usr/bin/tar -xzf "$archive" -C /mnt/install/native-root \
	--numeric-owner --same-owner --same-permissions --acls --xattrs --xattrs-include='*' --warning=no-timestamp
PERSIST_SRC=/run/persist sh /usr/lib/liuqin/provision.sh /mnt/install/native-root
if [ "$rescue" = ENABLE-USB-RESCUE ]; then
	touch /mnt/install/native-root/etc/liuqin-rescue-enabled
fi
# /home is a separate partition so that a later system reinstall can keep it.
# The label is the identity here too; nofail keeps a missing or unreadable
# home partition from holding up the boot at the console-less first start.
mount -t ext4 "$home" /mnt/install-home
home_mounted=true
if [ -d /mnt/install/native-root/home ]; then
	/usr/bin/tar -C /mnt/install/native-root/home -cf - . --numeric-owner --acls --xattrs \
		--xattrs-include='*' | /usr/bin/tar -C /mnt/install-home -xf - \
		--numeric-owner --same-owner --same-permissions --acls --xattrs --xattrs-include='*' \
		--warning=no-timestamp
fi
{
	printf '# Written by the liuqin installer. The root filesystem is mounted by\n'
	printf '# the boot image, which finds it by LABEL=LIUQIN_ROOT.\n'
	printf 'LABEL=LIUQIN_HOME\t/home\text4\tdefaults,nofail,x-systemd.device-timeout=30s\t0\t2\n'
} >/mnt/install/native-root/etc/fstab
chmod 0644 /mnt/install/native-root/etc/fstab
[ -n "$(/usr/sbin/getcap /mnt/install/native-root/usr/lib/snapd/snap-confine)" ] ||
	die 'snap-confine capability was not restored'
# Verify immutable boot-contract files after extraction and provisioning.
tail -n +2 /etc/liuqin-native-root.contract | while read -r expected path; do
	[ -n "$expected" ] || continue
	actual=$(/bin/busybox sha256sum "/mnt/install/native-root$path" | /bin/busybox cut -d' ' -f1)
	[ "$actual" = "$expected" ] || die "root contract mismatch: $path"
done
cleanup
trap - EXIT HUP INT TERM
printf 'liuqin-install: ROOT_INSTALLED\n'
