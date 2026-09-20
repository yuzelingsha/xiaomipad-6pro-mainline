# `liuqin_boot_ubuntu` — KernelSU module: Android → Ubuntu

Android-side half of the liuqin dual-boot switch. The Ubuntu-side half is
`/usr/local/bin/liuqin-boot-android`, shipped in the `liuqin-device-support`
package.

The layout is fixed and asymmetric: **Android always runs from slot A, mainline
Ubuntu always from slot B.** `super` is 8.5 GiB and one Android dynamic-partition
set is about 7.24 GiB, so a second Android cannot exist in slot B; Ubuntu is the
side that moves. This module therefore only ever selects slot 1.

## Contents

| Path | What it is |
|---|---|
| `module.prop` | KernelSU module metadata |
| `system/bin/boot-ubuntu` | terminal command (`su -c boot-ubuntu`) |
| `service.sh` | boot-time guard that disables the Android system updater |
| `webroot/index.html` | the KernelSU WebUI page: state read-out plus one button |
| `META-INF/com/google/android/*` | stub that refuses a recovery sideload |

Nothing here writes the GPT. Both entry points call the vendor `bootctl`
(the `boot_control` HAL), which owns the Qualcomm A/B attribute bits on the
Android side, exactly as `fastboot --set-active` does:

```
bootctl set-active-boot-slot 1 && sleep 1 && reboot
```

Both entry points refuse before touching anything when `bootctl get-number-slots`
is not `2` or `bootctl get-current-slot` is not `0`.

## OTA freeze

Both systems share one disk, and an Android over-the-air update is applied by
the A/B update engine, which writes the **inactive** slot. On this layout the
inactive slot is B, where mainline Ubuntu lives, so accepting an OTA would
overwrite the Ubuntu installation.

`service.sh` therefore runs once per boot — KernelSU starts it in late start —
waits for `sys.boot_completed`, and disables the system updater for the primary
user:

```
pm disable-user --user 0 com.android.updater
```

It logs every decision to `/data/adb/ksu/log/liuqin-ota-freeze.log`, falling
back to the module directory when that directory does not exist, and keeps the
log bounded. The package list can be overridden with `LIUQIN_OTA_PACKAGES`.

Nothing about this is irreversible: `pm enable com.android.updater` brings the
application back, and uninstalling the module removes the script. Updating
Android on this tablet is a deliberate operation — restore the stock partition
table first, update, then install again.

## Install

Build the zip and install it from the KernelSU manager:

```
sh tools/build-ksu-module.sh          # -> out/ksu-module/liuqin_boot_ubuntu-v0.2.zip
```

Recovery sideload is deliberately rejected — it would unpack the files outside
KernelSU's module directory.

## 待真机核实

- **WebUI API shape.** The page calls `ksu.exec(command, optionsJson, callbackName)`
  and expects `window[callbackName](errno, stdout, stderr)`. This is the widely
  documented KernelSU WebUI bridge, but there is **no local file in this repo
  that documents it**, so it was not verified against a citation and has not
  run on hardware. If the manager's bridge differs, only `exec()` in
  `webroot/index.html` needs to change.
- **KernelSU flavour.** Resolved on the host side: the stock kernel is GKI
  `5.10.209-android12-9`, KMI `android12-5.10`, and upstream KernelSU ships a
  matching loadable module for it. `tools/patch-android-boot-ksu.py` builds the
  patched Android boot image; the assets are pinned in
  `tools/lib/kernelsu-assets.json`. Whether the tablet boots that image, and
  whether the manager then reports root, is still untested.
- **Updater package name.** `com.android.updater` is the system updater on this
  ROM generation, but it has not been read off the tablet. `service.sh` logs
  `is not installed for user 0` when the name is wrong, so the log answers the
  question on the first boot.
- **`bootctl` path.** Assumed `/system/bin/bootctl`; override with `BOOTCTL=`.
- **`reboot` from the WebUI.** Whether the manager's shell context survives long
  enough for `sleep 1 && reboot` to land, or whether the callback is lost first,
  is untested. The terminal command is the fallback.
