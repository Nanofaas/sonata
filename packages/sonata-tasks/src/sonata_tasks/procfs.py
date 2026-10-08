"""Decode captured Linux procfs memory text without collecting or inferring causes.

These pure parsers can run on any host. Callers bound captured text and select
fields; mapping backing describes VMAs, not every resident page inside them.
"""

from __future__ import annotations

import re
from typing import TypedDict


class SmapsTotals(TypedDict):
    """Virtual size, resident size and proportional resident size in bytes."""

    size: int
    rss: int
    pss: int


class SmapsMapping(SmapsTotals):
    """One mapping's identity, backing category and memory byte counts."""

    address: str
    permissions: str
    backing: str
    path: str


class SmapsResult(TypedDict):
    """Complete mapping inventory and totals grouped by VMA backing."""

    mappings: int
    mapping_details: list[SmapsMapping]
    anonymous: SmapsTotals
    file: SmapsTotals
    shared_memory: SmapsTotals
    unknown: SmapsTotals


_KB = re.compile(r"^(\w+):\s+(\d+) kB$", re.MULTILINE)
_HEADER = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+)[ \t]+([rwxps-]{4})[ \t]+"
    r"[0-9a-f]+[ \t]+([0-9a-f]+:[0-9a-f]+)[ \t]+([0-9]+)"
    r"(?:[ \t]+(.*))?$",
    re.MULTILINE,
)


def _kilobytes(value: str) -> int:
    return int(value) * 1024


def parse_kib_field(text: str | None, field: str) -> int | None:
    """Read a selected KiB field as bytes, keeping missing distinct from zero.

    Malformed or duplicate selected fields raise ValueError. Unrelated records
    are ignored; None means no captured source. Callers own capture bounds.
    """
    if text is None:
        return None
    found = None
    for line in text.splitlines():
        if line.startswith(field + ":"):
            match = re.fullmatch(re.escape(field) + r":\s+(\d+)\s+kB\s*", line)
            if match is None or found is not None:
                raise ValueError(f"invalid procfs {field}")
            found = _kilobytes(match[1])
    return found


def _backing(path: str, permissions: str) -> str:
    """Describe VMA backing, never the backing of every resident page."""
    # /dev/shm/ is a procfs path prefix in a mapping header, not a temp
    # directory this code creates; bandit matches the literal by name.
    if path.startswith(
        ("[anon_shmem:", "/dev/shm/", "/memfd:", "/SYSV")  # nosec B108
    ) or (not path and permissions.endswith("s")):
        return "shared_memory"
    if permissions.endswith("p") and (
        not path
        or path in {"[heap]", "[stack]"}
        or path.startswith(("[anon:", "[stack:"))
    ):
        return "anonymous"
    if path.startswith("/"):
        return "file"
    return "unknown"


def parse_smaps(text: str) -> SmapsResult:
    """Reject incomplete records and separate VMA backing from page residency."""
    headers = list(_HEADER.finditer(text))
    if not headers or text[: headers[0].start()].strip():
        raise ValueError("smaps has no complete mapping header")
    totals: dict[str, SmapsTotals] = {
        name: {"size": 0, "rss": 0, "pss": 0}
        for name in ("anonymous", "file", "shared_memory", "unknown")
    }
    records: list[SmapsMapping] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end]
        # A malformed subsequent header must not silently join this record.
        if re.search(r"^[0-9a-f]+-", body, re.MULTILINE):
            raise ValueError("smaps contains an unrecognized mapping header")
        pairs = _KB.findall(body)
        fields = dict(pairs)
        required = {"Size", "Rss", "Pss"}
        if any(sum(name == key for name, _ in pairs) != 1 for key in required):
            raise ValueError("smaps mapping has missing or duplicate Size/Rss/Pss")
        if int(header[2], 16) <= int(header[1], 16):
            raise ValueError("invalid smaps address range")
        path = (header[6] or "").strip()
        backed = _backing(path, header[3])
        values: SmapsTotals = {
            "size": _kilobytes(fields["Size"]),
            "rss": _kilobytes(fields["Rss"]),
            "pss": _kilobytes(fields["Pss"]),
        }
        record: SmapsMapping = {
            "address": f"{header[1]}-{header[2]}",
            "permissions": header[3],
            "backing": backed,
            "path": path,
            **values,
        }
        records.append(record)
        for key, value in values.items():
            totals[backed][key] += value
    return {
        "mappings": len(records),
        "mapping_details": records,
        "anonymous": totals["anonymous"],
        "file": totals["file"],
        "shared_memory": totals["shared_memory"],
        "unknown": totals["unknown"],
    }
