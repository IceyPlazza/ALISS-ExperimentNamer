"""Tests for core.slack.naming — pure name generation/detection/validation."""

from datetime import date

import pytest

from core.slack import naming
from core.slack.naming import (
    BOX_FOLDER_LINK_RE,
    CODENAME_RE,
    EXPERIMENT_NAME_RE,
    WORD_COMBO_RE,
    detect_category,
    detect_date,
    extract_codename,
    generate_experiment_name,
    generate_word_combo,
)


def test_generate_word_combo_shape():
    combo = generate_word_combo()
    assert WORD_COMBO_RE.match(combo), combo
    assert combo.islower()
    assert "-" in combo


def test_generate_experiment_name_has_date_and_category():
    name = generate_experiment_name("bph")
    today = date.today().isoformat()
    assert name.startswith(f"{today}-bph-")
    # A generated name (date-category-word-word) is a valid full name.
    assert EXPERIMENT_NAME_RE.match(name), name


@pytest.mark.parametrize("category", naming.EXPERIMENT_CATEGORIES)
def test_generate_experiment_name_for_each_category(category):
    name = generate_experiment_name(category)
    assert f"-{category}-" in name


@pytest.mark.parametrize(
    "folder_name,expected",
    [
        ("2026-06-17 - FLR: CT", "2026-06-17"),
        ("2026-06-23 - BPH", "2026-06-23"),
        ("2025-10-08-bph-coolly-cut", "2025-10-08"),
        ("2026_05_22_bph", "2026-05-22"),
        ("2026.07.04.notes", "2026-07-04"),
        ("no date here", None),
        ("2026-13-40 bad date", None),  # parses shape but not a real date
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
        ("2025-10-08-bph-coolly-cut", "bph"),
        ("Some CAO Experiment", "cao"),
        ("no category", None),
        ("bphony is not a match", None),  # \b word boundary guards substrings
    ],
)
def test_detect_category(folder_name, expected):
    assert detect_category(folder_name) == expected


@pytest.mark.parametrize(
    "name,matches",
    [
        ("2026-07-04-bph-decorous-harbor", True),
        ("2025-10-08-bph-coolly-cut", True),  # legacy adverb-verb name
        ("2026-07-05-bph-sunny-harbor-(3)", True),  # scan-count suffix
        ("2026-07-05-cao-mad-polyphony-modelA", True),  # codename suffix
        ("decorous-harbor", False),  # bare combo, not a full name
        ("2026-07-04-bph", False),  # missing the word-word part
        ("not-a-name", False),
    ],
)
def test_experiment_name_re(name, matches):
    assert bool(EXPERIMENT_NAME_RE.match(name)) is matches


@pytest.mark.parametrize(
    "combo,matches",
    [
        ("coolly-cut", True),
        ("decorous-harbor", True),
        ("one-two-three", True),
        ("single", False),  # needs at least two words
        ("Has-Caps", False),
        ("2026-07-04", False),
    ],
)
def test_word_combo_re(combo, matches):
    assert bool(WORD_COMBO_RE.match(combo)) is matches


@pytest.mark.parametrize(
    "folder_name,expected",
    [
        ("2026-07-05-cao-mad-polyphony-modelA", "modelA"),  # codename
        ("2026-07-05-cao-mad-polyphony-model-v2", "model-v2"),  # hyphenated
        ("2026-07-05-bph-sunny-harbor-(3)", None),  # scan count, not a codename
        ("2026-07-04-bph-decorous-harbor", None),  # no suffix
        ("2025-10-08-bph-coolly-cut", None),  # legacy, no suffix
        ("2026-06-17 - FLR: CT", None),  # not the scheme
    ],
)
def test_extract_codename(folder_name, expected):
    assert extract_codename(folder_name) == expected


@pytest.mark.parametrize(
    "codename,matches",
    [
        ("modelA", True),
        ("model_v2", True),
        ("model-v2", True),
        ("v1.0", True),
        ("has space", False),
        ("no!", False),
    ],
)
def test_codename_re(codename, matches):
    assert bool(CODENAME_RE.match(codename)) is matches


@pytest.mark.parametrize(
    "text,expected_id",
    [
        ("https://vanderbilt.app.box.com/folder/123456789", "123456789"),
        ("https://app.box.com/folder/42", "42"),
        ("see box.com/folder/7 for details", "7"),
        ("https://example.com/folder/999", None),  # not a box link
        ("just some text", None),
    ],
)
def test_box_folder_link_re(text, expected_id):
    m = BOX_FOLDER_LINK_RE.search(text)
    assert (m.group(1) if m else None) == expected_id
