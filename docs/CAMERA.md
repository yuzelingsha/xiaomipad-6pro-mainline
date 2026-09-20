# Experimental rear camera support

This branch enables the Xiaomi Pad 6 Pro (liuqin, SM8475) rear S5KJN1 camera.
Preview and still photos work on the tested tablet. Video can be recorded and
played with the distribution application, but motion still
stutters. The front camera is not enabled. This is downstream board bring-up,
not a claim of complete camera support or readiness for upstream Linux.

## Build and runtime integration

Use the normal [kernel build](BUILD.md) with this branch's `kernel/source.json`.
The final camera fragment is applied after the module trim and enables CAMCC,
CCI, CAMSS, S5KJN1 and the system DMA heap. The generated configuration SHA-256
is `9571ee93d6b4718ad3958ea64b748dd332553d36f8893cacc81d72c07a34ed8e`.

The device-support overlay loads CAMCC before CAMSS, grants the active local
seat/video group access to `/dev/dma_heap/system`, and starts Snapshot through
`/usr/local/bin/liuqin-camera`. Its desktop and D-Bus entries clear `GDK_DISABLE`
for Snapshot so that it can import camera DMA buffers; the existing global
display workaround remains in effect for other applications. A pre-existing
user-level Snapshot desktop or D-Bus override takes precedence over these
system entries and should be checked when reproducing activation behavior.

The native root builder installs Snapshot, libcamera's software image-processing
module and GStreamer camera/codec plugins. `gstreamer1.0-plugins-bad` provides
`h264parse`; `gstreamer1.0-libav` provides `avdec_h264`. On the test system the
recorded MP4 contained valid images while the in-app player showed black until
these missing playback components were installed. Successful playback used
`avdec_h264` (software decoding); it is not proof of hardware codec use.

## Binding validation (2026-09-20)

The selected kernel `b363e34ac41a3b63b05376e79dd85d2adc56afc6` adds CAMSS and
S5KJN1 device-tree schemas to the hardware-tested revision below; driver,
board description are unchanged. The product configuration was subsequently
regenerated with the complete cross-toolchain: only the host capability
`CONFIG_CC_CAN_LINK=y` differs from the tested image configuration. Camera
options are unchanged; user-program samples are disabled. Both schemas and
their examples passed `dt_binding_check`. The built liuqin DTB passed
targeted validation against CAMSS, S5KJN1 and CAMCC schemas. A temporary DTB
with an invalid CAMSS clock lane was rejected by the new schema. These checks
cover the camera nodes; they are not a claim of full-board schema compliance.

## Hardware validation (2026-09-20)

Latest validation uses kernel `2b3d48e094dff7a7eec838105b1e13cb3d861d27` and the verified Ubuntu Snapshot
binary described below. The clock operations are now selected only for
SM8475, and legacy CSID update-flag behavior is preserved on other platforms.
Image, board DTB and 480 matching modules built and packaged successfully.
Temporary image SHA-256:
`5fa0dc27c6eb10454e3f148faf29dc083ad60826ceaf7f72e28a21058efc892c`.

A real-scene retest on that image saved a 1920 x 1080 JPEG and a ten-second
video with distribution Snapshot. The photo and an on-device software-decoded
1920 x 1080 RGB video frame were visually inspected and showed recognizable
scene content. Three close/reopen/photo cycles passed, with both flip controls
remaining 1 and TITAN_TOP/IFE0 returning to off after every close. No sensor
enumeration was run during preview. Image softness and video stutter remain;
this test does not establish autofocus, calibrated image quality or suspend
reliability.

The following earlier results are retained with their original source/image
identities; they were not all repeated on the latest build.

- Device: Xiaomi Pad 6 Pro / liuqin / SM8475; Ubuntu 26.04 arm64.
- Tested kernel: `48bdd1304abfde79d7e4b8011a13e88425bb5ae5`, based on
  `f2d5ed65ac03b7c14b5e5d51ba4c625eea44ac30`. The final source at
  `b616824603f64a60e88c19370cb09ad097279aff` differs only by restored CSIPHY copyright
  notices; its history is consolidated into three logical commits.
- Temporary boot image SHA-256:
  `0bfcf3faaee2114e01a440028bef6670c17606152139a0066086fa878fc63511`.
  It was started with `fastboot boot`; no boot partition was flashed. This
  image used the existing tested boot wrapper with the newly built kernel.
  A new native root filesystem assembled by this branch has not been flashed.
- Full cross-build of Image, board DTB and modules passed, including the
  final locked build and packaging of 480 modules. Final temporary image
  SHA-256: `b2088363d10f763ef6b7f01967ddf4f143eef64e80cc83b344878a97a6fa7633`.
  The final image also passed temporary boot, system D-Bus activation, JPEG
  capture, a ten-second recording and on-device 1920 x 1080 RGB decoding.
  The owner reconfirmed preview direction. Earlier sustained RAW results
  below refer to the tested temporary image, separately from build success.
- Ten capture start/stop cycles passed; TITAN_TOP and IFE0 power domains
  returned to off after capture. A continuous 300-frame RAW capture at
  4080 x 3060 had a mean frame interval of 33.3298 ms and maximum 35.188 ms.
- Snapshot saved real-scene 1920 x 1080 JPEGs. The device owner confirmed
  orientation, and photos at approximately 0.5 m and 2-3 m.
- Three MP4 samples decoded without errors on the host: 472 frames / 20.064 s,
  253 / 10.398 s and 59 / 2.299667 s (about 23.5-25.7 average frames/s).
  This does not imply smooth pacing. Motion stutter remains visible.
- On-device playback produced an RGB frame through `h264parse ! decodebin`
  with `avdec_h264`; the device owner confirmed visible in-app playback.
- There was one GPU HFI timeout in the broader session. Camera causality has
  not been established; this run is not a system suspend/stability sign-off.

Personal pictures, videos, device addresses and full system logs are omitted.

## Application identity and reproduction

Tested userspace: libcamera 0.7.0-1ubuntu2, Ubuntu gnome-snapshot
50.0-0ubuntu1, GStreamer libav/ugly 1.28.2-1 and bad 1.28.2-1ubuntu1.1.
The running Snapshot executable, `/usr/bin/snapshot`, and the executable
extracted from a freshly downloaded matching Ubuntu package have identical
SHA-256 `e27cb3e60003571219c213eea2022674f2d1ea386057c81adefdd15ca0a2f634`.
`dpkg -V gnome-snapshot` reported no modified package files.

An earlier draft incorrectly attributed the final application tests to a
locally modified encoder build based on experimental source files. Those
files do not establish which executable ran. The current executable identity
is verified above; the unused experiment is not part of this integration.
Older video measurements without an executable hash should not be used to
compare encoder implementations.

Hardware encoding was disabled for the software recording test:

```sh
gsettings set org.gnome.Snapshot enable-hardware-encoding false
```

For a short smoke test on the camera kernel:

1. With Camera closed, use `cam -l` (from `libcamera-tools`) to confirm an
   internal back camera. Do not run camera enumeration while preview/capture
   is active: libcamera 0.7 sensor initialization clears both flip controls,
   which disturbed the active preview in this test. Closing and reopening
   Camera restored the configured 180-degree compensation (both flips = 1),
   and the owner reconfirmed normal direction.
2. Open Camera, take a photo, close/reopen the application, and take another.
3. Record about ten seconds in Snapshot, then open the
   saved clip in the application's gallery. Confirm visible playback and
   report stutter separately from a decode failure.
4. After closing Camera, inspect the sensor runtime status and
   `/sys/kernel/debug/pm_genpd/pm_genpd_summary` as root. The camera domains
   should return to off. Do not assume fixed `/dev/videoN` numbering;
   resolve `msm_vfe0_video0` with `media-ctl` for direct capture tests.

## Remaining limitations

- Front camera, autofocus actuator control, tuning/calibration, other sensor
  modes and long-duration or suspend/resume reliability are not validated.
- libcamera uses `uncalibrated.yaml`, lacks an S5KJN1 sensor helper/static
  properties, and warns about the active-area selection rectangle.
- Image processing and the tested recording path use the CPU. No successful
  hardware encoding path has been demonstrated. GPU utilization alone does
  not identify which image-processing or video codec path is in use.
- The new SM8450 CAMSS resources are a subset for the tested rear path. SM8475
  clock/power changes have not been tested on other SM8475 boards; the
  original SM8450 clock operations are retained.
- The kernel repository's CI reads the integration repository's `main`.
  Its old configuration digest cannot validate this new sensor Kconfig.
  The companion integration change supplies the matching fragment and lock;
  coordinate the two PRs and rerun the kernel job after it lands. Do not
  bypass the configuration digest check.

## Source provenance

The sensor driver is adapted from Vladimir Zapolskiy's/Linaro's upstream
[S5KJN1 driver, commit e38fd0933c75](https://github.com/torvalds/linux/commit/e38fd0933c759bfd51aafbee07f11e8d69da4366).
The retained reference file was byte-compared with that commit (SHA-256
`6ba9e4a5ef6aba795251a7f0e5c13dea4f6bd235d5d3f52e544eb77ab37268c5`).
Its copyright and module author remain intact. The liuqin adaptation uses
19.2 MHz clocking and the board's 4080 x 3060 RAW10 initialization/mode data,
reconstructed from the original liuqin QTech S5KJN1 CamX sensor/module files.
Those proprietary binaries are not distributed here.

CSIPHY v2.1.3 settings and Cape/waipio resource references were checked against
Qualcomm downstream camera sources, including the
[OnePlus SM8450 camera tree](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8450/tree/oneplus/sm8450_v_15.0.0_oneplus_10_pro/vendor/qcom/opensource/camera-kernel).
Imported table copyright notices are preserved. Existing CAMSS building
blocks and licensing are retained. This board adaptation and its integration
were developed with AI assistance; no third-party testing endorsement is
implied for the modified liuqin code.
