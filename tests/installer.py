#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests: no Fastboot, tablet connection or block-device writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', project / 'tools/install-liuqin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def boot_header(kind, fields=None):
    """A 4096-byte boot image header page shaped like the real ones.

    'ubuntu' is the project's v2 image (page 4096, header 1660, a DTB);
    'android' the stock GKI v4 image (header 1584).  ``fields`` overrides
    individual u32 fields by offset, e.g. ``{1648: 0}`` for "no DTB".
    """
    page = bytearray(4096)
    page[:8] = b'ANDROID!'
    values = {8: 1000}
    if kind == 'ubuntu':
        values.update({36: 4096, 40: 2, 1644: 1660, 1648: 5000})
    elif kind == 'android':
        values.update({8: 46000, 20: 1584, 40: 4})
    values.update(fields or {})
    for offset, value in values.items():
        page[offset:offset + 4] = value.to_bytes(4, 'little')
    return bytes(page)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files}))
    args = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root), '--check']
    subprocess.run(args, check=True)
    subprocess.run(args + ['--enable-rescue'], check=True)
    (root / 'boot.img').write_bytes(b'corrupted')
    assert subprocess.run(args, capture_output=True).returncode != 0
    (root / 'boot.img').write_bytes(b'boot.img')
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                               'status': 'OFFLINE_ASSEMBLED'}))
    class StopFlow(Exception):
        """Not an OSError/RuntimeError, so the RAM-channel retry loop lets it through."""

    def run_install(argv_extra, reported, stdin_tty=False, answer=None):
        calls = []

        def fastboot(command, **kwargs):
            calls.append(command)
            assert command[:3] == ['fastboot', '-s', 'TEST_SERIAL']
            if command[3] == 'boot':
                return SimpleNamespace(stdout='booting\n')
            name = command[-1]
            values = {'product': 'liuqin', 'unlocked': 'yes', 'current-slot': 'a',
                      'partition-size:userdata': reported, 'partition-size:boot_a': '0x10000000',
                      'partition-size:boot_b': '0x10000000'}
            return SimpleNamespace(stdout=name + ': ' + values[name] + '\n')

        stdin = SimpleNamespace(isatty=lambda: stdin_tty)
        with patch.object(installer.sys, 'argv', ['install.py', '--bundle', str(root),
                          '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
                          '--erase-userdata', '--allow-unverified', '--layout', 'linux-only',
                          *argv_extra]), \
             patch.object(installer.subprocess, 'run', side_effect=fastboot), \
             patch.object(installer.sys, 'stdin', stdin), \
             patch('builtins.input', lambda *a: answer), \
             patch.object(installer, 'command', side_effect=StopFlow('stop after boot')):
            try:
                installer.main()
            except (SystemExit, RuntimeError, StopFlow):
                pass
            else:
                raise AssertionError('installation unexpectedly completed: ' + reported)
        return calls

    for reported in ('0x100000', hex(8 * 1024**3)):
        calls = run_install([], reported)
        assert calls[-1][-1] == 'partition-size:userdata', (reported, calls)
    print('PASS: undersized userdata layouts are rejected before RAM boot')

    # A tablet whose userdata has already been replaced by a Linux-only split
    # reports no userdata size at all.  That case is decided by the partition
    # table itself, read in the RAM installer, not by this pre-boot probe.
    calls = run_install(['--yes'], 'unknown')
    assert calls[-1][3] == 'boot', calls
    print('PASS: a tablet without userdata is referred to the on-device table check')

    for reported in (hex(16 * 1024**3), hex(471789528 * 512)):
        calls = run_install(['--yes'], reported)
        assert calls[-1][3] == 'boot', (reported, calls)
    print('PASS: userdata layouts of 16 GiB and above are admitted')

    calls = run_install([], hex(471789528 * 512), stdin_tty=True, answer='no')
    assert not any(call[3:4] == ['boot'] for call in calls)
    calls = run_install([], hex(471789528 * 512), stdin_tty=True, answer='YES')
    assert calls[-1][3] == 'boot'
    print('PASS: interactive erasure confirmation gates the RAM installer boot')

    # Both boot partitions are size-checked before the dual layout boots the
    # RAM installer, because both receive the project image.
    calls = run_install(['--yes'], hex(16 * 1024**3))
    assert ['partition-size:boot_b'] == [c[-1] for c in calls if c[-1].startswith('partition-size:boot')], calls
    print('PASS: the Linux-only layout checks boot_b only')

# A local fake shell supplies a CRLF transcript containing the echoed command.
# Only complete marker lines may finish the transaction, not the echo itself.
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
address = listener.getsockname()

def shell():
    with listener.accept()[0] as connection:
        received = b''
        while not received.endswith(b'\n'):
            received += connection.recv(4096)
        token = re.search(rb'LIUQIN_[a-f0-9]+', received)[0]
        connection.sendall(received.replace(b'\n', b'\r\n'))
        connection.sendall(b'\r\n' + token + b'_START\r\nvalue\r\n' + token + b'_END 0\r\n')

thread = threading.Thread(target=shell)
thread.start()
connect = socket.create_connection
with patch.object(installer.socket, 'create_connection', side_effect=lambda *a, **k: connect(address, **k)):
    assert installer.command('unused', 'printf value') == b'value'
thread.join()
listener.close()
assert subprocess.run(['sh', str(project / 'tools/lib/install-root.sh')], capture_output=True).returncode != 0
invalid = subprocess.run(['sh', str(project / 'tools/lib/install-root.sh'),
                          'unused', 'unused', 'unused', 'unused', 'ERASE-LIUQIN-USERDATA',
                          'linux_root', 'linux_home', 'INVALID'], capture_output=True)
assert invalid.returncode != 0 and b'unsupported rescue option' in invalid.stderr
unauthorized = subprocess.run(['sh', str(project / 'tools/lib/install-root.sh'),
                               'unused', 'unused', 'unused', 'unused', 'NO',
                               'linux_root', 'linux_home'], capture_output=True)
assert unauthorized.returncode != 0 and b'data-erasure acknowledgement' in unauthorized.stderr
for arguments in ([], ['id', 'report'], ['id', 'nonsense', 'x']):
    refused = subprocess.run(['sh', str(project / 'tools/lib/install-layout.sh'), *arguments],
                             capture_output=True)
    assert refused.returncode != 0, arguments
print('PASS: checksum rejection, local-only check, CRLF command framing and missing-authorization refusal')

# Argument combinations that must never reach a device.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                                  'status': 'OFFLINE_ASSEMBLED'}))
    base = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root),
            '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
            '--erase-userdata', '--allow-unverified', '--yes']
    for extra, fragment in (
            ([], b'--layout'),
            (['--layout', 'dual'], b'--rom-dir'),
            (['--layout', 'linux-only', '--rom-dir', str(root)], b'--rom-dir only applies'),
            (['--layout', 'linux-only', '--restore-partition-table', str(root)], b'separate action')):
        refused = subprocess.run(base + extra, capture_output=True)
        assert refused.returncode != 0 and fragment in refused.stderr, (extra, refused.stderr)
print('PASS: layout, ROM and restore argument combinations are checked before any device access')

# The Android boot override replaces a checksum-pinned stock image, so its own
# admission rules are checked offline, before any device is contacted.
pinned = json.loads((project / 'tools/lib/liuqin-rom-images.json').read_text())
stock_boot_bytes = pinned['images']['boot.img']['bytes']
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                                  'status': 'OFFLINE_ASSEMBLED'}))
    good = root / 'android-boot-good.img'
    good.write_bytes(boot_header('android') + bytes(stock_boot_bytes - 4096))
    short = root / 'android-boot-short.img'
    short.write_bytes(boot_header('android') + bytes(stock_boot_bytes - 4097))
    wrong_magic = root / 'android-boot-magic.img'
    wrong_magic.write_bytes(b'NOTABOOT' + boot_header('android')[8:] + bytes(stock_boot_bytes - 4096))
    # A v2 image, ours, is exactly what the Android slot must never be handed.
    ours = root / 'android-boot-v2.img'
    ours.write_bytes(boot_header('ubuntu') + bytes(stock_boot_bytes - 4096))
    bare = root / 'android-boot-bare.img'
    bare.write_bytes(b'ANDROID!' + bytes(stock_boot_bytes - 8))
    check = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root), '--check']
    for extra, fragment in (
            (['--layout', 'dual', '--android-boot', str(short)], b'exactly'),
            (['--layout', 'dual', '--android-boot', str(wrong_magic)], b'Android boot magic'),
            (['--layout', 'dual', '--android-boot', str(ours)], b'header version 3 or 4'),
            (['--layout', 'dual', '--android-boot', str(bare)], b'header version 3 or 4'),
            (['--layout', 'dual', '--android-boot', str(root / 'absent.img')], b'not a file'),
            (['--layout', 'linux-only', '--android-boot', str(good)], b'only applies to --layout dual'),
            (['--android-boot', str(good)], b'only applies to --layout dual')):
        refused = subprocess.run(check + extra, capture_output=True)
        assert refused.returncode != 0 and fragment in refused.stderr, (extra, refused.stderr)
    accepted = subprocess.run(check + ['--layout', 'dual', '--android-boot', str(good)],
                              capture_output=True)
    assert accepted.returncode == 0, accepted.stderr
    restore = subprocess.run(check + ['--restore-partition-table', str(root),
                                      '--android-boot', str(good)], capture_output=True)
    assert restore.returncode != 0 and b'separate action' in restore.stderr, restore.stderr
print('PASS: the Android boot override is refused unless it is a dual-layout, '
      'partition-sized v3/v4 Android boot image')

# The override must replace the stock boot.img only after every other stock
# image has been verified, and must always be written.
with tempfile.TemporaryDirectory() as directory:
    rom = Path(directory) / 'images'
    rom.mkdir(parents=True)
    for name, entry in pinned['images'].items():
        (rom / name).write_bytes(b'')
    parser, args = installer.parse_arguments(
        ['--bundle', str(rom), '--layout', 'dual', '--rom-dir', str(rom.parent)])
    refused = []
    with patch.object(parser, 'error', side_effect=lambda message: refused.append(message)
                      or (_ for _ in ()).throw(SystemExit(2))):
        try:
            installer.verify_rom(parser, rom.parent)
        except SystemExit:
            pass
    assert refused and 'does not match the pinned' in refused[0], refused
print('PASS: a ROM directory whose images do not match the pinned release is refused')

# The installer RAM image has no /tmp, so everything the host writes on the
# tablet goes to the scratch directory the device script reads from -- and the
# host creates that directory before writing into it, because the first
# command it sends may well be this one.
sent = []
installer.stage_blob(lambda text, timeout=60: sent.append(text), 'head', b'liuqin')
assert sent[0] == 'mkdir -p ' + installer.LAYOUT_WORK, sent
staged = installer.LAYOUT_WORK + '/liuqin-gpt-head.b64'
assert sent[1] == ':>' + staged, sent
assert all('/tmp' not in text for text in sent), sent
assert sent[2] == 'printf %s bGl1cWlu >>' + staged, sent
assert sent[3] == 'printf "\\n" >>' + staged, sent
assert installer.LAYOUT_WORK == '/run/liuqin-layout'
# The device script must read the halves back from that same directory.
layout_script = (project / 'tools/lib/install-layout.sh').read_text()
assert 'LIUQIN_LAYOUT_WORK:-' + installer.LAYOUT_WORK in layout_script
print('PASS: the staged partition-table halves land in the scratch directory the RAM image has')

# --- slot assignment and the fastboot tail ---------------------------------
# The dual layout boots both systems from slot A: the project image goes to
# boot_a and, as the bootloader's fallback, boot_b; slot A is selected through
# fastboot itself.  The stock boot.img is never flashed there -- it is stored.
assert installer.boot_slots('dual') == ('a', 'b')
assert installer.boot_slots('linux-only') == ('b',)
for mode, expected in (
        ('dual', [['flash', 'vendor_boot_a', '/rom/vendor_boot.img'],
                  ['flash', 'boot_a', '/bundle/boot.img'],
                  ['flash', 'boot_b', '/bundle/boot.img'],
                  ['--set-active=a'], ['reboot']]),
        ('linux-only', [['flash', 'boot_b', '/bundle/boot.img'],
                        ['--set-active=b'], ['reboot']])):
    sent = []
    flashes = [('vendor_boot.img', {'partition': 'vendor_boot_a', 'path': '/rom/vendor_boot.img'})] \
        if mode == 'dual' else []
    installer.finish_in_fastboot(lambda *a: sent.append(list(a)), mode, flashes, Path('/bundle/boot.img'))
    assert sent == expected, (mode, sent)
    assert not any(call[0].startswith('--set-active') and call != expected[-2] for call in sent), sent
print('PASS: dual writes the project image to boot_a and boot_b and selects slot A; '
      'linux-only keeps slot B')

installer_source = (project / 'tools/install-liuqin.py').read_text()
assert "'--set-active=b'" not in installer_source and '--set-active=b' not in installer_source
assert 'stock_images_to_flash(device_layout, {' in installer_source
assert 'if name != ANDROID_BOOT_IMAGE})' in installer_source
print('PASS: the stock boot.img is stored for liuqin-switch, not flashed')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    ubuntu = root / 'boot.img'
    ubuntu.write_bytes(boot_header('ubuntu') + bytes(4096))
    android = root / 'android.img'
    android.write_bytes(boot_header('android') + bytes(8192))
    arguments = installer.switch_store_arguments(
        'http://192.168.7.1:8000', 'a' * 64, ubuntu, {'sha256': 'b' * 64, 'path': android})
    assert arguments == ['SWITCH-STORE',
                         'ubuntu', 'http://192.168.7.1:8000/boot.img', 'a' * 64, '8192',
                         'android', 'http://192.168.7.1:8000/android-boot.img', 'b' * 64, '12288'], arguments
    # install-root.sh accepts exactly this shape, validates it before touching
    # anything, and refuses a malformed one.
    base = ['sh', str(project / 'tools/lib/install-root.sh'), 'not-this-boot', 'u', 's', '1',
            'ERASE-LIUQIN-USERDATA', 'linux_root', 'linux_home']
    for extra, fragment in (
            (arguments, b'RAM boot identity changed'),
            (['ENABLE-USB-RESCUE'] + arguments, b'RAM boot identity changed'),
            (['NOT-A-STORE'] + arguments[1:], b'unsupported option'),
            (arguments[:3] + ['nothex'] + arguments[4:], b'invalid ubuntu sha256'),
            (arguments[:5] + ['ubuntu'] + arguments[6:], b'expected the android image'),
            (arguments[:8] + ['201326593'], b'does not fit'),
            (arguments[:2] + ['ftp://x/boot.img'] + arguments[3:], b'not http'),
            (arguments[:-1], b'usage')):
        result = subprocess.run(base + extra, capture_output=True)
        assert result.returncode != 0 and fragment in result.stderr, (extra, result.stderr)
print('PASS: the switch-store arguments are built for and validated by install-root.sh')

# Serve the bundle plus exactly the one extra file; nothing else outside.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    bundle = root / 'bundle'
    bundle.mkdir()
    (bundle / 'boot.img').write_bytes(b'project')
    outside = root / 'rom-boot.img'
    outside.write_bytes(b'android')
    (root / 'secret').write_bytes(b'secret')
    server = installer.http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), installer.bundle_handler(bundle, {'/android-boot.img': outside}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import urllib.request
    import urllib.error
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        assert urllib.request.urlopen(url + '/boot.img').read() == b'project'
        assert urllib.request.urlopen(url + '/android-boot.img').read() == b'android'
        assert urllib.request.urlopen(url + '/android-boot.img?x=1').read() == b'android'
        for path in ('/../secret', '/%2e%2e/secret', '/rom-boot.img'):
            try:
                urllib.request.urlopen(url + path).read()
            except urllib.error.HTTPError as error:
                assert error.code == 404, (path, error.code)
            else:
                raise AssertionError('served a file outside the bundle: ' + path)
    finally:
        server.shutdown()
        server.server_close()
print('PASS: the installer serves the Android boot image and nothing else outside the bundle')

# --- one header rule, three implementations --------------------------------
# liuqin-switch and the store step share the shell text; the installer's
# Python copy must agree with it on every shape.
def shell_function(path, name):
    lines = Path(path).read_text().splitlines()
    start = lines.index(name + '() { # <image or device>' if name == 'classify' else name + '() { # <file> <offset>')
    end = lines.index('}', start)
    return '\n'.join(lines[start:end + 1])


switch_script = project / 'device/gnome-overlay/usr/local/sbin/liuqin-switch'
store_script = project / 'tools/lib/install-switch-store.sh'
for name in ('u32_at', 'classify'):
    assert shell_function(switch_script, name) == shell_function(store_script, name), name
classifier = shell_function(switch_script, 'u32_at') + '\n' + shell_function(switch_script, 'classify') + \
    '\nclassify "$1"\n'
shapes = {
    'ubuntu': boot_header('ubuntu'),
    'android v4': boot_header('android'),
    'android v3': boot_header('android', {20: 1580, 40: 3}),
    'v3 with a v4 header size': boot_header('android', {40: 3}),
    'v2 without a DTB': boot_header('ubuntu', {1648: 0}),
    'v2 with a 2048-byte page': boot_header('ubuntu', {36: 2048}),
    'v2 with a v1 header size': boot_header('ubuntu', {1644: 1648}),
    'v4 without a kernel': boot_header('android', {8: 0}),
    'v1': boot_header('ubuntu', {40: 1}),
    'v5': boot_header('android', {40: 5}),
    'no magic': b'NOTABOOT' + boot_header('android')[8:],
    'erased': bytes(4096),
}
with tempfile.TemporaryDirectory() as directory:
    for label, header in shapes.items():
        image = Path(directory) / 'image'
        image.write_bytes(header + bytes(4096))
        want = installer.boot_image_kind(header)
        for shell in (['sh'], ['busybox', 'sh']):
            got = subprocess.run([*shell, '-c', classifier, 'sh', str(image)],
                                 capture_output=True, text=True).stdout.strip()
            assert got == want, (label, shell, got, want)
    assert installer.boot_image_kind(shapes['ubuntu']) == 'ubuntu'
    assert installer.boot_image_kind(shapes['android v3']) == 'android'
    assert installer.boot_image_kind(shapes['android v4']) == 'android'
print('PASS: the Python and shell header rules agree on ' + str(len(shapes)) + ' header shapes')
