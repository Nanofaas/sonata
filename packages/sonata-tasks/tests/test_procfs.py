import pytest

from sonata_tasks.procfs import parse_kib_field, parse_smaps

SMAPS = """\
1000-3000 rw-p 0 00:00 0
Size: 8 kB
Rss: 4 kB
Pss: 2 kB
3000-4000 r--p 0 08:01 42 /opt/app data.bin (deleted)
Size: 4 kB
Rss: 2 kB
Pss: 1 kB
Anonymous: 2 kB
4000-5000 rw-s 0 00:00 0 /memfd:shared (deleted)
Size: 4 kB
Rss: 0 kB
Pss: 0 kB
5000-6000 r-xp 0 00:00 0 [vdso]
Size: 4 kB
Rss: 4 kB
Pss: 4 kB
"""


def test_smaps_preserves_bytes_mapping_identity_and_backing():
    parsed = parse_smaps(SMAPS)
    assert parsed["mappings"] == 4
    assert parsed["anonymous"] == {"size": 8192, "rss": 4096, "pss": 2048}
    assert parsed["file"] == {"size": 4096, "rss": 2048, "pss": 1024}
    assert parsed["shared_memory"] == {"size": 4096, "rss": 0, "pss": 0}
    assert parsed["unknown"] == {"size": 4096, "rss": 4096, "pss": 4096}
    assert parsed["mapping_details"][1] == {
        "address": "3000-4000",
        "permissions": "r--p",
        "backing": "file",
        "path": "/opt/app data.bin (deleted)",
        "size": 4096,
        "rss": 2048,
        "pss": 1024,
    }
    assert "large_anonymous_mappings" not in parsed


@pytest.mark.parametrize(
    ("permissions", "path", "category"),
    [
        ("rw-p", "[heap]", "anonymous"),
        ("rw-p", "[stack:123]", "anonymous"),
        ("rw-p", "[anon:python-buffer]", "anonymous"),
        ("rw-s", "", "shared_memory"),
        ("rw-s", "[anon_shmem:shared]", "shared_memory"),
        ("rw-s", "/dev/shm/shared", "shared_memory"),
        ("rw-s", "/SYSV00000000", "shared_memory"),
        ("rw-p", "/opt/file", "file"),
        ("r-xp", "[vvar]", "unknown"),
    ],
)
def test_smaps_categories_are_vma_backing_not_page_residency(
    permissions, path, category
):
    parsed = parse_smaps(
        f"1000-2000 {permissions} 0 00:00 0 {path}\n"
        "Size: 4 kB\nRss: 4 kB\nPss: 2 kB\nAnonymous: 4 kB\n"
    )
    assert parsed["mapping_details"][0]["backing"] == category
    assert parsed[category]["rss"] == 4096


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not smaps",
        "garbage\n" + SMAPS,
        SMAPS.rsplit("Pss:", 1)[0],
        SMAPS + "6000-g000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\nPss: 4 kB\n",
        SMAPS.replace("1000-3000", "3000-1000"),
        SMAPS.replace("1000-3000", "1000-1000"),
        SMAPS.replace("Size: 8 kB", "Size: 8 kB\nSize: 8 kB"),
        SMAPS.replace("Rss: 4 kB", "Rss: -4 kB", 1),
        SMAPS.replace("Pss: 2 kB", "Pss: 2 MB", 1),
    ],
)
def test_invalid_smaps_returns_no_partial_totals(text):
    with pytest.raises(ValueError, match="smaps"):
        parse_smaps(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("Name: python\n", None),
        ("VmRSS: 0 kB\n", 0),
        ("VmRSS:\t12 kB \nPss: invalid\n", 12288),
        ("VmRSSExtra: 9 kB\nVmRSS: 3 kB\n", 3072),
    ],
)
def test_kib_field_keeps_absent_zero_and_unrelated_fields_distinct(text, expected):
    assert parse_kib_field(text, "VmRSS") == expected


@pytest.mark.parametrize(
    "text",
    [
        "VmRSS: -1 kB",
        "VmRSS: 1 MB",
        "VmRSS: bad kB",
        "VmRSS: 1 kB\nVmRSS: 2 kB",
        "VmRSS: 0 kB\nVmRSS: 0 kB",
    ],
)
def test_kib_field_rejects_malformed_and_duplicate_selected_values(text):
    with pytest.raises(ValueError, match="invalid procfs VmRSS"):
        parse_kib_field(text, "VmRSS")


def test_kib_field_selects_rollup_fields_without_schema():
    assert parse_kib_field("Pss_Anon: 7 kB\nPss_File: 2 kB\n", "Pss_Anon") == 7168
