#!/usr/bin/env python3
"""Host-side tests for liuqin's Qualcomm A/B slot handling.

Two things are covered:

  * the cmdline slot parser, both copies of it -- the ``slot_from_cmdline``
    function in ``initramfs/init`` and the inline copy in
    ``device/gnome-overlay/usr/local/bin/liuqin-boot-android`` -- against a
    table of accepted and rejected command lines;

  * ``device/boot/liuqin-mark-slot-successful`` against a synthetic image the
    size of the device's ``sde`` LUN, whose GPT carries ``boot_a``/``boot_b``
    at the sector ranges the tool pins, plus decoy entries that must never
    change.

The GPT is built here rather than with ``sgdisk``: this LUN has 4096-byte
logical sectors, and sgdisk derives the sector size from the block device, so
against a plain file it always writes a 512-byte-sector table (``GPT_SECTOR_SIZE``
is not honoured by gdisk 1.0.10) and realigns the partitions the tool pins.
Building it from the spec also means the CRC32s are computed by zlib, i.e. by
an implementation independent of the one in the C helper.

Attribute bits 48-55: b48-49 priority, b50 active, b51-53 retry count,
b54 successful, b55 unbootable.
"""
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

SECTOR = 4096
DISK_BYTES = 2923429888
DISK_LBAS = DISK_BYTES // SECTOR
ENTRY_COUNT = 96
ENTRY_SIZE = 128
HEADER_SIZE = 92
BOOT_A = (75014, 124165)
BOOT_B = (344107, 393258)
DECOYS = {
    24: ("vendor_boot_a", 210667, 235242, 0x00FF),
    53: ("vendor_boot_b", 479760, 504335, 0x007B),
    60: ("dtbo_a", 150000, 156143, 0x00FF),
}

PROJECT = pathlib.Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def gpt_entries(attr_a, attr_b):
    entries = bytearray(ENTRY_COUNT * ENTRY_SIZE)

    def put(index, name, first, last, attrs):
        off = index * ENTRY_SIZE
        entries[off:off + 16] = bytes(range(16))
        entries[off + 16:off + 32] = bytes((index + i) & 0xFF for i in range(16))
        struct.pack_into("<QQQ", entries, off + 32, first, last, attrs << 48)
        raw = name.encode("utf-16-le")
        entries[off + 56:off + 56 + len(raw)] = raw

    put(13, "boot_a", BOOT_A[0], BOOT_A[1], attr_a)
    put(42, "boot_b", BOOT_B[0], BOOT_B[1], attr_b)
    for index, (name, first, last, attrs) in DECOYS.items():
        put(index, name, first, last, attrs)
    return bytes(entries)


def gpt_header(my_lba, alt_lba, entries_lba, entries):
    header = bytearray(HEADER_SIZE)
    header[0:8] = b"EFI PART"
    struct.pack_into("<IIII", header, 8, 0x00010000, HEADER_SIZE, 0, 0)
    struct.pack_into("<QQQQ", header, 24, my_lba, alt_lba, 6, DISK_LBAS - 6)
    header[56:72] = bytes(range(0x40, 0x50))
    struct.pack_into("<QIII", header, 72, entries_lba, ENTRY_COUNT, ENTRY_SIZE,
                     zlib.crc32(entries))
    struct.pack_into("<I", header, 16, zlib.crc32(bytes(header)))
    return bytes(header).ljust(SECTOR, b"\0")


def make_image(path, attr_a=0x0077, attr_b=0x007A):
    entries = gpt_entries(attr_a, attr_b)
    with open(path, "wb") as image:
        image.truncate(DISK_BYTES)
        mbr = bytearray(SECTOR)
        mbr[446 + 4] = 0xEE
        struct.pack_into("<II", mbr, 446 + 8, 1, min(DISK_LBAS - 1, 0xFFFFFFFF))
        mbr[510:512] = b"\x55\xaa"
        image.seek(0)
        image.write(mbr)
        image.write(gpt_header(1, DISK_LBAS - 1, 2, entries))
        image.write(entries)
        image.seek((DISK_LBAS - 5) * SECTOR)
        image.write(entries)
        image.seek((DISK_LBAS - 1) * SECTOR)
        image.write(gpt_header(DISK_LBAS - 1, 1, DISK_LBAS - 5, entries))


def read_copy(path, header_lba):
    with open(path, "rb") as image:
        image.seek(header_lba * SECTOR)
        header = image.read(SECTOR)
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        image.seek(entries_lba * SECTOR)
        entries = image.read(ENTRY_COUNT * ENTRY_SIZE)
    return header, entries


def crcs_valid(path, header_lba):
    header, entries = read_copy(path, header_lba)
    if header[0:8] != b"EFI PART":
        return False
    if struct.unpack_from("<I", header, 88)[0] != zlib.crc32(entries):
        return False
    zeroed = bytearray(header[:HEADER_SIZE])
    struct.pack_into("<I", zeroed, 16, 0)
    return struct.unpack_from("<I", header, 16)[0] == zlib.crc32(bytes(zeroed))


def attrs(path, header_lba, index):
    _, entries = read_copy(path, header_lba)
    return struct.unpack_from("<Q", entries, index * ENTRY_SIZE + 48)[0] >> 48


def other_entries(path, header_lba):
    _, entries = read_copy(path, header_lba)
    return [entries[i * ENTRY_SIZE:(i + 1) * ENTRY_SIZE]
            for i in range(ENTRY_COUNT) if i not in (13, 42)]


def write_magic(path, magic=b"ANDROID!"):
    with open(path, "r+b") as image:
        image.seek(BOOT_A[0] * SECTOR)
        image.write(magic)


def run_tool(binary, image, *args):
    env = dict(os.environ, LIUQIN_SLOT_SUCCESS_TESTING="unsafe-mock-only")
    return subprocess.run([binary, "--test-image", str(image), *args],
                          capture_output=True, text=True, env=env)


# --------------------------------------------------------------------------
# cmdline parsing
# --------------------------------------------------------------------------
CMDLINE_CASES = [
    ("androidboot.slot_suffix=_a", "a"),
    ("console=ttyMSM0 androidboot.slot_suffix=_a root=/dev/sda35", "a"),
    ("androidboot.slot_suffix=_b", "b"),
    ("slot_suffix=_b", "b"),
    ("slot_suffix=_a androidboot.slot_suffix=_a", "a"),
    ("slot_suffix=_a androidboot.slot_suffix=_b", None),
    ("", None),
    ("console=ttyMSM0 root=/dev/sda35", None),
    ("androidboot.slot_suffix=", None),
    ("androidboot.slot_suffix=_c", None),
    ("androidboot.slot_suffix=a", None),
    ("androidboot.slot_suffix=_A", None),
    ("androidboot.slot_suffix=_a_b", None),
    ("androidboot.slot_suffix=*", None),
    ("xandroidboot.slot_suffix=_b", None),
]


def extract_function(path, name):
    lines = pathlib.Path(path).read_text().splitlines()
    for start, line in enumerate(lines):
        if line == f"{name}() {{":
            break
    else:
        raise SystemExit(f"slot-tool: {name}() not found in {path}")
    for end in range(start, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start:end + 1])
    raise SystemExit(f"slot-tool: {name}() is unterminated in {path}")


def test_init_parser(shell):
    print("initramfs/init slot_from_cmdline")
    body = extract_function(PROJECT / "initramfs/init", "slot_from_cmdline")
    for cmdline, expected in CMDLINE_CASES:
        script = body + '\nif slot_from_cmdline "$1"; then printf "ok:%s\\n" "$slot_suffix"; else printf "no:%s\\n" "$slot_suffix"; fi\n'
        out = subprocess.run([*shell, "-c", script, "sh", cmdline],
                             capture_output=True, text=True).stdout.strip()
        want = f"ok:{expected}" if expected else "no:"
        check(f"{cmdline!r} -> {want}", out == want, f"got {out!r}")


def test_boot_android_parser(shell, tmp):
    print("liuqin-boot-android slot gate")
    script = PROJECT / "device/gnome-overlay/usr/local/bin/liuqin-boot-android"
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "id").write_text("#!/bin/sh\nprintf '0\\n'\n")
    (fake / "id").chmod(0o755)
    mark = fake / "mark"
    mark.write_text("#!/bin/sh\nprintf 'mark %s\\n' \"$*\"\n")
    mark.chmod(0o755)
    cmdline_file = tmp / "cmdline"

    def invoke(cmdline, uid="0"):
        cmdline_file.write_text(cmdline + "\n")
        (fake / "id").write_text(f"#!/bin/sh\nprintf '{uid}\\n'\n")
        (fake / "id").chmod(0o755)
        env = dict(os.environ,
                   LIUQIN_ID=str(fake / "id"), LIUQIN_MARK_SLOT=str(mark),
                   LIUQIN_CMDLINE=str(cmdline_file), LIUQIN_BOOT_A=str(tmp / "absent"),
                   LIUQIN_SYSTEMCTL="/bin/true", LIUQIN_DD="/bin/true")
        return subprocess.run([*shell, str(script), "--dry-run"],
                              capture_output=True, text=True, env=env)

    run = invoke("androidboot.slot_suffix=_b", uid="1000")
    check("non-root is refused", run.returncode == 1 and "must run as root" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")
    run = invoke("androidboot.slot_suffix=_a")
    check("slot a is refused", run.returncode == 1 and "nothing to switch" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")
    run = invoke("console=ttyMSM0")
    check("missing suffix is refused", run.returncode == 1 and "no usable" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")
    run = invoke("androidboot.slot_suffix=_c")
    check("garbage suffix is refused", run.returncode == 1 and "no usable" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")
    run = invoke("androidboot.slot_suffix=_b")
    check("slot b reaches the boot_a check",
          run.returncode == 1 and "not a block device" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")
    run = invoke("androidboot.slot_suffix=_b slot_suffix=_a")
    check("disagreeing suffixes are refused",
          run.returncode == 1 and "no usable" in run.stderr,
          f"rc={run.returncode} {run.stderr!r}")


# --------------------------------------------------------------------------
# the mark tool
# --------------------------------------------------------------------------
def test_mark_tool(binary, tmp):
    print("liuqin-mark-slot-successful")
    image = tmp / "sde.img"

    # usage
    make_image(image)
    for args, why in (([], "no operation"), (["--mark"], "--mark without a slot"),
                      (["--mark", "c"], "--mark c"), (["--set-active"], "--set-active alone"),
                      (["--check", "_c"], "--check _c")):
        run = run_tool(binary, image, *args)
        check(f"usage: {why} exits 2", run.returncode == 2, f"rc={run.returncode}")

    # --set-active demands the acknowledgement
    run = run_tool(binary, image, "--set-active", "b")
    check("--set-active without --i-know is refused",
          run.returncode == 1 and "--i-know" in run.stderr, f"rc={run.returncode}")
    check("refused --set-active left the GPT alone",
          attrs(image, 1, 13) == 0x0077 and attrs(image, 1, 42) == 0x007A)

    # --check / --mark on the active slot
    make_image(image, attr_a=0x0077, attr_b=0x007A)
    run = run_tool(binary, image, "--check", "a")
    check("--check a on a successful slot A passes", run.returncode == 0,
          f"rc={run.returncode} {run.stderr!r}")
    run = run_tool(binary, image, "--check", "b")
    check("--check b on the inactive slot B is refused", run.returncode == 1)

    make_image(image, attr_a=0x0037, attr_b=0x007A)
    before = other_entries(image, 1)
    run = run_tool(binary, image, "--check", "a")
    check("--check a before marking is refused", run.returncode == 1)
    run = run_tool(binary, image, "--mark", "a")
    check("--mark a succeeds", run.returncode == 0, f"{run.stderr!r}")
    check("--mark a set successful and kept the rest",
          attrs(image, 1, 13) == 0x0077, f"{attrs(image, 1, 13):#06x}")
    check("--mark a did not touch slot B", attrs(image, 1, 42) == 0x007A)
    check("--mark a mirrored into the backup GPT", attrs(image, DISK_LBAS - 1, 13) == 0x0077)
    check("--mark a left both CRCs valid",
          crcs_valid(image, 1) and crcs_valid(image, DISK_LBAS - 1))
    check("--mark a left every other entry byte-identical",
          other_entries(image, 1) == before and other_entries(image, DISK_LBAS - 1) == before)
    run = run_tool(binary, image, "--check", "a")
    check("--check a after marking passes", run.returncode == 0)

    # --set-active a without the ANDROID! magic
    make_image(image)
    before = other_entries(image, 1)
    run = run_tool(binary, image, "--set-active", "a", "--i-know")
    check("--set-active a without ANDROID! is refused",
          run.returncode == 1 and "ANDROID!" in run.stderr, f"{run.stderr!r}")
    check("the refused --set-active a changed nothing",
          attrs(image, 1, 13) == 0x0077 and attrs(image, 1, 42) == 0x007A and
          other_entries(image, 1) == before)

    write_magic(image, b"ANDROIDX")
    run = run_tool(binary, image, "--set-active", "a", "--i-know")
    check("--set-active a with a near-miss magic is refused", run.returncode == 1)

    # --set-active a with the magic
    write_magic(image)
    run = run_tool(binary, image, "--set-active", "a", "--i-know")
    check("--set-active a with ANDROID! succeeds", run.returncode == 0, f"{run.stderr!r}")
    check("slot A became 0x3F (priority 3, active, 7 retries)",
          attrs(image, 1, 13) == 0x003F, f"{attrs(image, 1, 13):#06x}")
    check("slot B became 0x3A (priority 2, inactive, 7 retries)",
          attrs(image, 1, 42) == 0x003A, f"{attrs(image, 1, 42):#06x}")
    check("--set-active mirrored into the backup GPT",
          attrs(image, DISK_LBAS - 1, 13) == 0x003F and
          attrs(image, DISK_LBAS - 1, 42) == 0x003A)
    check("--set-active left both CRCs valid",
          crcs_valid(image, 1) and crcs_valid(image, DISK_LBAS - 1))
    check("--set-active left every other entry byte-identical",
          other_entries(image, 1) == before and other_entries(image, DISK_LBAS - 1) == before)

    # --set-active b needs no magic, and the marked-successful cycle follows
    make_image(image)
    run = run_tool(binary, image, "--set-active", "b", "--i-know")
    check("--set-active b succeeds without any magic", run.returncode == 0, f"{run.stderr!r}")
    check("slot B is active and slot A demoted",
          attrs(image, 1, 42) == 0x003F and attrs(image, 1, 13) == 0x003A)
    run = run_tool(binary, image, "--mark", "b")
    check("--mark b after --set-active b succeeds", run.returncode == 0, f"{run.stderr!r}")
    check("slot B is now successful", attrs(image, 1, 42) == 0x007F,
          f"{attrs(image, 1, 42):#06x}")
    run = run_tool(binary, image, "--check", "b")
    check("--check b passes", run.returncode == 0)
    run = run_tool(binary, image, "--check", "a")
    check("--check a now fails: slot A is no longer active", run.returncode == 1)

    # both slots active at once is not a table this tool understands
    make_image(image, attr_a=0x0077, attr_b=0x007E)
    write_magic(image)
    run = run_tool(binary, image, "--set-active", "a", "--i-know")
    check("--set-active refuses a table with two active slots", run.returncode == 1)

    # a corrupt primary CRC fails closed
    make_image(image)
    with open(image, "r+b") as handle:
        handle.seek(2 * SECTOR + 100)
        handle.write(b"\xff")
    run = run_tool(binary, image, "--check", "a")
    check("a corrupt primary entry array is refused", run.returncode == 1)
    run = run_tool(binary, image, "--mark", "a")
    check("--mark on a corrupt primary is refused", run.returncode == 1)

    # a wrong-sized image is refused
    small = tmp / "small.img"
    make_image(image)
    shutil.copyfile(image, small)
    with open(small, "r+b") as handle:
        handle.truncate(DISK_BYTES - SECTOR)
    run = run_tool(binary, small, "--check", "a")
    check("a wrong-sized image is refused", run.returncode == 1)

    # the mock path is gated on the environment variable
    run = subprocess.run([binary, "--test-image", str(image), "--check", "a"],
                         capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items()
                              if k != "LIUQIN_SLOT_SUCCESS_TESTING"})
    check("--test-image without the opt-in env is refused", run.returncode == 2,
          f"rc={run.returncode}")


def main():
    # The initramfs runs under BusyBox ash and the device script under dash;
    # prefer BusyBox so the parser is exercised by its real interpreter.
    busybox = shutil.which("busybox")
    shell = [busybox, "sh"] if busybox else [shutil.which("dash") or "/bin/sh"]
    compiler = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise SystemExit("slot-tool: a host C compiler is required")
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        binary = tmp / "liuqin-mark-slot-successful"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(PROJECT / "device/boot/liuqin-mark-slot-successful.c"),
                        "-o", str(binary)], check=True)
        test_init_parser(shell)
        test_boot_android_parser(shell, tmp)
        test_mark_tool(str(binary), tmp)
    if FAILURES:
        print(f"liuqin slot-tool tests: FAIL ({len(FAILURES)})")
        return 1
    print("liuqin slot-tool tests: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
