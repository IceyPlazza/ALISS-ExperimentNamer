"""Tests for core.slack.naming — pure name generation/detection/validation."""

from datetime import date

import pytest

from core.slack import naming
from core.slack.naming import (
    BOX_FOLDER_LINK_RE,
    CODENAME_RE,
    EXPERIMENT_NAME_RE,
    build_experiment_name,
    detect_category,
    detect_date,
    extract_codename,
    extract_name_parts,
    generate_codename,
    generate_experiment_name,
    sanitize_segment,
)


# --------------------------------------------------------------------------
# codename generation
# --------------------------------------------------------------------------


def test_generate_codename_shape():
    code = generate_codename()
    assert CODENAME_RE.match(code), code
    assert code.islower()
    assert "-" in code


def test_generate_codename_avoids_excluded(monkeypatch):
    seq = iter(["taken-one", "taken-two", "free-combo"])
    monkeypatch.setattr(naming.namer, "generate", lambda **kw: next(seq))
    code = naming.generate_codename(exclude={"taken-one", "taken-two"})
    assert code == "free-combo"


def test_generate_codename_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(naming.namer, "generate", lambda **kw: "always-taken")
    code = naming.generate_codename(exclude={"always-taken"}, attempts=3)
    assert code == "always-taken"


# --------------------------------------------------------------------------
# name building
# --------------------------------------------------------------------------


def test_build_experiment_name_experiments():
    name = build_experiment_name(
        "experiments", "bph", "decorous-harbor", "iven.chen", action="segmentation"
    )
    today = date.today().isoformat()
    assert name == f"{today}-decorous-harbor-BPH-segmentation-iven.chen"
    assert EXPERIMENT_NAME_RE.match(name), name


def test_build_experiment_name_scans():
    name = build_experiment_name(
        "scans", "cao", "sunny-harbor", "iven.chen", num_scans=3
    )
    today = date.today().isoformat()
    assert name == f"{today}-sunny-harbor-CAO-(3)-iven.chen"
    assert EXPERIMENT_NAME_RE.match(name), name


def test_build_experiment_name_uppercases_category():
    name = build_experiment_name(
        "experiments", "flr", "x-y", "u", action="a", on_date="2026-01-01"
    )
    assert name == "2026-01-01-x-y-FLR-a-u"


def test_generate_experiment_name_auto_codename(monkeypatch):
    seq = iter(["dup", "fresh"])
    monkeypatch.setattr(naming.namer, "generate", lambda **kw: next(seq))
    name = generate_experiment_name(
        "experiments", "bph", "iven.chen", action="seg", exclude={"dup"}
    )
    today = date.today().isoformat()
    assert name == f"{today}-fresh-BPH-seg-iven.chen"


def test_generate_experiment_name_codename_override():
    name = generate_experiment_name(
        "scans", "cao", "iven.chen", num_scans=2, codename="my-code"
    )
    assert "-my-code-CAO-(2)-iven.chen" in name


# --------------------------------------------------------------------------
# sanitize_segment
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hello world", "hello-world"),
        ("  trim me  ", "trim-me"),
        ("keep.dots_and-dashes", "keep.dots_and-dashes"),
        ("drop!@#chars", "dropchars"),
        ("modelA", "modelA"),  # case preserved
        ("", ""),
        (None, ""),
    ],
)
def test_sanitize_segment(raw, expected):
    assert sanitize_segment(raw) == expected


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder_name,expected",
    [
        ("2026-06-17 - FLR: CT", "2026-06-17"),
        ("2026-06-23 - BPH", "2026-06-23"),
        ("2025-10-08-bph-coolly-cut", "2025-10-08"),
        ("2026-07-06-decorous-harbor-BPH-seg-iven.chen", "2026-07-06"),
        ("2026_05_22_bph", "2026-05-22"),
        ("2026.07.04.notes", "2026-07-04"),
        ("20260623_PerceptionTuesday", "2026-06-23"),  # compact YYYYMMDD
        ("12345678", None),  # 8 digits but not a valid date
        ("no date here", None),
        ("2026-13-40 bad date", None),
        ("random-folder", None),
    ],
)
def test_detect_date(folder_name, expected):
    assert detect_date(folder_name) == expected


@pytest.mark.parametrize(
    "folder_name,expected",
    [
        ("2026-06-23 - BPH", "bph"),
        ("2026-06-17 - cao scan", "cao"),
        ("2026-06-17 - FLR: CT", "flr"),
        ("2026-07-06-decorous-harbor-BPH-seg-iven.chen", "bph"),  # new scheme, uppercase
        ("2025-10-08-bph-coolly-cut", "bph"),  # legacy, lowercase
        ("Some CAO Experiment", "cao"),
        ("no category", None),
        ("bphony is not a match", None),
    ],
)
def test_detect_category(folder_name, expected):
    assert detect_category(folder_name) == expected


# --------------------------------------------------------------------------
# EXPERIMENT_NAME_RE / CODENAME_RE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,matches",
    [
        ("2026-07-06-decorous-harbor-BPH-segmentation-iven.chen", True),  # new experiments
        ("2026-07-06-sunny-harbor-CAO-(3)-iven.chen", True),  # new scans
        ("2025-10-08-bph-coolly-cut", True),  # legacy adverb-verb name
        ("2026-07-05-bph-sunny-harbor-(3)", True),  # old scan-count suffix
        ("2026-07-05-cao-mad-polyphony-modelA", True),  # old codename suffix
        ("decorous-harbor", False),  # bare codename, not a full name
        ("2026-07-04-bph", False),  # only one token after the date
        ("not-a-name", False),
    ],
)
def test_experiment_name_re(name, matches):
    assert bool(EXPERIMENT_NAME_RE.match(name)) is matches


@pytest.mark.parametrize(
    "codename,matches",
    [
        ("decorous-harbor", True),
        ("modelA", True),
        ("model_v2", True),
        ("v1.0", True),
        ("single", True),  # a single-word codename is allowed
        ("has space", False),
        ("no!", False),
    ],
)
def test_codename_re(codename, matches):
    assert bool(CODENAME_RE.match(codename)) is matches


# --------------------------------------------------------------------------
# extract_codename — the unique lookup token
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder_name,expected",
    [
        # new scheme: codename sits between the date and the CATEGORY token
        ("2026-07-06-decorous-harbor-BPH-segmentation-iven.chen", "decorous-harbor"),
        ("2026-07-06-sunny-harbor-CAO-(3)-iven.chen", "sunny-harbor"),
        ("2026-07-06-modelA-FLR-anno-jane.doe", "modelA"),
        # legacy scheme: word-word combo after the lowercase category
        ("2025-10-08-bph-coolly-cut", "coolly-cut"),
        ("2026-07-05-bph-sunny-harbor-(3)", "sunny-harbor"),  # old suffix
        # neither shape
        ("2026-06-17 - FLR: CT", None),
        ("2026-07-04-bph", None),
        ("random-folder", None),
    ],
)
def test_extract_codename(folder_name, expected):
    assert extract_codename(folder_name) == expected


# --------------------------------------------------------------------------
# extract_name_parts — per-segment columns for new-scheme names
# --------------------------------------------------------------------------


def test_extract_name_parts_experiments():
    parts = extract_name_parts(
        "2026-07-06-decorous-harbor-BPH-segmentation-iven.chen"
    )
    assert parts == {
        "codename": "decorous-harbor",
        "category": "bph",
        "action": "segmentation",
        "num_scans": "",
        "slack_user": "iven.chen",
    }


def test_extract_name_parts_scans():
    parts = extract_name_parts("2026-07-06-sunny-harbor-CAO-(3)-iven.chen")
    assert parts == {
        "codename": "sunny-harbor",
        "category": "cao",
        "action": "",
        "num_scans": "3",
        "slack_user": "iven.chen",
    }


def test_extract_name_parts_multiword_action():
    parts = extract_name_parts("2026-07-06-x-y-FLR-multi-word-action-iven.chen")
    assert parts["action"] == "multi-word-action"
    assert parts["slack_user"] == "iven.chen"
    assert parts["codename"] == "x-y"


def test_extract_name_parts_legacy_returns_none():
    assert extract_name_parts("2025-10-08-bph-coolly-cut") is None
    assert extract_name_parts("2026-06-17 - FLR: CT") is None


# --------------------------------------------------------------------------
# BOX_FOLDER_LINK_RE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_id",
    [
        ("https://vanderbilt.app.box.com/folder/123456789", "123456789"),
        ("https://app.box.com/folder/42", "42"),
        ("see box.com/folder/7 for details", "7"),
        ("https://example.com/folder/999", None),
        ("just some text", None),
    ],
)
def test_box_folder_link_re(text, expected_id):
    m = BOX_FOLDER_LINK_RE.search(text)
    assert (m.group(1) if m else None) == expected_id
