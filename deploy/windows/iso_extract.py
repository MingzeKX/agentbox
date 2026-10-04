#!/usr/bin/env python3
"""Extract the installer kernel and initramfs from a Debian ISO (stdlib only).

Why this exists: QEMU only honours ``-append`` when you boot with ``-kernel``.
Booting the ISO with ``-cdrom`` means isolinux owns the kernel command line, so a
fully automatic preseed install is impossible that way.  Extracting
``install.amd/vmlinuz`` and ``install.amd/initrd.gz`` and booting them directly is
the reliable route, and doing it in pure Python avoids needing an elevated
``Mount-DiskImage``, 7-Zip or any other external tool.

Usage:
    python iso_extract.py <debian.iso> <outdir>

Writes ``<outdir>/vmlinuz`` and ``<outdir>/initrd.gz`` (or ``initrd.img``) and
prints what it found.  A minimal ISO9660 reader is used: the primary volume
descriptor, the directory records, and the standard ``;1`` version suffixes --
Rock Ridge / Joliet are not needed because we match names case-insensitively.
"""

from __future__ import annotations

import sys
from pathlib import Path

SECTOR = 2048
PVD_SECTOR = 16
MAX_DEPTH = 6

#: candidate file names (lowercase, no version suffix) we are looking for
KERNEL_NAMES = ("vmlinuz", "linux")
INITRD_NAMES = ("initrd.gz", "initrd.img", "initrd.lz", "initrd")

#: directories that most likely hold the installer payload, tried first
PREFERRED_DIRS = ("install.amd", "install", "boot", "isolinux", "casper")


class IsoError(RuntimeError):
    pass


class IsoReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = path.open("rb")
        self.size = path.stat().st_size
        self._root: tuple[int, int] | None = None

    # ------------------------------------------------------------------ basic
    def close(self) -> None:
        self.fh.close()

    def __enter__(self) -> IsoReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sector(self, lba: int, count: int = 1) -> bytes:
        self.fh.seek(lba * SECTOR)
        data = self.fh.read(SECTOR * count)
        if len(data) < SECTOR:
            raise IsoError(f"short read at sector {lba}")
        return data

    def read_extent(self, lba: int, length: int) -> bytes:
        self.fh.seek(lba * SECTOR)
        return self.fh.read(length)

    # ------------------------------------------------------------- structure
    def volume_descriptors(self) -> list[tuple[str, bytes]]:
        found: list[tuple[str, bytes]] = []
        lba = PVD_SECTOR
        while lba < PVD_SECTOR + 32:
            data = self.sector(lba)
            if data[1:6] != b"CD001":
                break
            kind = data[0]
            if kind == 255:
                break
            if kind in (1, 2):
                found.append(("primary" if kind == 1 else "supplementary", data))
            lba += 1
        if not found:
            raise IsoError("no ISO9660 volume descriptor found (is this really an ISO image?)")
        return found

    def root_record(self) -> tuple[int, int]:
        if self._root is not None:
            return self._root
        descriptors = self.volume_descriptors()
        primary = next((data for kind, data in descriptors if kind == "primary"), None)
        if primary is None:
            raise IsoError("no primary volume descriptor")
        record = primary[156:190]
        if len(record) < 34:
            raise IsoError("primary volume descriptor has no root directory record")
        self._root = (int.from_bytes(record[2:6], "little"), int.from_bytes(record[10:14], "little"))
        return self._root

    def list_dir(self, lba: int, length: int) -> list[tuple[str, bool, int, int]]:
        """Return ``[(name, is_dir, extent_lba, data_length), ...]`` for one directory."""
        raw = self.read_extent(lba, length)
        entries: list[tuple[str, bool, int, int]] = []
        offset = 0
        while offset < len(raw):
            record_len = raw[offset]
            if record_len == 0:
                # move to the next sector boundary
                offset = (offset // SECTOR + 1) * SECTOR
                continue
            record = raw[offset : offset + record_len]
            offset += record_len
            if len(record) < 34:
                continue
            extent = int.from_bytes(record[2:6], "little")
            data_len = int.from_bytes(record[10:14], "little")
            flags = record[25]
            name_len = record[32]
            name_bytes = record[33 : 33 + name_len]
            if name_len == 1 and name_bytes in (b"\x00", b"\x01"):
                continue  # "." and ".."
            name = name_bytes.decode("utf-8", errors="replace")
            if ";" in name:
                name = name.split(";", 1)[0]
            name = name.rstrip(".") or name
            entries.append((name, bool(flags & 0x02), extent, data_len))
        return entries

    def walk(self, lba: int, length: int, prefix: str = "", depth: int = 0):
        for name, is_dir, extent, data_len in self.list_dir(lba, length):
            path = f"{prefix}/{name}" if prefix else name
            if is_dir:
                if depth < MAX_DEPTH:
                    yield from self.walk(extent, data_len, path, depth + 1)
            else:
                yield path, extent, data_len


def _score(path: str) -> int:
    """Prefer the plain installer payload over gtk/speech variants."""
    lowered = path.lower()
    score = 0
    if any(part in lowered for part in PREFERRED_DIRS):
        score -= 10
    for noise in ("gtk", "speech", "xen", "virtual", "gtk-"):
        if noise in lowered:
            score += 20
    score += len(lowered.split("/"))
    return score


def find_payload(iso: Path) -> tuple[tuple[str, int, int], tuple[str, int, int]]:
    with IsoReader(iso) as reader:
        root_lba, root_len = reader.root_record()
        entries = list(reader.walk(root_lba, root_len))

        def pick(names: tuple[str, ...], exclude: tuple[str, ...] = ()) -> tuple[str, int, int] | None:
            candidates = [
                entry
                for entry in entries
                if entry[0].split("/")[-1].lower() in names
                and not any(token in entry[0].lower() for token in exclude)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda entry: (_score(entry[0]), entry[0]))
            return candidates[0]

        kernel = pick(KERNEL_NAMES, exclude=("gtk", "speech"))
        initrd = pick(INITRD_NAMES, exclude=("gtk", "speech"))
        if kernel is None or initrd is None:
            listing = "\n  ".join(sorted(path for path, _, _ in entries)[:40])
            raise IsoError(
                "could not find the installer kernel/initramfs inside the ISO.\n"
                f"first entries seen:\n  {listing}"
            )
        kernel_data = reader.read_extent(kernel[1], kernel[2])
        initrd_data = reader.read_extent(initrd[1], initrd[2])
    return _write(kernel, kernel_data), _write(initrd, initrd_data)


def _write(entry: tuple[str, int, int], data: bytes) -> tuple[str, int, int]:
    return entry[0], entry[1], len(data)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    iso = Path(argv[1])
    outdir = Path(argv[2])
    if not iso.is_file():
        print(f"iso not found: {iso}", file=sys.stderr)
        return 1
    outdir.mkdir(parents=True, exist_ok=True)

    with IsoReader(iso) as reader:
        root_lba, root_len = reader.root_record()
        entries = list(reader.walk(root_lba, root_len))

        def pick(names: tuple[str, ...]) -> tuple[str, int, int]:
            candidates = [
                entry
                for entry in entries
                if entry[0].split("/")[-1].lower() in names
                and not any(token in entry[0].lower() for token in ("gtk", "speech"))
            ]
            if not candidates:
                raise IsoError(f"none of {names} found inside {iso.name}")
            candidates.sort(key=lambda entry: (_score(entry[0]), entry[0]))
            return candidates[0]

        kernel = pick(KERNEL_NAMES)
        initrd = pick(INITRD_NAMES)
        print(f"iso          : {iso} ({iso.stat().st_size / 1048576:.0f} MiB)")
        print(f"kernel entry : /{kernel[0]}  (extent LBA {kernel[1]}, {kernel[2]} bytes)")
        print(f"initrd entry : /{initrd[0]}  (extent LBA {initrd[1]}, {initrd[2]} bytes)")

        kernel_bytes = reader.read_extent(kernel[1], kernel[2])
        initrd_bytes = reader.read_extent(initrd[1], initrd[2])

    kernel_out = outdir / "vmlinuz"
    initrd_out = outdir / ("initrd.gz" if initrd[0].lower().endswith(".gz") else "initrd.img")
    kernel_out.write_bytes(kernel_bytes)
    initrd_out.write_bytes(initrd_bytes)

    problems: list[str] = []
    if not kernel_bytes[:2] == b"MZ":
        problems.append(f"kernel does not look like a bzImage (first bytes: {kernel_bytes[:4]!r})")
    if not initrd_bytes[:2] == b"\x1f\x8b":
        problems.append(f"initramfs is not gzip compressed (first bytes: {initrd_bytes[:4]!r})")

    print(f"wrote kernel : {kernel_out} ({len(kernel_bytes) / 1048576:.1f} MiB)")
    print(f"wrote initrd : {initrd_out} ({len(initrd_bytes) / 1048576:.1f} MiB)")
    for problem in problems:
        print(f"WARNING: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except IsoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
