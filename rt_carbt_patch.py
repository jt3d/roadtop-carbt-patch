#!/usr/bin/env python3
"""RoadTop S7 v3.0.1 "calls via iDrive, media via AUX" firmware patcher.

Takes the stock RoadTop GEMINI_PACK.BIN
(QD505-QD517-bmw-incell-S7-LT-2026.04.28-v3.0.1) and writes a patched copy.

  Build B (default, tested in a car): leave "Use Car's BT Channel" OFF. The
    Android Auto module behaves as if the toggle were ON only where PEMP's ZLT
    firmware applies its "Use Car's BT Phone Channel" setting. The head unit
    still bootstraps wireless AA over Bluetooth, but does not take over the
    phone's Bluetooth, so calls stay on the car's own Bluetooth (iDrive) and
    media stays on AUX.
  Build A (untested): turn "Use Car's BT Channel" ON. The media paths behave
    as if the toggle were OFF (AA audio output mode 0, MCU AUX switch, mix state).

Only the spapp filesystem changes. Everything else in the image is checked to
be byte-identical to stock, and the output is fully re-validated before it is
written.

Usage:
  rt_carbt_patch.py patch STOCK_GEMINI_PACK.BIN OUT_GEMINI_PACK.BIN [--build B|A]
  rt_carbt_patch.py check GEMINI_PACK.BIN

Requires Linux (or WSL2) with Python 3.8+ and squashfs-tools built with LZO
(unsquashfs, mksquashfs). Run it on a Linux filesystem, not /mnt/c.

Never flash upgrade.bin (MCU). Keep the stock GEMINI_PACK.BIN as a recovery image.

GEMINI_PACK format handling is based on GeminiTop's firmware_tools
(MIT License, Copyright (c) 2026 Mart).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

STOCK_MD5 = "1ad653bd76be9f19bd2df3beca30baab"  # RoadTop S7 LT v3.0.1 GEMINI_PACK.BIN

AA = "lib/module/libandroidautomodule.so"
MCU = "lib/libappmcucommunication.so.1.0.0"
STOCK_FILE_MD5 = {
    AA: "d2668402ca1e3fad802edad60fcfc6dd",
    MCU: "03d2382ba95340fc8db9fac83fe1079e",
}

# (file in spapp, file offset, original bytes, new bytes, description)
# Each patch replaces the test of the useCarsBluetoothChannel value with an instruction that loads
# the wanted value into the same register (Thumb-2), so every later use of the register agrees.
PATCHES = {
    "B": [
        (AA, 0x029BCE, "7eb3", "0126", "bluetoothPairingRequestHandler: cbz r6 -> movs r6,#1 (skip BT takeover)"),
        (AA, 0x026B5C, "b8f1000f", "5ff00108", "androidAutoShow: cmp.w r8,#0 -> movs.w r8,#1 (start 2 s timer)"),
        (AA, 0x031CAA, "b9f1000f", "5ff00109", "timerEvent: cmp.w r9,#0 -> movs.w r9,#1 (car-BT branch on timer)"),
    ],
    "A": [
        (AA, 0x021150, "bbf1000f", "5ff0000b", "initAAMngr: cmp.w r11,#0 -> movs.w r11,#0 (setAudioOutPutMode(0))"),
        (MCU, 0x014152, "002f", "0027", "switchAudioChannel: cmp r7,#0 -> movs r7,#0 (send AUX switch)"),
        (MCU, 0x015B06, "bbf1000f", "5ff0000b", "setMixAudioState: cmp.w r11,#0 -> movs.w r11,#0"),
    ],
}

# Output of this script with squashfs-tools 4.7.5 (the build is deterministic).
REFERENCE_OUT_MD5 = {
    "B": "1203ce88e980b4d670aca3cd51a8535c",
    "A": "3995452c402c9aea7ee343a2ce708bb4",
}

HDR_PAYLOAD_SIZE = 0xD8
HDR_HEADER_SIZE = 0xDC
HDR_PAYLOAD_MD5 = 0xEA
TABLE_OFF = 0xC80
TABLE_REC = 0x80
MAX_COMPONENTS = 16
CHUNK = 0x200000
UIMAGE_MAGIC = 0x27051956
MD5_CHECK = re.compile(rb'if test "\$md5sum_value" = ([0-9a-f]{32}) ; then')
MKSQUASHFS_OPTS = ["-comp", "lzo", "-Xalgorithm", "lzo1x_999", "-Xcompression-level", "9",
                   "-b", "32768", "-noappend", "-all-root", "-no-exports", "-no-progress"]


class PatchError(Exception):
    pass


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- GEMINI_PACK parsing

class Pack:
    """A parsed GEMINI_PACK.BIN. Offsets in components are absolute file offsets."""

    def __init__(self, data: bytes):
        if len(data) < TABLE_OFF + TABLE_REC:
            raise PatchError("file too small to be a GEMINI_PACK.BIN")
        self.data = data
        self.payload_size = struct.unpack_from("<I", data, HDR_PAYLOAD_SIZE)[0]
        self.header_size = struct.unpack_from("<I", data, HDR_HEADER_SIZE)[0]
        if not self.header_size or self.header_size + self.payload_size > len(data):
            raise PatchError("header sizes are invalid; not a GEMINI_PACK.BIN?")
        self.payload_md5 = data[HDR_PAYLOAD_MD5:HDR_PAYLOAD_MD5 + 32].decode("ascii", "replace")
        self.components = []
        for i in range(MAX_COMPONENTS):
            rec = TABLE_OFF + i * TABLE_REC
            name = data[rec:rec + 0x20].split(b"\0", 1)[0].decode("ascii", "replace")
            if not name:
                break
            _, off, size, _ = struct.unpack_from("<IIII", data, rec + 0x40)
            self.components.append({
                "name": name, "md5_at": rec + 0x20,
                "md5": data[rec + 0x20:rec + 0x40].decode("ascii", "replace"),
                "start": self.header_size + off, "size": size,
            })
        if not self.components:
            raise PatchError("empty component table")
        self.script_start = max(c["start"] + c["size"] for c in self.components)
        self.payload_end = self.header_size + self.payload_size
        self._parse_script()

    def comp(self, name: str) -> dict:
        for c in self.components:
            if c["name"].rstrip(".") == name:
                return c
        raise PatchError(f"component {name} not found")

    def blob(self, c: dict) -> bytes:
        return self.data[c["start"]:c["start"] + c["size"]]

    def _parse_script(self) -> None:
        h = self.data[self.script_start:self.script_start + 64]
        if len(h) < 64 or struct.unpack_from(">I", h, 0)[0] != UIMAGE_MAGIC:
            raise PatchError("update script uImage not found after the last component")
        hz = bytearray(h)
        hz[4:8] = b"\0\0\0\0"
        if zlib.crc32(hz) & 0xFFFFFFFF != struct.unpack_from(">I", h, 4)[0]:
            raise PatchError("update script uImage header CRC mismatch")
        size, dcrc = struct.unpack_from(">I", h, 12)[0], struct.unpack_from(">I", h, 24)[0]
        body = self.data[self.script_start + 64:self.script_start + 64 + size]
        if len(body) != size or self.script_start + 64 + size > self.payload_end:
            raise PatchError("update script uImage truncated")
        if zlib.crc32(body) & 0xFFFFFFFF != dcrc:
            raise PatchError("update script uImage data CRC mismatch")
        self.script_body = body
        # body = be32 text length, 4 zero bytes, text
        text_len = struct.unpack_from(">I", body, 0)[0]
        self.script_text_off = 8
        self.script_text = body[8:8 + text_len]
        self.script_md5s = [m.group(1).decode() for m in MD5_CHECK.finditer(self.script_text)]

    def expected_script_md5s(self) -> list:
        out = []
        for c in self.components:
            b = self.blob(c)
            out.extend(md5(b[i:i + CHUNK]) for i in range(0, len(b), CHUNK))
        return out

    def validate(self) -> list:
        """Return a list of errors (empty if the image is internally consistent)."""
        errors = []
        if md5(self.data[self.header_size:self.payload_end]) != self.payload_md5:
            errors.append("payload MD5 mismatch")
        for c in self.components:
            if md5(self.blob(c)) != c["md5"]:
                errors.append(f"component {c['name']} MD5 mismatch")
        if self.script_md5s != self.expected_script_md5s():
            errors.append("update script per-2MB MD5 checks do not match the components")
        return errors


# ---------------------------------------------------------------- SquashFS helpers

def run(cmd: list) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise PatchError(f"command failed: {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")


def require_tools() -> None:
    for t in ("unsquashfs", "mksquashfs"):
        if not shutil.which(t):
            raise PatchError(f"{t} not found; install squashfs-tools (e.g. sudo apt install squashfs-tools)")


def sqsh_used(b: bytes) -> int:
    if b[:4] != b"hsqs":
        raise PatchError("spapp component is not SquashFS")
    return struct.unpack_from("<Q", b, 0x28)[0]


def sqsh_mkfs_time(b: bytes) -> int:
    return struct.unpack_from("<I", b, 8)[0]


def build_sqsh(src: Path, dst: Path, mkfs_time: int, slot: int) -> bytes:
    if dst.exists():
        dst.unlink()
    run(["mksquashfs", str(src), str(dst), *MKSQUASHFS_OPTS, "-mkfs-time", str(mkfs_time)])
    b = dst.read_bytes()
    if len(b) > slot:
        raise PatchError(f"rebuilt spapp is too large for its slot (0x{len(b):x} > 0x{slot:x})")
    return b + b"\0" * (slot - len(b))


def tree_diff(a: Path, b: Path) -> list:
    """Paths whose type, mode, link target or content differ between two trees."""
    def walk(root: Path) -> dict:
        out = {}
        for dirpath, dirs, files in os.walk(root):
            for n in dirs + files:
                p = Path(dirpath) / n
                st = p.lstat()
                if p.is_symlink():
                    v = ("l", os.readlink(p))
                elif p.is_dir():
                    v = ("d", st.st_mode)
                else:
                    v = ("f", st.st_mode, md5(p.read_bytes()))
                out[str(p.relative_to(root))] = v
        return out
    wa, wb = walk(a), walk(b)
    return sorted(k for k in set(wa) | set(wb) if wa.get(k) != wb.get(k))


# ---------------------------------------------------------------- commands

def cmd_check(path: Path) -> int:
    data = path.read_bytes()
    p = Pack(data)
    log(f"{path}: MD5 {md5(data)}")
    log(f"  {len(p.components)} components: " + ", ".join(c["name"].rstrip(".") for c in p.components))
    log(f"  {len(p.script_md5s)} update-script MD5 checks")
    if md5(data) == STOCK_MD5:
        log("  this is the stock RoadTop S7 v3.0.1 image")
    errors = p.validate()
    for e in errors:
        log(f"  FAIL: {e}")
    log("  RESULT: " + ("FAIL" if errors else "PASS (payload, component and script checksums all consistent)"))
    return 1 if errors else 0


def apply_patches(root: Path, build: str) -> None:
    by_file: dict = {}
    for rel, off, old, new, desc in PATCHES[build]:
        by_file.setdefault(rel, []).append((off, old, new, desc))
    for rel, items in by_file.items():
        f = root / rel
        data = bytearray(f.read_bytes())
        if md5(data) != STOCK_FILE_MD5[rel]:
            raise PatchError(f"{rel} is not the stock v3.0.1 file")
        for off, old, new, desc in items:
            have = data[off:off + len(old) // 2].hex()
            if have != old:
                raise PatchError(f"{rel}+0x{off:x}: found {have}, expected {old}")
            data[off:off + len(new) // 2] = bytes.fromhex(new)
            log(f"  {Path(rel).name}+0x{off:06x}  {old} -> {new}  {desc}")
        st = f.stat()
        with f.open("r+b") as fh:  # in place: keep mode
            fh.write(data)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # keep mtime, so only content differs


def cmd_patch(src: Path, out: Path, build: str, keep: Path | None, skip_roundtrip: bool) -> int:
    require_tools()
    data = src.read_bytes()
    if md5(data) != STOCK_MD5:
        raise PatchError(f"{src} MD5 is {md5(data)}, not the stock RoadTop S7 v3.0.1 image ({STOCK_MD5}). "
                         "The patch offsets are only valid for that exact file.")
    stock = Pack(data)
    errors = stock.validate()
    if errors:
        raise PatchError("stock image does not validate: " + "; ".join(errors))
    log(f"[1/6] stock image OK ({len(stock.components)} components)")

    sp = stock.comp("spapp")
    slot = stock.blob(sp)
    mkfs_time = sqsh_mkfs_time(slot)
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rt_carbt_", dir=keep))
    try:
        (work / "stock.sqsh").write_bytes(slot[:sqsh_used(slot)])
        run(["unsquashfs", "-q", "-d", str(work / "spapp"), str(work / "stock.sqsh")])
        log("[2/6] spapp extracted")

        if skip_roundtrip:
            log("[3/6] round-trip skipped")
        elif build_sqsh(work / "spapp", work / "rt.sqsh", mkfs_time, sp["size"]) == slot:
            log("[3/6] round-trip: repacking unmodified spapp reproduces stock exactly")
        else:
            log("[3/6] WARNING: repacking unmodified spapp does not reproduce stock byte-for-byte.\n"
                "      Your squashfs-tools differ from the reference; the result is still verified below.")

        log(f"[4/6] applying build {build}")
        apply_patches(work / "spapp", build)
        new_slot = build_sqsh(work / "spapp", work / "new.sqsh", mkfs_time, sp["size"])

        # Assemble: spapp region, its table MD5, its script MD5s, script CRCs, payload MD5.
        buf = bytearray(data)
        buf[sp["start"]:sp["start"] + sp["size"]] = new_slot
        buf[sp["md5_at"]:sp["md5_at"] + 32] = md5(new_slot).encode()
        idx = 0
        for c in stock.components:
            n = -(-c["size"] // CHUNK)
            if c is sp:
                first, count = idx, n
            idx += n
        new_md5s = list(stock.script_md5s)
        new_md5s[first:first + count] = [md5(new_slot[i:i + CHUNK]) for i in range(0, sp["size"], CHUNK)]
        it = iter(new_md5s)
        text = MD5_CHECK.sub(lambda m: m.group(0).replace(m.group(1), next(it).encode()), stock.script_text)
        body = stock.script_body[:8] + text + stock.script_body[8 + len(text):]
        assert len(body) == len(stock.script_body)
        hdr = bytearray(data[stock.script_start:stock.script_start + 64])
        struct.pack_into(">I", hdr, 24, zlib.crc32(body) & 0xFFFFFFFF)
        struct.pack_into(">I", hdr, 4, 0)
        struct.pack_into(">I", hdr, 4, zlib.crc32(hdr) & 0xFFFFFFFF)
        buf[stock.script_start:stock.script_start + 64 + len(body)] = bytes(hdr) + body
        buf[HDR_PAYLOAD_MD5:HDR_PAYLOAD_MD5 + 32] = md5(bytes(buf[stock.header_size:stock.payload_end])).encode()
        log("[5/6] image assembled")

        # Verify the result independently of how it was built.
        newp = Pack(bytes(buf))
        errors = newp.validate()
        if errors:
            raise PatchError("output does not validate: " + "; ".join(errors))
        for c in newp.components:
            if c["name"] != sp["name"] and newp.blob(c) != stock.blob(stock.comp(c["name"].rstrip("."))):
                raise PatchError(f"component {c['name']} changed unexpectedly")
        allowed = sorted([(HDR_PAYLOAD_MD5, HDR_PAYLOAD_MD5 + 32), (sp["md5_at"], sp["md5_at"] + 32),
                          (sp["start"], sp["start"] + sp["size"]),
                          (stock.script_start, stock.script_start + 64 + len(body))])
        pos = 0
        for s, e in allowed + [(len(buf), len(buf))]:
            if buf[pos:s] != data[pos:s]:
                raise PatchError(f"unexpected change outside the expected regions between 0x{pos:x} and 0x{s:x}")
            pos = max(pos, e)
        (work / "out.sqsh").write_bytes(newp.blob(newp.comp("spapp"))[:sqsh_used(new_slot)])
        run(["unsquashfs", "-q", "-d", str(work / "check"), str(work / "out.sqsh")])
        (work / "orig").mkdir()
        run(["unsquashfs", "-q", "-d", str(work / "orig" / "spapp"), str(work / "stock.sqsh")])
        changed = tree_diff(work / "orig" / "spapp", work / "check")
        expected = sorted({p[0] for p in PATCHES[build]})
        if changed != expected:
            raise PatchError(f"files changed in spapp {changed}, expected {expected}")
        for rel in expected:
            a = (work / "orig" / "spapp" / rel).read_bytes()
            b = (work / "check" / rel).read_bytes()
            diff = {i for i in range(len(a)) if a[i] != b[i]}
            want = {off + k for r, off, old, new, _ in PATCHES[build] if r == rel
                    for k in range(len(old) // 2) if old[2 * k:2 * k + 2] != new[2 * k:2 * k + 2]}
            if len(a) != len(b) or diff != want:
                raise PatchError(f"{rel}: changed bytes do not match the patch list")
        log(f"[6/6] verified: validates; only spapp changed; inside it only {', '.join(expected)} "
            f"changed, at exactly the patched bytes")

        out.write_bytes(bytes(buf))
        out_md5 = md5(bytes(buf))
        log(f"\nwrote {out}\n  MD5 {out_md5}  (build {build})")
        if out_md5 == REFERENCE_OUT_MD5[build]:
            log("  identical to the reference build")
        else:
            log("  differs from the reference build (different squashfs-tools); contents verified above")
        log("Copy it alone to the root of a FAT32 USB stick as GEMINI_PACK.BIN and use System Update.")
        log("Never flash upgrade.bin (MCU).")
        return 0
    finally:
        if keep:
            log(f"(work files kept in {work})")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("patch", help="patch a stock v3.0.1 GEMINI_PACK.BIN")
    p.add_argument("stock", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--build", choices=["A", "B"], default="B", help="B (default, tested) or A (untested)")
    p.add_argument("--keep-work", type=Path, metavar="DIR", help="keep the work directory under DIR")
    p.add_argument("--skip-roundtrip", action="store_true", help="skip the stock repack self-test (faster)")
    c = sub.add_parser("check", help="validate any GEMINI_PACK.BIN's checksums")
    c.add_argument("image", type=Path)
    a = ap.parse_args()
    try:
        if a.cmd == "check":
            return cmd_check(a.image)
        if a.out.resolve() == a.stock.resolve():
            raise PatchError("output must not overwrite the stock image")
        return cmd_patch(a.stock, a.out, a.build, a.keep_work, a.skip_roundtrip)
    except PatchError as e:
        log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
