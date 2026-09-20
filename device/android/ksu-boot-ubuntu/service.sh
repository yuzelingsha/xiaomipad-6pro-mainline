#!/system/bin/sh
# SPDX-License-Identifier: MIT
#
# Keep Android from taking an over-the-air update on a dual-boot tablet.
#
# The two systems share one disk.  An Android OTA is written through the A/B
# update engine, which rewrites the inactive slot -- slot B, where mainline
# Ubuntu lives.  Accepting one would therefore destroy the Ubuntu installation
# and leave a partition table the Android updater does not expect.  This
# script disables the system updater application for the primary user once per
# boot; KernelSU runs service.sh exactly once, in late start.
#
# Nothing here is irreversible: `pm enable com.android.updater` restores the
# application, and removing the module removes this script.
set -u

MODULE_DIR=${0%/*}
PACKAGES=${LIUQIN_OTA_PACKAGES:-com.android.updater}
BOOT_WAIT=${LIUQIN_OTA_BOOT_WAIT:-600}

log_dir=/data/adb/ksu/log
[ -d "$log_dir" ] || log_dir=$MODULE_DIR
log_file=$log_dir/liuqin-ota-freeze.log

# Keep the log bounded: this runs on every boot, forever.
if [ -f "$log_file" ] && [ "$(wc -c <"$log_file" 2>/dev/null || echo 0)" -gt 65536 ]; then
	tail -c 16384 "$log_file" >"$log_file.trim" 2>/dev/null && mv -f "$log_file.trim" "$log_file"
fi

log() {
	printf '%s liuqin-ota-freeze: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo '?')" "$*" \
		>>"$log_file" 2>/dev/null
}

log "starting (packages: $PACKAGES)"

# `pm` needs a running package manager, which is not up when service.sh runs.
waited=0
while [ "$(getprop sys.boot_completed 2>/dev/null)" != 1 ]; do
	if [ "$waited" -ge "$BOOT_WAIT" ]; then
		log "sys.boot_completed did not appear within ${BOOT_WAIT}s; giving up"
		exit 0
	fi
	sleep 5
	waited=$((waited + 5))
done
log "boot completed after ${waited}s"

for package in $PACKAGES; do
	if ! pm path --user 0 "$package" >/dev/null 2>&1; then
		log "$package is not installed for user 0; nothing to disable"
		continue
	fi
	if pm list packages -d --user 0 2>/dev/null | grep -qx "package:$package"; then
		log "$package is already disabled"
		continue
	fi
	if output=$(pm disable-user --user 0 "$package" 2>&1); then
		log "disabled $package: $output"
	else
		log "could not disable $package: $output"
	fi
done

log "finished"
