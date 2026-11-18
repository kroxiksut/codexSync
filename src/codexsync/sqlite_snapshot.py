"""Materialize SQLite's committed main + WAL image without opening either.

Only the documented WAL format 3007000 is accepted. Checksums cover every
complete frame, including uncommitted frames; malformed/truncated tails are
refused. No recovery, checkpoint, shared-index rebuild or file write occurs.
The result is for a private in-memory SQLite connection, never for persistence.
"""
from __future__ import annotations

import struct


MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


class SnapshotRefused(ValueError):
    """The source cannot be interpreted as one bounded committed image."""


def _checksum(payload: bytes, previous: tuple[int, int], order: str) -> tuple[int, int]:
    first, second = previous
    for left, right in struct.iter_unpack(order + "II", payload):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def materialize_database(main: bytes, wal: bytes = b"", *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> bytes:
    """Apply only frames through the last commit, with no source handles.

    Bounded main/WAL/output lengths protect diagnostic reads from a malformed
    page number or commit size requesting an unbounded allocation.
    """
    if len(main) > max_bytes or len(wal) > max_bytes:
        raise SnapshotRefused("SQLITE_SNAPSHOT_TOO_LARGE")
    if len(main) < 100 or main[:16] != b"SQLite format 3\x00":
        raise SnapshotRefused("SQLITE_HEADER_UNKNOWN")
    page_size = int.from_bytes(main[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1) or len(main) % page_size:
        raise SnapshotRefused("SQLITE_PAGE_SIZE_UNKNOWN")
    if main[18] not in (1, 2) or main[19] not in (1, 2):
        raise SnapshotRefused("SQLITE_FORMAT_UNKNOWN")
    committed: dict[int, bytes] = {}
    commit_size = len(main) // page_size
    if wal:
        if len(wal) < 32:
            raise SnapshotRefused("SQLITE_WAL_TRUNCATED")
        magic, version, wal_page_size = struct.unpack(">III", wal[:12])
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or wal_page_size != page_size:
            raise SnapshotRefused("SQLITE_WAL_FORMAT_UNKNOWN")
        if (len(wal) - 32) % (24 + page_size):
            raise SnapshotRefused("SQLITE_WAL_TRUNCATED")
        order = "<" if magic == 0x377F0682 else ">"
        checksum = _checksum(wal[:24], (0, 0), order)
        if checksum != struct.unpack(">II", wal[24:32]):
            raise SnapshotRefused("SQLITE_WAL_CHECKSUM")
        pending: dict[int, bytes] = {}
        for offset in range(32, len(wal), 24 + page_size):
            page_number, size = struct.unpack(">II", wal[offset:offset+8])
            if not page_number or max(page_number, size) > max_bytes // page_size:
                raise SnapshotRefused("SQLITE_WAL_PAGE_RANGE")
            if wal[offset+8:offset+16] != wal[16:24]:
                raise SnapshotRefused("SQLITE_WAL_SALT")
            page = wal[offset+24:offset+24+page_size]
            checksum = _checksum(wal[offset:offset+8], checksum, order)
            checksum = _checksum(page, checksum, order)
            if checksum != struct.unpack(">II", wal[offset+16:offset+24]):
                raise SnapshotRefused("SQLITE_WAL_CHECKSUM")
            pending[page_number] = page
            if size:
                if size > commit_size and any(number not in pending for number in range(commit_size + 1, size + 1)):
                    # A growing transaction supplies every new page. Guessing
                    # zeros, or reusing main pages an earlier commit truncated,
                    # would manufacture a database that never existed.
                    raise SnapshotRefused("SQLITE_WAL_MISSING_PAGE")
                committed.update(pending)
                # A truncate commit discards pages even if a later transaction
                # grows the database again. Do not resurrect an older frame.
                committed = {number: value for number, value in committed.items() if number <= size}
                pending.clear()
                commit_size = size
    image = bytearray(main[:commit_size * page_size])
    if len(image) < commit_size * page_size:
        image.extend(bytes(commit_size * page_size - len(image)))
    for number, page in committed.items():
        start = (number - 1) * page_size
        image[start:start+page_size] = page
    # deserialize does not implement WAL recovery. The private image already
    # contains committed frames, so tell SQLite it needs no WAL machinery.
    image[18:20] = b"\x01\x01"
    return bytes(image)
