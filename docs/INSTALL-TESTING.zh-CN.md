# 安装步骤

[English](INSTALL-TESTING.md)

首次安装和首次启动已在 256 GB 机型完成真机验证；128 GB 与 512 GB 变体按
[安装指南](FLASHING.zh-CN.md)的规则放行但未逐一真机验证。本项目仍属于实验性设备移植，
安装时请保持有人在场，并准备恢复条件。

下文所述的双系统布局已在安装器中实现，但尚未完成真机安装验证。在发布包标记为
真机验证通过之前，应按有人在场的实验流程对待。

## 分区布局

安装器提供两种布局，由同一套布局引擎生成；两种布局都把 Ubuntu 安装到 B 槽，A 槽留给 Android。

| 布局 | Android | Ubuntu 系统 | Ubuntu 用户目录 |
| --- | --- | --- | --- |
| `linux-only` | 默认不保留 | `linux_root`，32 GiB | `linux_home`，剩余全部 |
| `dual` | `userdata`，96 GiB | `linux_root`，32 GiB | `linux_home`，剩余全部 |

使用 `--layout linux-only` 或 `--layout dual` 选择。尺寸可写作 `NNG`，
也可写作磁盘尾部可用区域的百分比：

- `--android-size` 指定 Android 数据分区大小。`dual` 默认 `96G`，`linux-only` 默认 `0`。
  取 `0` 表示删除 `userdata`，尾部空间全部归 Ubuntu；非零值不得小于 16 GiB。
  `--layout linux-only --android-size 32G` 可保留一个应急用的小 Android。
- `--root-size` 指定 `linux_root` 大小，默认 `32G`，不得小于 16 GiB。
- `linux_home` 取剩余全部空间，不得小于 8 GiB。

所有分区按 4 MiB 对齐。`userdata` 只做原地缩小，保留原有的类型 GUID、唯一 GUID
与属性位，不移动任何既有分区。超出可行范围的尺寸会在访问设备之前被拒绝，
并打印可行区间。

安装器会打印完整的布局计划（分区、起始扇区、大小、操作），并沿用原有的交互确认。

### 两种布局各自写入什么

`linux-only` 写入分区表、`linux_root`、`linux_home` 与 `boot_b`，
不触碰 `super`、`metadata` 以及任何 A 槽分区。

`dual` 另外清零 `userdata` 与 `metadata` 的前 16 MiB，使 Android 首次开机重新格式化这两个分区，
而不是读到过期的文件级加密密钥；并用用户提供的 ROM 目录恢复 A 槽的原厂 Android 启动链。
只有与设备当前内容不一致的镜像才会被写入，且只写入 `_a` 后缀的分区。
`super` 是 Android 稀疏镜像，无法与分区内容逐字节比对，因此选择 `dual` 时总会写入。
安装器不会执行原厂 `flash_all` 脚本，除 `boot_b` 外不写入任何 `_b` 分区。

### 双系统模式的前提

`--layout dual` 必须提供 `--rom-dir`，指向解包后的原厂小米 Fastboot ROM 目录。
原厂 ROM 从上游取得，不在本项目重复托管。安装器会用 `liuqin-rom-images.json` 中固定的校验值
核对 `boot.img`、`vendor_boot.img`、`dtbo.img`、`vbmeta.img`、`vbmeta_system.img` 与 `super.img`，
不匹配即拒绝。这些校验值对应本移植验证过的确切 ROM 版本；更换版本需要重新完成该验证。

同时需遵守 ROM 自身的防回滚要求：ROM 版本不得低于设备已熔断的版本。

## 准备

- Xiaomi Pad 6 Pro（liuqin），出厂分区表、4096 字节逻辑扇区，且 `userdata` 为最后一个分区；
  自定义分区布局会被拒绝。
- Bootloader 已解锁，平板进入 Fastboot，电量至少 30%。
- Linux 主机、Python 3.11 或更新版本、Android platform-tools，以及正常的 USB 网络支持。
- 个人文件已备份到平板以外；安装会清空整个 userdata，安装器不会备份个人文件。
- 已准备适配本机、满足防回滚要求的原厂 Fastboot ROM，并明确如何恢复 Android。

## 执行安装

下载同一版本的全部文件，在安装包目录执行。`install.py`、`liuqin_layout.py` 与
`liuqin-rom-images.json` 必须齐备，缺一安装器拒绝运行。若系统归档分卷提供，先合并：

```sh
if [ ! -f rootfs.tar.gz ]; then
  cat rootfs.tar.gz.part-* > rootfs.tar.gz
fi
```

安装器会在访问设备前自动校验镜像，无需重复校验。开始安装前需交互输入
`YES` 确认清空数据（脚本或无交互环境显式加 `--yes`）：

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout linux-only
```

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout dual \
  --rom-dir /path/to/extracted-stock-rom --android-size 96G --root-size 32G
```

只检查文件、不访问设备时使用 `python3 install.py --bundle . --check`；
加上 `--layout` 可在不访问设备的情况下打印布局计划。
自行构建或 CI 生成的未验收包，需要在有人在场的测试中显式添加 `--allow-unverified`。

安装器临时启动 installer.img，等待 USB 网络，备份并校验 boot_a、boot_b、persist
以及分区表的主备两份副本；随后修改分区表，下载并校验系统归档，
把 `linux_root` 与 `linux_home` 格式化为 ext4，安装系统并提取本机校准和地址，
写入把 `LABEL=LIUQIN_HOME` 挂载到 `/home` 的 `/etc/fstab` 条目。
根文件系统安装成功且卸载后，才写入 `boot_b`、将 B 槽置为活动槽并重启。
不会写入 persist，也不会重新锁定 Bootloader。备份必须放在安装包目录以外，并保持私密。

分区表写入后，`sgdisk` 会校验新表，安装器随即重新读取并与计划逐项比对；
任何一项不符即停止安装，可用备份写回原分区表。

USB 网络通常通过 DHCP 配置；必要时可用 `--host-address` 指定主机 USB 网卡地址。
安装 RAM 环境的救援 shell 没有身份认证，只能使用可信的直连 USB，不要接入共享网络。
失败后先保留报错与备份，确认已完成哪些步骤，不要直接反复重跑。

## 重装与调整切分比例

对已经是本布局的平板，安装器拒绝调整分区尺寸。尺寸无法原地修改，
因为 `linux_root` 与 `linux_home` 必须移动，移动过程中的数据无法保留。

若只重装系统并保留 `/home`，使用 `--keep-home`：沿用现有尺寸，
不触碰 `linux_home`，只重装 `linux_root`；该选项不可与 `--android-size`
或 `--root-size` 同时使用。

要更改切分比例，先恢复出厂分区表，再重新安装：

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --restore-partition-table /path/to/private-backup
```

恢复操作拒绝来自其他平板的备份，也拒绝校验值与随附清单不符的备份。
恢复出厂分区表后平板上没有可用系统，需继续执行完整安装，
或执行原厂 ROM 的完整清刷流程。

## 双系统下的 Android

双系统安装后的首次 Android 开机会重新格式化 `userdata` 与 `metadata`，需要数分钟。

KernelSU 取得 root 是设备侧步骤，安装器不代为执行。请自行对原厂 `boot.img` 打补丁
后写入 `boot_a`，或使用 KernelSU 管理器自带的打补丁并刷入功能。

**切勿使用 KernelSU 管理器的"安装到未使用的槽位"。** 未使用的槽位是 B 槽，
其中是 Ubuntu；该操作会覆盖 Ubuntu 的 boot 镜像。

**必须冻结系统更新。** MIUI / HyperOS 的 OTA 会写入未使用的槽位，即 Ubuntu 所在的槽位，
会直接破坏 Ubuntu 安装。

**切勿再次执行原厂 `flash_all` 脚本。** 该脚本绝大多数镜像使用 `_ab` 后缀，
即一次写入两个槽位，并在结尾执行 `fastboot set_active a`。
双系统建成后运行它会覆盖 Ubuntu 的启动链，使平板退回到只有 Android 的状态。
`flash_all_lock.sh` 与 `flash_all_except_storage.sh` 同理。

## 桌面诊断

有人在场的安装测试可在安装命令后添加 `--enable-rescue`，让救援通道从首次启动即可使用。
该选项会开放免认证 root 访问，仅适用于可信连接；默认安装不启用。

也可以在平板上手动开启：

```sh
sudo liuqin-rescue on
```

使用 `liuqin-rescue status` 查看状态。开启后，`192.168.7.2:2323` 提供救援访问，重启后仍然有效。
不要将此端口转发或暴露到其他网络。诊断结束后，在平板上执行 `sudo liuqin-rescue off` 关闭通道；
现有救援连接也会断开。

## 恢复 Android

仅恢复 Android 会清除 Ubuntu，需要使用匹配的原厂 Fastboot ROM 完成系统恢复和 userdata 初始化。
仅还原 boot 分区不等于恢复 Android，也不会恢复分区表；
若平板使用过分区布局安装，应先执行 `--restore-partition-table`。

使用原厂完整清刷流程，不用保留数据或重新上锁的变体；保留原厂防回滚检查。
不得恢复其他平板的 persist 或校准。在仍有非原厂镜像时保持 Bootloader 解锁。
原厂 ROM 从上游取得，不在本项目重复托管。

Android 恢复路线仍待独立真机验证，不应将 Ubuntu 安装通过等同于恢复已验证。
