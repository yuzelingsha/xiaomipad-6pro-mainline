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
    good.write_bytes(b'ANDROID!' + bytes(stock_boot_bytes - 8))
    short = root / 'android-boot-short.img'
    short.write_bytes(b'ANDROID!' + bytes(stock_boot_bytes - 9))
    wrong_magic = root / 'android-boot-magic.img'
    wrong_magic.write_bytes(b'NOTABOOT' + bytes(stock_boot_bytes - 8))
    check = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root), '--check']
    for extra, fragment in (
            (['--layout', 'dual', '--android-boot', str(short)], b'exactly'),
            (['--layout', 'dual', '--android-boot', str(wrong_magic)], b'Android boot magic'),
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
      'partition-sized Android boot image')

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
