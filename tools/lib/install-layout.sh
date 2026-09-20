#!/bin/sh
# SPDX-License-Identifier: MIT
# Device-side partition-table operations, intended only for the dedicated
# read-only RAM image.  The host computes every layout decision and every
# sgdisk argument; this script owns the guards, the privilege window and the
# read-only posture that must hold before and after each write.
set -eu
die() { printf 'liuqin-layout: %s\n' "$*" >&2; exit 1; }
[ "$#" -ge 2 ] || die 'usage: install-layout.sh BOOT_ID VERB [ARGUMENT...]'
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM boot identity changed'
[ "$(cat /etc/liuqin-installer 2>/dev/null)" = liuqin ] || die 'not the installer RAM image'
verb=$2
shift 2

BB=/bin/busybox
SGDISK=/usr/sbin/sgdisk
[ -x "$SGDISK" ] || die 'sgdisk is missing from the installer runtime'

# Resolve a partition by PARTNAME.  /dev/disk/by-partlabel is built once at
# boot from a fixed name list, so it cannot describe a table this script is
# about to change; the sysfs uevent files always can.
find_part() {
	found=
	for info in /sys/class/block/sd*/uevent; do
		"$BB" grep -qx "PARTNAME=$1" "$info" || continue
		[ -z "$found" ] || die "duplicate partition name: $1"
		node=${info%/uevent}
		found=/dev/${node##*/}
	done
	[ -n "$found" ] && [ -b "$found" ] || die "partition is missing: $1"
	printf '%s' "$found"
}

parent_disk() {
	node=${1##*/}
	path=$("$BB" readlink -f "/sys/class/block/$node/.." 2>/dev/null || true)
	[ -n "$path" ] || die 'parent disk is unavailable'
	disk=/dev/${path##*/}
	[ -b "$disk" ] || die 'parent disk is missing'
	printf '%s' "$disk"
}

sector_size() {
	value=$(cat "/sys/class/block/${1##*/}/queue/logical_block_size")
	case $value in ''|*[!0-9]*) die 'logical sector size is unavailable' ;; esac
	printf '%s' "$value"
}

# /sys/class/block/*/size counts 512-byte units whatever the logical size is.
sector_count() {
	value=$(cat "/sys/class/block/${1##*/}/size")
	case $value in ''|*[!0-9]*) die 'disk size is unavailable' ;; esac
	printf '%s' "$(( value * 512 / $(sector_size "$1") ))"
}

assert_idle() {
	for info in /sys/class/block/"${1##*/}"*/dev; do
		[ -f "$info" ] || continue
		number=$(cat "$info")
		awk -v device="$number" '$3 == device {found=1} END {exit !found}' /proc/self/mountinfo &&
			die 'a partition of the target disk is mounted'
	done
}

# Restore the fail-closed posture the RAM image boots with: the whole disk and
# every partition read-only again, including partitions the kernel has just
# re-created from the new table.
seal() {
	for info in /sys/class/block/"${1##*/}"*/dev; do
		[ -f "$info" ] || continue
		node=${info%/dev}
		"$BB" blockdev --setro "/dev/${node##*/}" || die 'cannot restore the read-only flag'
	done
	"$BB" blockdev --setro "$1" || die 'cannot restore the read-only flag'
}

case $verb in
disk)
	[ "$#" = 1 ] || die 'usage: disk PARTNAME'
	parent_disk "$(find_part "$1")"
	printf '\n'
	;;
report)
	[ "$#" = 1 ] || die 'usage: report PARTNAME'
	"$SGDISK" -p "$(parent_disk "$(find_part "$1")")"
	;;
info)
	[ "$#" = 1 ] || die 'usage: info PARTNAME'
	part=$(find_part "$1")
	number=$(cat "/sys/class/block/${part##*/}/partition")
	"$SGDISK" -i "$number" "$(parent_disk "$part")"
	;;
geometry)
	[ "$#" = 1 ] || die 'usage: geometry PARTNAME'
	disk=$(parent_disk "$(find_part "$1")")
	printf 'disk %s\nsector %s\nsectors %s\n' "$disk" "$(sector_size "$disk")" "$(sector_count "$disk")"
	;;
backup-gpt)
	# Both GPT copies of the disk: the protective MBR, primary header and
	# primary entry array at the head, the entry array and backup header at
	# the tail.  Six sectors each, as dumped by the P0 inventory.
	[ "$#" = 2 ] || die 'usage: backup-gpt PARTNAME head|tail'
	disk=$(parent_disk "$(find_part "$1")")
	sector=$(sector_size "$disk")
	case $2 in
	head) "$BB" dd if="$disk" bs="$sector" count=6 2>/dev/null | "$BB" base64 ;;
	tail) "$BB" dd if="$disk" bs="$sector" skip="$(( $(sector_count "$disk") - 6 ))" count=6 2>/dev/null | "$BB" base64 ;;
	*) die 'backup-gpt takes head or tail' ;;
	esac
	;;
restore-gpt)
	# The host has already staged the two verified copies as base64 files.
	[ "$#" = 1 ] || die 'usage: restore-gpt PARTNAME'
	disk=$(parent_disk "$(find_part "$1")")
	sector=$(sector_size "$disk")
	assert_idle "$disk"
	for half in head tail; do
		[ -f "/tmp/liuqin-gpt-$half.b64" ] || die "staged GPT copy is missing: $half"
		"$BB" base64 -d <"/tmp/liuqin-gpt-$half.b64" >"/tmp/liuqin-gpt-$half.bin"
		[ "$(stat -c %s "/tmp/liuqin-gpt-$half.bin")" = "$(( sector * 6 ))" ] ||
			die "staged GPT copy has the wrong size: $half"
	done
	"$BB" blockdev --setrw "$disk" || die 'cannot open the disk for writing'
	"$BB" dd if=/tmp/liuqin-gpt-head.bin of="$disk" bs="$sector" count=6 conv=notrunc 2>/dev/null ||
		die 'writing the primary GPT failed'
	"$BB" dd if=/tmp/liuqin-gpt-tail.bin of="$disk" bs="$sector" \
		seek="$(( $(sector_count "$disk") - 6 ))" count=6 conv=notrunc 2>/dev/null ||
		die 'writing the backup GPT failed'
	sync
	"$BB" blockdev --rereadpt "$disk" || true
	seal "$disk"
	"$SGDISK" -v "$disk" || die 'the restored partition table does not verify'
	printf 'liuqin-layout: GPT_RESTORED\n'
	;;
wipe-head)
	# Zero the head of a partition so that no stale filesystem superblock and
	# no stale file-based-encryption key survives the layout change.
	[ "$#" = 2 ] || die 'usage: wipe-head PARTNAME BYTES'
	part=$(find_part "$1")
	case $2 in ''|*[!0-9]*) die 'invalid wipe length' ;; esac
	[ "$2" -gt 0 ] && [ "$2" -le 67108864 ] || die 'wipe length is out of range'
	disk=$(parent_disk "$part")
	assert_idle "$disk"
	size=$(( $(cat "/sys/class/block/${part##*/}/size") * 512 ))
	[ "$size" -gt "$2" ] || die 'partition is smaller than the requested wipe'
	"$BB" blockdev --setrw "$disk" || die 'cannot open the disk for writing'
	"$BB" blockdev --setrw "$part" || die 'cannot open the partition for writing'
	"$BB" dd if=/dev/zero of="$part" bs=1048576 count="$(( $2 / 1048576 ))" conv=notrunc 2>/dev/null ||
		die "wiping $1 failed"
	sync
	seal "$disk"
	printf 'liuqin-layout: WIPED %s\n' "$1"
	;;
apply)
	# Every layout decision was made on the host; this runs exactly one sgdisk
	# invocation, so the table is written once and never left half-edited.
	[ "$#" -ge 2 ] || die 'usage: apply PARTNAME SGDISK-ARGUMENT...'
	name=$1
	shift
	disk=$(parent_disk "$(find_part "$name")")
	assert_idle "$disk"
	eval "target=\${$#}"
	[ "$target" = "$disk" ] ||
		die 'the sgdisk arguments do not end with the resolved disk'
	"$BB" blockdev --setrw "$disk" || die 'cannot open the disk for writing'
	if "$SGDISK" "$@"; then
		status=0
	else
		status=1
	fi
	sync
	"$BB" blockdev --rereadpt "$disk" || true
	seal "$disk"
	[ "$status" = 0 ] || die 'sgdisk refused the requested layout'
	"$SGDISK" -v "$disk" || die 'the new partition table does not verify'
	printf 'liuqin-layout: LAYOUT_APPLIED\n'
	;;
digest)
	# sha256 of the first BYTES of a partition, and a second digest of
	# everything after them, so that a stock image padded with zeros compares
	# equal to the partition that already holds it.  Whole 4096-byte blocks
	# only: busybox dd byte counts are not portable across builds.
	[ "$#" = 2 ] || die 'usage: digest PARTNAME BYTES'
	part=$(find_part "$1")
	case $2 in ''|*[!0-9]*) die 'invalid length' ;; esac
	[ "$(( $2 % 4096 ))" = 0 ] || die 'length must be a whole number of 4096-byte blocks'
	size=$(( $(cat "/sys/class/block/${part##*/}/size") * 512 ))
	[ "$size" -ge "$2" ] || die "$1 is smaller than the image"
	printf 'size %s\n' "$size"
	printf 'content %s\n' "$("$BB" dd if="$part" bs=4096 count="$(( $2 / 4096 ))" 2>/dev/null |
		"$BB" sha256sum | "$BB" cut -d' ' -f1)"
	printf 'padding %s\n' "$("$BB" dd if="$part" bs=4096 skip="$(( $2 / 4096 ))" \
		count="$(( (size - $2) / 4096 ))" 2>/dev/null | "$BB" sha256sum | "$BB" cut -d' ' -f1)"
	;;
*)
	die "unknown verb: $verb"
	;;
esac
