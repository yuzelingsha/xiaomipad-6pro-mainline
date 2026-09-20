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

## Install

Build the zip and install it from the KernelSU manager:

```
sh tools/build-ksu-module.sh          # -> out/ksu-module/liuqin_boot_ubuntu-v0.1.zip
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
- **KernelSU availability.** The stock kernel is GKI `5.10.209-android12-9`.
  Which KernelSU flavour (GKI-built, LKM, KernelSU-Next) applies to that
  generation is still unconfirmed; see the dual-boot plan, §7.3.
- **`bootctl` path.** Assumed `/system/bin/bootctl`; override with `BOOTCTL=`.
- **`reboot` from the WebUI.** Whether the manager's shell context survives long
  enough for `sleep 1 && reboot` to land, or whether the callback is lost first,
  is untested. The terminal command is the fallback.
