# Installation Steps

Initial installation and first boot have been tested on a 256 GB unit. The
128 GB and 512 GB variants are admitted by the rules in the
[installation guide](FLASHING.md) but have not been tested on real hardware.
This remains an experimental device port. Keep the tablet attended and prepare a recovery plan.

The dual-boot layout described below is implemented in the installer but has
not yet completed a device installation test. Until a release marks it as
device tested, treat it as an attended experiment.

## Installation Layouts

The installer offers two layouts. Both are laid out by the same engine, and
both place Ubuntu in slot B and leave slot A to Android.

| Layout | Android | Ubuntu system | Ubuntu home |
| --- | --- | --- | --- |
| `linux-only` | none by default | `linux_root`, 32 GiB | `linux_home`, the remainder |
| `dual` | `userdata`, 96 GiB | `linux_root`, 32 GiB | `linux_home`, the remainder |

Select one with `--layout linux-only` or `--layout dual`. Sizes are given as
`NNG` or as a percentage of the available tail of the disk:

- `--android-size` sets the size of the Android data partition. The default is
  `96G` for `dual` and `0` for `linux-only`. `0` deletes `userdata`, which
  gives the whole tail to Ubuntu. A non-zero size must be at least 16 GiB;
  `--layout linux-only --android-size 32G` keeps a small emergency Android.
- `--root-size` sets the size of `linux_root`. The default is `32G` and the
  minimum is 16 GiB.
- `linux_home` receives everything that is left and must be at least 8 GiB.

Every partition is aligned to 4 MiB. `userdata` is only ever shrunk in place:
it keeps its original type GUID, unique GUID and attribute bits, and no
existing partition is moved. A size outside the feasible range is refused
before any device access, with the feasible range printed.

The installer prints the complete layout plan — partition, start sector, size
and action — and requires the same interactive confirmation as before.

### What each layout writes

`linux-only` writes the partition table, `linux_root`, `linux_home` and
`boot_b`. It does not touch `super`, `metadata` or any slot A partition.

`dual` additionally clears the first 16 MiB of `userdata` and of `metadata`, so
that Android reformats both on first boot instead of finding stale file-based
encryption keys, and restores the stock Android boot chain in slot A from a ROM
directory you supply. Only the images whose bytes differ from what the tablet
already holds are written, and only to `_a` partition names. `super` is an
Android sparse image and cannot be compared against the partition, so it is
always written when the dual layout is selected. The installer never runs the
stock `flash_all` script and never writes a `_b` partition other than `boot_b`.

### Dual-boot prerequisites

`--layout dual` requires `--rom-dir`, pointing at an extracted stock Xiaomi
Fastboot ROM. The original ROM is an upstream input and is not redistributed
here. The installer verifies `boot.img`, `vendor_boot.img`, `dtbo.img`,
`vbmeta.img`, `vbmeta_system.img` and `super.img` against the checksums pinned
in `liuqin-rom-images.json`, and refuses any other release. Those checksums
identify the exact ROM this port was validated against; a different ROM
requires repeating that validation.

Observe the ROM's own anti-rollback rule: its version must be at least the
version already fused into the tablet.

### Replacing the Android boot image

`--layout dual` accepts `--android-boot IMG`, which writes `IMG` to `boot_a`
instead of the ROM's own `boot.img`. Every other stock image is still verified
against the pinned checksums, and the override itself is admitted only when it
is exactly as long as the stock `boot.img` and begins with the Android boot
magic. Its sha256 is printed in the plan summary and again in the erasure
confirmation, and the image is always written, whatever the partition already
holds. The option is rejected with any layout other than `dual`.

This is how a root-enabled Android is installed alongside Ubuntu.
`tools/patch-android-boot-ksu.py` produces such an image entirely on the host:
it unpacks the stock `boot.img`, renames `init` to `init.real` in the ramdisk,
installs KernelSU's `ksuinit` as `init` and the matching loadable module as
`/kernelsu.ko`, and repacks the image with the kernel, the command line and
every other header field unchanged. The KernelSU release it draws on is pinned
in `tools/lib/kernelsu-assets.json` and fetched by
`tools/fetch-kernelsu-assets.sh`.

```sh
sh tools/fetch-kernelsu-assets.sh
python3 tools/patch-android-boot-ksu.py \
  --boot /path/to/extracted-stock-rom/images/boot.img \
  --ksuinit tools/local/downloads/kernelsu/v3.3.0/ksuinit-aarch64 \
  --lkm tools/local/downloads/kernelsu/v3.3.0/lkm-aarch64-android12-5.10_kernelsu.ko \
  --out out/android-ksu/boot-ksu.img --report out/android-ksu/boot-ksu.report.txt
```

The tool refuses to write anything unless an unpatched unpack-and-repack of the
same image reproduces the stock bytes exactly, so the only difference between
its output and the stock image is the ramdisk it was asked to change.

Any change to the ramdisk invalidates the AVB boot signature and the vbmeta
hash descriptor for that partition, so the resulting image boots only on an
unlocked bootloader. Root also makes Android able to modify slot B: the
`liuqin_boot_ubuntu` KernelSU module disables the system updater on every boot
for that reason, because an Android over-the-air update rewrites the inactive
slot, which is where Ubuntu lives.

## Requirements

- Xiaomi Pad 6 Pro (liuqin) with the factory partition table, 4096-byte
  logical sectors and `userdata` as the last partition. Modified partition
  layouts are rejected.
- Unlocked bootloader and the device in Fastboot mode.
- Battery at least 30 percent charged.
- Linux host with Python 3.11 or newer, Android platform-tools and USB networking.
- Personal files backed up outside the tablet. Installation erases all userdata.
- A matching original Xiaomi Fastboot ROM and an Android recovery plan prepared
  before installation. The installer does not back up personal userdata.

Download all files from the same release. `install.py`, `liuqin_layout.py` and
`liuqin-rom-images.json` belong together; the installer refuses to run without
them. If the system archive is split, join it in the bundle directory:

```sh
if [ ! -f rootfs.tar.gz ]; then
  cat rootfs.tar.gz.part-* > rootfs.tar.gz
fi
```

Images are verified automatically before any device access. Before the
installation starts, confirm the erasure interactively by typing `YES`
(scripts and non-interactive shells pass `--yes` explicitly):

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout linux-only
```

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout dual \
  --rom-dir /path/to/extracted-stock-rom --android-size 96G --root-size 32G
```

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout dual \
  --rom-dir /path/to/extracted-stock-rom --android-boot /path/to/boot-ksu.img \
  --android-size 96G --root-size 32G
```

Use `python3 install.py --bundle . --check` for an optional local-only check;
adding `--layout` prints the planned layout without accessing a device.
Locally built or CI-generated bundles that have not passed device testing require
`--allow-unverified` for an explicitly attended test.

The installer boots `installer.img` in RAM, waits for its USB network, backs up
boot_a, boot_b, persist and both copies of the partition table, verifies those
backups, then edits the partition table, installs the rootfs and provisions this
tablet's calibration and addresses. It formats `linux_root` and `linux_home` as
ext4, writes an `/etc/fstab` entry mounting `LABEL=LIUQIN_HOME` at `/home`,
writes `boot_b` only after root installation succeeds, sets slot B active and
reboots. It does not relock the bootloader and does not write persist.
Keep the backup directory private. The USB rescue shell has no authentication:
use a direct, trusted USB connection, not a shared network.

After a partition-table edit, `sgdisk` verifies the new table and the installer
re-reads it and compares it with the plan. If anything does not match, the
installation stops and the saved table can be written back.

USB networking normally obtains an address through DHCP. `--host-address` selects
the host's USB address when automatic route selection is unsuitable. If the
installer cannot establish its control channel it stops; do not blindly retry
after a partial installation. Preserve the error output and backup first.

## Reinstalling and Changing the Split

On a tablet that already carries this split layout, the installer refuses to
resize it. Partition sizes cannot be adjusted in place, because `linux_root`
and `linux_home` would have to move and their contents cannot be preserved
through the move.

To reinstall the system on an existing split and keep `/home`, pass
`--keep-home`. It keeps the existing sizes, leaves `linux_home` untouched and
reinstalls `linux_root` only, so it may not be combined with `--android-size`
or `--root-size`.

To change the split, restore the stock partition table and install again:

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --restore-partition-table /path/to/private-backup
```

The restore refuses a backup taken from another tablet, and refuses a saved
table whose checksums do not match the manifest recorded beside it. A restored
stock table leaves the tablet without a usable system; complete it with a full
installation or with the original ROM's clean-flash procedure.

## Android in the Dual Layout

The first Android boot after a dual installation reformats `userdata` and
`metadata` and takes several minutes.

Root through KernelSU is an on-device step and is deliberately not performed by
the installer. Patch the stock `boot.img` and flash it to `boot_a` yourself, or
use the KernelSU manager's own patch-and-flash action.

**Never use "install to inactive slot" in the KernelSU manager.** The inactive
slot is slot B, which holds Ubuntu. That action overwrites the Ubuntu boot image.

**Freeze system updates.** A MIUI or HyperOS over-the-air update writes to the
inactive slot, which is the Ubuntu slot, and destroys the Ubuntu installation.

**Never run the stock `flash_all` script again.** It writes most images with the
`_ab` suffix, that is, into both slots at once, and it ends with
`fastboot set_active a`. Running it after a dual installation overwrites the
Ubuntu boot chain and returns the tablet to Android only. The same applies to
`flash_all_lock.sh` and `flash_all_except_storage.sh`.

## Desktop Diagnostics

For an attended installation test, add `--enable-rescue` to the installation
command to make the rescue shell available from the first boot. This is an
explicit opt-in to unauthenticated root access, not the default installation.

On the tablet, enable the rescue shell with:

```sh
sudo liuqin-rescue on
```

Check it with `liuqin-rescue status`. This grants unauthenticated root access
at `192.168.7.2:2323` and remains enabled across boots. Use only a trusted
connection; do not expose or forward this port to other networks. After
diagnostics, run `sudo liuqin-rescue off` on the tablet to disable it and
close existing rescue connections. Release images leave it disabled by default.

## Recovery

Returning to Android alone erases the Ubuntu installation and requires a
compatible original Fastboot ROM, including its userdata initialization.
Restoring a boot partition alone is not a complete Android recovery, and it does
not restore the partition table: use `--restore-partition-table` first if the
tablet was installed with a split layout.

Use the original ROM's full clean-flash procedure, not its keep-data or relock
variant. Preserve the anti-rollback checks. Never restore another tablet's
persist or calibration. Keep the bootloader unlocked while non-stock images
remain. The original ROM is an upstream input, not duplicated in this repository.

Android recovery still requires independent device testing. Successful Ubuntu
installation does not establish that Android recovery has been validated.
