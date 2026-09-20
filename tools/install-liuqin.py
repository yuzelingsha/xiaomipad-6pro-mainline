#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install a verified liuqin bundle from a Linux host over USB networking."""
import argparse
import base64
import functools
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid

# Slot assignment is fixed and is not a layout decision: Android keeps the
# stock boot chain in slot A, Ubuntu always installs into slot B.
UBUNTU_SLOT = 'b'
ANDROID_SLOT = 'a'
# Every layout operation resolves the disk through a partition that exists in
# every state of this installation: persist is never created, moved or removed.
ANCHOR = 'persist'
ZERO_DIGEST = hashlib.sha256(b'').hexdigest()


def load_layout():
    """Load the layout engine from the repository or from the bundle beside us."""
    here = Path(__file__).resolve().parent
    for candidate in (here / 'lib/liuqin_layout.py', here / 'liuqin_layout.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('liuqin_layout', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('liuqin_layout.py is missing next to this program'
                       ' —— 安装包不完整，请下载同一版本的全部文件')


layout = load_layout()


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def zero_digest(length):
    """sha256 of `length` zero bytes, for the padding check on stock images."""
    digest = hashlib.sha256()
    while length > 0:
        block = min(length, 1 << 20)
        digest.update(bytes(block))
        length -= block
    return digest.hexdigest()


def command(address, text, timeout=60):
    """Use exact line markers, not command echo, to delimit one shell result."""
    token = 'LIUQIN_' + uuid.uuid4().hex
    start, end = token + '_START', token + '_END'
    with socket.create_connection((address, 2323), timeout=10) as connection:
        connection.settimeout(1)
        # BusyBox telnetd announces WILL ECHO / WILL SGA / DO NAWS.
        connection.sendall(b'\xff\xfd\x01\xff\xfd\x03\xff\xfc\x1f')
        connection.sendall(("stty -echo; printf '\\n%s\\n' " + shlex.quote(start) +
                            '; sh -c ' + shlex.quote(text) +
                            "; result=$?; printf '\\n%s %s\\n' " + shlex.quote(end) +
                            ' "$result"\n').encode())
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        pattern = re.compile(rb'(?:^|\n)' + end.encode() + rb' ([0-9]+)\r?\n')
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
            # Backup output can be large; the terminator is always at the tail.
            match = pattern.search(buffer, max(0, len(buffer) - 512))
            if match:
                first = re.search(rb'(?:^|\n)' + start.encode() + rb'\r?\n', buffer)
                if not first or int(match[1]) != 0:
                    raise RuntimeError(bytes(buffer[-4096:]).decode(errors='replace'))
                return bytes(buffer[first.end():match.start()]).replace(b'\r\n', b'\n').removesuffix(b'\r')
        raise RuntimeError('RAM command timed out or connection closed; installation stopped')


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Verify local files without accessing any device')
    parser.add_argument('--serial')
    parser.add_argument('--host-address', help='Host IPv4 address on the tablet USB network')
    parser.add_argument('--device-address', default='192.168.7.2')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--erase-userdata', action='store_true')
    parser.add_argument('--layout', choices=list(layout.MODES),
                        help='linux-only: Ubuntu is the only system. '
                             'dual: keep a stock Android in slot A alongside it')
    parser.add_argument('--android-size', help='Android userdata size as NNG or NN%% '
                                               '(0 deletes the partition; default 96G dual, 0 linux-only)')
    parser.add_argument('--root-size', help='linux_root size as NNG or NN%% (default 32G); '
                                            'linux_home takes the remainder')
    parser.add_argument('--keep-home', action='store_true',
                        help='Reinstall the system on an existing split layout and keep linux_home')
    parser.add_argument('--rom-dir', type=Path,
                        help='Extracted stock Fastboot ROM directory; required by --layout dual')
    parser.add_argument('--restore-partition-table', type=Path, metavar='BACKUP_DIR',
                        help='Restore the partition table saved in an earlier backup directory and stop')
    parser.add_argument('--yes', action='store_true', help='Skip the interactive data-erasure confirmation')
    parser.add_argument('--allow-unverified', action='store_true', help='Explicitly test an offline-only bundle')
    parser.add_argument('--enable-rescue', action='store_true',
                        help='Enable unauthenticated root rescue access after installation (trusted USB only)')
    return parser, parser.parse_args(argv)


def reference_plan(parser, args):
    """The plan as it applies to an untouched factory table, for the prompt.

    The device's own table is read and re-planned before anything is written;
    this preview exists so that the confirmation names real numbers.
    """
    disk = layout.reference_stock_disk()
    region = disk.usable_end - disk.by_name(layout.USERDATA_NAME).start
    try:
        android, root = layout.resolve_sizes(args.layout, args.android_size, args.root_size, region)
        return layout.plan_layout(disk, args.layout, android, root)
    except layout.LayoutError as error:
        parser.error(str(error))


def validate_arguments(parser, args, preview=False):
    """Check the argument combinations.  ``preview`` covers the offline --check
    path, where no device and no stock ROM are involved."""
    if args.keep_home and (args.android_size or args.root_size):
        parser.error('--keep-home keeps the existing sizes; do not also request new ones'
                     ' —— 现有分区尺寸不可调整，请勿同时指定尺寸参数')
    if args.restore_partition_table is not None:
        for name, value in (('--layout', args.layout), ('--rom-dir', args.rom_dir),
                            ('--android-size', args.android_size), ('--root-size', args.root_size)):
            if value:
                parser.error('--restore-partition-table is a separate action; ' + name + ' does not apply')
        if not preview and not args.serial:
            parser.error('--restore-partition-table requires --serial')
        return
    if preview:
        return
    if not args.layout:
        parser.error('--layout linux-only|dual is required')
    if args.layout == 'dual' and not args.rom_dir:
        parser.error('--layout dual needs --rom-dir pointing at an extracted stock Fastboot ROM'
                     ' —— 双系统模式必须提供原厂 ROM 目录，本项目不分发原厂镜像')
    if args.layout != 'dual' and args.rom_dir:
        parser.error('--rom-dir only applies to --layout dual')


def verify_rom(parser, rom_dir):
    """Check every stock image this installation writes against the pinned table."""
    here = Path(__file__).resolve().parent
    pinned = next((item for item in (here / 'lib/liuqin-rom-images.json',
                                     here / 'liuqin-rom-images.json') if item.is_file()), None)
    if pinned is None:
        parser.error('liuqin-rom-images.json is missing next to this program'
                     ' —— 安装包不完整，请下载同一版本的全部文件')
    table = json.loads(pinned.read_text())
    images = rom_dir.resolve()
    if (images / 'images').is_dir():
        images = images / 'images'
    selected = {}
    for name, entry in sorted(table['images'].items()):
        path = images / name
        if not path.is_file():
            parser.error('the ROM directory does not contain ' + name +
                         ' —— 请指向解包后的原厂 Fastboot ROM 目录（含 images/）')
        if path.stat().st_size != entry['bytes'] or sha(path) != entry['sha256']:
            parser.error('ROM image does not match the pinned ' + table['rom'] + ' release: ' + name +
                         ' —— 原厂镜像与本项目验证过的版本不一致，请使用 ' + table['rom'])
        selected[name] = dict(entry, path=path)
    return table, selected


def main(argv=None):
    parser, args = parse_arguments(argv)
    bundle = args.bundle.resolve()
    manifest = json.loads((bundle / 'bundle.json').read_text())
    if manifest['device'] != 'liuqin':
        parser.error('wrong device bundle')
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        if sha(bundle / name) != manifest['files'][name]:
            parser.error('bundle checksum mismatch: ' + name + ' —— 包文件损坏或不完整，请重新下载')
    if args.check:
        validate_arguments(parser, args, preview=True)
        print('Local bundle checksums verified; no device access')
        if args.layout:
            print('Planned layout for an untouched factory partition table:')
            print(reference_plan(parser, args).table())
        return
    validate_arguments(parser, args)
    if manifest['status'] != 'DEVICE_TESTED' and not args.allow_unverified:
        parser.error('bundle has not passed device testing; use --allow-unverified only for attended tests'
                     ' —— 该包未通过真机验证，请勿用于正式安装')
    restore = args.restore_partition_table
    if restore is None and not all((args.serial, args.backup, args.erase_userdata)):
        parser.error('--serial, --backup, --erase-userdata and --layout are required')
    if restore is None:
        if args.backup.exists():
            parser.error('--backup must be a new directory')
        args.backup = args.backup.resolve()
        if args.backup == bundle or bundle in args.backup.parents:
            parser.error('private backups must be outside the served bundle directory')
        planned = None if args.keep_home else reference_plan(parser, args)
        rom_table, rom_images = ({}, {})
        if args.layout == 'dual':
            rom_table, rom_images = verify_rom(parser, args.rom_dir)
            print('Stock ROM verified against the pinned ' + rom_table['rom'] + ' checksums')
    else:
        restore = restore.resolve()
        if not (restore / 'gpt/manifest.json').is_file():
            parser.error('the backup directory has no saved partition table: ' + str(restore))
    if args.host_address:
        socket.inet_pton(socket.AF_INET, args.host_address)
    socket.inet_pton(socket.AF_INET, args.device_address)

    def fastboot(*arguments):
        result = subprocess.run(['fastboot', '-s', args.serial, *arguments], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
        return result.stdout

    for name, value in (('product', 'liuqin'), ('unlocked', 'yes')):
        if not re.search(r'\b' + name + r':\s*' + value + r'\b', fastboot('getvar', name)):
            parser.error('Fastboot device check failed: ' + name +
                         ' —— 请确认设备是小米平板 6 Pro（liuqin）且已解锁 Bootloader')

    def partition_size(name, required=True):
        match = re.search(r'partition-size:' + re.escape(name) + r':\s*(0x[0-9a-fA-F]+)',
                          fastboot('getvar', 'partition-size:' + name))
        if not match:
            if required:
                raise RuntimeError('Cannot determine partition size: ' + name)
            return None
        return int(match[1], 16)

    if restore is None:
        # The partition table is the authority once the RAM installer is up;
        # this is the cheapest identity check that runs before booting it.
        userdata_size = partition_size('userdata', required=False)
        if userdata_size is not None and userdata_size < 16 * 1024 ** 3:
            parser.error('userdata is smaller than 16 GiB; only Xiaomi Pad 6 Pro (liuqin) is supported'
                         ' —— 请确认设备为小米平板 6 Pro（liuqin），不要用于其他机型')
        # Ubuntu installs into slot B, so the project boot image must fit boot_b.
        boot_partition = 'boot_' + UBUNTU_SLOT
        if max((bundle / name).stat().st_size for name in ('boot.img', 'installer.img')) > partition_size(boot_partition):
            parser.error('boot image exceeds the reported boot partition size'
                         ' —— boot 镜像大于 boot 分区，包与设备不匹配')
        print('Layout: ' + args.layout)
        if args.keep_home:
            print('Reinstalling on the existing split layout: the partition sizes are kept as'
                  ' they are and linux_home is left untouched.')
        else:
            print(planned.table())
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error('data erasure needs an interactive confirmation; pass --yes to skip it'
                             ' —— 非交互环境请显式加 --yes 确认清空 userdata')
            print(f'About to REPARTITION tablet {args.serial} and erase every partition shown above.')
            print(f'即将修改平板 {args.serial} 的分区表并清空上表所列分区的全部数据。')
            if args.layout == 'dual':
                print('Android keeps slot A and its own userdata; Ubuntu installs into slot B.')
                print('Android 保留 A 槽与独立的 userdata；Ubuntu 安装到 B 槽。')
            if input('Type YES to continue / 输入 YES 继续: ') != 'YES':
                parser.error('data erasure was not confirmed —— 未确认，已取消')
    server = None
    try:
        print('Booting the RAM installer...', flush=True)
        fastboot('boot', str(bundle / 'installer.img'))
        deadline = time.monotonic() + 120
        while True:
            try:
                boot_id = command(args.device_address,
                                  'test "$(cat /etc/liuqin-installer)" = liuqin && cat /proc/sys/kernel/random/boot_id').decode().strip()
                uuid.UUID(boot_id)
                break
            except (OSError, RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Installer USB channel did not become ready; no formatting performed')
                time.sleep(2)

        def remote(text, timeout=60):
            guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(boot_id)
            return command(args.device_address, guard + ' && ' + text, timeout)

        def device_layout(*arguments, timeout=180):
            return remote(shlex.join(['sh', '/usr/lib/liuqin/install-layout.sh', boot_id, *arguments]),
                          timeout).decode()

        cmdline = shlex.split(remote('cat /proc/cmdline').decode())
        serial = next((item.split('=', 1)[1] for item in cmdline if item.startswith('androidboot.serialno=')), '')
        if serial != args.serial:
            raise RuntimeError('RAM device serial does not match the selected Fastboot device'
                               ' —— 序列号不一致，请检查 --serial 参数')
        release = remote('uname -r').decode().strip()
        if release != manifest['kernel_release']:
            raise RuntimeError('Installer kernel does not match this bundle'
                               ' —— 安装器内核与包不匹配，请使用同一发布包内的全部文件')
        if restore is not None:
            restore_partition_table(remote, device_layout, restore, args.serial)
            remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
            print('Stock partition table restored. The tablet is returning to Fastboot.')
            return
        if not args.host_address:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((args.device_address, 2323))
                args.host_address = route.getsockname()[0]
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(bundle))
        server = http.server.ThreadingHTTPServer((args.host_address, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args.backup.mkdir(mode=0o700, parents=True)
        backups = {}
        for name in ('boot_a', 'boot_b', 'persist'):
            print('Backing up and verifying ' + name + '...', flush=True)
            device = '/dev/disk/by-partlabel/' + name
            content = remote('test -b ' + device + ' && /bin/busybox base64 ' + device, 600)
            target = args.backup / (name + '.img')
            target.write_bytes(base64.b64decode(content, validate=False))
            target.chmod(0o600)
            expected = remote('/bin/busybox sha256sum ' + device, 120).decode().split()[0]
            if sha(target) != expected:
                raise RuntimeError('Backup verification failed: ' + name)
            backups[target.name] = expected
        (args.backup / 'SHA256SUMS').write_text(''.join(f'{h}  {n}\n' for n, h in backups.items()))
        saved = backup_partition_table(device_layout, args.backup, args.serial)
        disk = layout.parse_print(device_layout('report', ANCHOR))
        state = layout.classify(disk)
        print('Partition table state: ' + state)
        if state == 'unknown':
            raise RuntimeError(
                'this partition table is neither the factory layout nor one this installer created;'
                ' refusing to change it —— 分区表无法识别，拒绝修改。'
                ' 可用 --restore-partition-table 恢复出厂分区表')
        if state == 'stock' and args.keep_home:
            raise RuntimeError(
                'the tablet still carries the factory partition table, so there is no linux_home'
                ' to keep; install without --keep-home —— 设备仍是出厂分区表，没有可保留的 linux_home')
        if state == 'split':
            if not args.keep_home:
                raise RuntimeError(
                    'the tablet already carries a split layout; pass --keep-home to reinstall the'
                    ' system and keep linux_home. Changing the split requires restoring the stock'
                    ' partition table and installing again —— 已有分区布局不支持原地调整尺寸')
            print('Reinstalling on the existing split layout; linux_home is left untouched.')
        else:
            layout.check_stock_device(disk)
            region = disk.usable_end - disk.by_name(layout.USERDATA_NAME).start
            android, root = layout.resolve_sizes(args.layout, args.android_size, args.root_size, region)
            plan = layout.plan_layout(disk, args.layout, android, root)
            print(plan.table(), flush=True)
            confirm_plan(planned, plan)
            apply_layout(device_layout, disk, plan, saved, args.layout)
        flashes = []
        if args.layout == 'dual':
            flashes = stock_images_to_flash(device_layout, rom_images)
        url = f'http://{args.host_address}:{server.server_port}/rootfs.tar.gz'
        install = ['sh', '/usr/lib/liuqin/install-root.sh', boot_id, url,
                   manifest['files']['rootfs.tar.gz'], str((bundle / 'rootfs.tar.gz').stat().st_size),
                   'ERASE-LIUQIN-USERDATA', layout.ROOT_NAME, layout.HOME_NAME]
        if args.enable_rescue:
            install.append('ENABLE-USB-RESCUE')
        print('Installing Ubuntu into ' + layout.ROOT_NAME + '.', flush=True)
        result = remote(shlex.join(install), 3600)
        if b'liuqin-install: ROOT_INSTALLED' not in result:
            raise RuntimeError('Device did not confirm root installation')
        remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            devices = subprocess.check_output(['fastboot', 'devices'], text=True, timeout=10)
            if any(line.split()[0] == args.serial for line in devices.splitlines() if line.split()):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Return to Fastboot not observed; boot partition was not flashed')
        for name, entry in flashes:
            print(f'Writing the stock {name} to {entry["partition"]}...', flush=True)
            fastboot('flash', entry['partition'], str(entry['path']))
        print('Writing the matching boot image to boot_' + UBUNTU_SLOT + '...', flush=True)
        fastboot('flash', 'boot_' + UBUNTU_SLOT, str(bundle / 'boot.img'))
        fastboot('--set-active=' + UBUNTU_SLOT)
        fastboot('reboot')
        print('Installation commands completed. First-boot verification is still required.')
    finally:
        if server:
            server.shutdown()
            server.server_close()


def confirm_plan(expected, actual):
    """What was confirmed and what will be written must be the same decision.

    Only the sizes the operator chose are compared.  linux_home is the
    remainder by definition, so its size legitimately follows the capacity of
    the tablet in front of us rather than the preview's reference geometry.
    """
    if [item.name for item in expected.entries] != [item.name for item in actual.entries]:
        raise RuntimeError('the computed layout has a different shape from the confirmed one')
    for one, two in zip(expected.entries, actual.entries):
        if one.action != two.action or (one.name != layout.HOME_NAME and one.size != two.size):
            raise RuntimeError(
                'the tablet\'s partition table does not produce the layout that was confirmed '
                f'({one.name}: {one.action} {one.size} confirmed, {two.action} {two.size} '
                'computed); nothing was written')


def backup_partition_table(device_layout, directory, serial):
    """Save both GPT copies of the disk before any edit, with a hash manifest."""
    print('Backing up the partition table...', flush=True)
    target = directory / 'gpt'
    target.mkdir(mode=0o700)
    report = device_layout('report', ANCHOR)
    (target / 'sgdisk-p.txt').write_text(report)
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    copies = {}
    for half in ('head', 'tail'):
        data = base64.b64decode(device_layout('backup-gpt', ANCHOR, half), validate=False)
        expected = 6 * int(geometry['sector'])
        if len(data) != expected:
            raise RuntimeError(f'the {half} GPT copy is {len(data)} bytes, not {expected}')
        path = target / f'sda-{half}.bin'
        path.write_bytes(data)
        path.chmod(0o600)
        copies[path.name] = hashlib.sha256(data).hexdigest()
    saved = {'serial': serial, 'disk': geometry['disk'], 'sector_size': int(geometry['sector']),
             'sectors': int(geometry['sectors']), 'files': copies}
    (target / 'manifest.json').write_text(json.dumps(saved, indent=2) + '\n')
    print('Partition table saved to ' + str(target))
    return target


def stage_blob(remote, name, data):
    """Push a small verified blob to the RAM image in base64 chunks."""
    path = '/tmp/liuqin-gpt-' + name + '.b64'
    text = base64.b64encode(data).decode()
    remote(':>' + path)
    for index in range(0, len(text), 3000):
        remote('printf %s ' + shlex.quote(text[index:index + 3000]) + ' >>' + path)
    remote('printf "\\n" >>' + path)


def restore_partition_table(remote, device_layout, directory, serial):
    """Restore the saved stock table, refusing any dump that is not the saved one."""
    saved = json.loads((directory / 'gpt/manifest.json').read_text())
    if saved.get('serial') != serial:
        raise RuntimeError('this backup was taken from serial ' + str(saved.get('serial')) +
                           ', not ' + serial + ' —— 拒绝把其他设备的分区表写入本机')
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    if int(geometry['sectors']) != saved['sectors'] or int(geometry['sector']) != saved['sector_size']:
        raise RuntimeError('the tablet geometry differs from the saved partition table')
    for name, digest in saved['files'].items():
        path = directory / 'gpt' / name
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError('the saved partition table does not match its manifest: ' + name +
                               ' —— 备份已损坏或被改动，拒绝写入')
        stage_blob(remote, name.split('-')[1].split('.')[0], path.read_bytes())
    print('Restoring the saved partition table...', flush=True)
    if 'GPT_RESTORED' not in device_layout('restore-gpt', ANCHOR):
        raise RuntimeError('the device did not confirm the partition-table restore')


def apply_layout(device_layout, before, plan, saved, mode):
    """Wipe stale metadata, write the table once, verify it, restore on failure."""
    userdata = before.by_name(layout.USERDATA_NAME)
    info = layout.parse_info(device_layout('info', layout.USERDATA_NAME))
    if info['first_sector'] != userdata.first_sector or info['name'] != layout.USERDATA_NAME:
        raise RuntimeError('the userdata partition reports inconsistent geometry')
    # Android reformats userdata and metadata on first boot, but only if the
    # stale file-based-encryption keys are gone.  Zeroing the head of userdata
    # also removes the LIUQIN_ROOT superblock of the old whole-disk root, so no
    # second filesystem can answer to that label afterwards.
    print('Wiping stale filesystem and encryption metadata...', flush=True)
    device_layout('wipe-head', layout.USERDATA_NAME, str(layout.WIPE_BYTES), timeout=300)
    if mode == 'dual':
        device_layout('wipe-head', layout.METADATA_NAME, str(layout.WIPE_BYTES), timeout=300)
    disk_path = json.loads((saved / 'manifest.json').read_text())['disk']
    arguments = layout.sgdisk_arguments(plan, disk_path, info)
    print('Writing the new partition table...', flush=True)
    try:
        if 'LAYOUT_APPLIED' not in device_layout('apply', ANCHOR, *arguments, timeout=300):
            raise RuntimeError('the device did not confirm the partition-table edit')
        after = layout.parse_print(device_layout('report', ANCHOR))
        layout.verify_applied(after, plan)
        if plan.entry(layout.USERDATA_NAME).action != 'delete':
            layout.verify_preserved(info, layout.parse_info(device_layout('info', layout.USERDATA_NAME)))
    except (RuntimeError, layout.LayoutError) as error:
        print('Partition-table edit failed; restoring the saved table.', file=sys.stderr, flush=True)
        raise RuntimeError(str(error) + ' —— 分区表写入失败，请用 --restore-partition-table '
                           + str(saved.parent) + ' 恢复出厂分区表后重试')
    print('New partition table verified.')


def stock_images_to_flash(device_layout, images):
    """Flash only the stock images whose bytes are not already on the tablet.

    A stock image is usually shorter than its partition and the remainder is
    zero, so the comparison is the image digest over the first N bytes plus a
    zero check over everything after them.
    """
    selected = []
    for name, entry in sorted(images.items()):
        if entry.get('sparse'):
            # An Android sparse image cannot be compared against the raw
            # partition, so it is always written when the dual layout asks for it.
            print(f'{name}: sparse image, will be written to {entry["partition"]}')
            selected.append((name, entry))
            continue
        report = dict(line.split(None, 1) for line in
                      device_layout('digest', entry['partition'], str(entry['bytes']),
                                    timeout=900).splitlines() if line)
        padding = int(report['size']) - entry['bytes']
        if report['content'] != entry['sha256']:
            reason = 'content differs'
        elif report['padding'] != zero_digest(padding):
            reason = 'the bytes after the image are not zero'
        else:
            print(f'{name}: already present on {entry["partition"]}, not written')
            continue
        print(f'{name}: {reason}, will be written to {entry["partition"]}')
        selected.append((name, entry))
    return selected


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, layout.LayoutError) as error:
        sys.exit('Installation stopped: ' + str(error))
