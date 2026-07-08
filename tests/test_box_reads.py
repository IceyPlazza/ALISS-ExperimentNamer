"""Tests for the CSV-index-first read layer in core.box.box_client.

These exercise find_experiment_folder / list_experiments_by_* by stubbing the
index rows (box_client._index_rows) and directory_info, plus the _crawl_*
fallbacks. No tokens/network.
"""

import pytest

from core.box import box_client, csv_index
from core.box.box_client import AmbiguousExperimentError


def _row(**kw):
    base = {c: "" for c in csv_index.COLUMNS}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _labels(monkeypatch):
    monkeypatch.setattr(
        box_client,
        "directory_info",
        lambda dk: {"label": dk.capitalize(), "path": f"\\Box\\ARPA-H\\{dk}"},
    )


def _index(monkeypatch, rows):
    monkeypatch.setattr(box_client, "_index_rows", lambda: rows)


# --------------------------------------------------------------------------
# find_experiment_folder
# --------------------------------------------------------------------------


def test_find_by_full_name_from_index(monkeypatch):
    _index(
        monkeypatch,
        [
            _row(
                experiment_name="2026-07-05-bph-a-b",
                codename="a-b",
                directory_key="scans",
                box_folder_id="9",
                box_url="url/9",
                associated_with="2026-01-01-cao-x-y",
            )
        ],
    )
    folder = box_client.find_experiment_folder("2026-07-05-bph-a-b")
    assert folder["id"] == "9"
    assert folder["url"] == "url/9"
    assert folder["directory"] == "Scans"
    assert folder["path"].endswith("scans\\2026-07-05-bph-a-b")
    assert folder["associated_with"] == "2026-01-01-cao-x-y"  # carried through


def test_find_by_combo_matches_suffixed_name(monkeypatch):
    _index(
        monkeypatch,
        [
            _row(
                experiment_name="2026-07-05-bph-sunny-harbor-(3)",
                codename="sunny-harbor",
                directory_key="scans",
                box_folder_id="9",
                box_url="url/9",
            )
        ],
    )
    assert box_client.find_experiment_folder("sunny-harbor")["id"] == "9"


def test_find_ambiguous_combo_raises(monkeypatch):
    _index(
        monkeypatch,
        [
            _row(experiment_name="2026-01-01-bph-a-b", codename="a-b", directory_key="scans", box_folder_id="1"),
            _row(experiment_name="2026-02-02-cao-a-b", codename="a-b", directory_key="experiments", box_folder_id="2"),
        ],
    )
    with pytest.raises(AmbiguousExperimentError) as exc:
        box_client.find_experiment_folder("a-b")
    assert set(exc.value.candidates) == {"2026-01-01-bph-a-b", "2026-02-02-cao-a-b"}


def test_find_miss_falls_back_and_self_heals(monkeypatch):
    _index(monkeypatch, [])  # empty index → miss
    found = {
        "id": "9",
        "name": "2026-07-05-bph-a-b",
        "url": "url/9",
        "directory": "Scans",
        "dir_key": "scans",
        "path": "p",
    }
    monkeypatch.setattr(box_client, "_crawl_find_experiment_folder", lambda name: found)
    healed = []
    monkeypatch.setattr(csv_index, "self_heal", lambda folders: healed.extend(folders))
    assert box_client.find_experiment_folder("2026-07-05-bph-a-b") is found
    assert healed == [found]  # the fallback result is written back to the index


def test_find_not_found_after_fallback(monkeypatch):
    _index(monkeypatch, [])

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(box_client, "_crawl_find_experiment_folder", missing)
    monkeypatch.setattr(csv_index, "self_heal", lambda folders: None)
    with pytest.raises(KeyError):
        box_client.find_experiment_folder("ghost-town")


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------


def test_list_by_date_from_index(monkeypatch):
    _index(
        monkeypatch,
        [
            _row(experiment_name="2026-07-05-bph-a-b", directory_key="scans", box_folder_id="1"),
            _row(experiment_name="2026-07-06-cao-c-d", directory_key="experiments", box_folder_id="2"),
        ],
    )
    out = box_client.list_experiments_by_date("2026-07-05")
    assert [f["name"] for f in out] == ["2026-07-05-bph-a-b"]


def test_list_by_category_and_directory_from_index(monkeypatch):
    rows = [
        _row(experiment_name="2026-07-05-bph-a-b", category="bph", directory_key="scans", box_folder_id="1"),
        _row(experiment_name="2026-07-06-bph-c-d", category="bph", directory_key="experiments", box_folder_id="2"),
        _row(experiment_name="2026-07-06-cao-e-f", category="cao", directory_key="scans", box_folder_id="3"),
    ]
    _index(monkeypatch, rows)
    assert {f["name"] for f in box_client.list_experiments_by_category("bph")} == {
        "2026-07-05-bph-a-b",
        "2026-07-06-bph-c-d",
    }
    assert {f["name"] for f in box_client.list_experiments_by_category("bph", "scans")} == {
        "2026-07-05-bph-a-b"
    }
    assert len(box_client.list_experiments_by_category()) == 3  # both None → all


def test_listing_falls_back_to_crawl_when_index_unusable(monkeypatch):
    _index(monkeypatch, None)  # index not usable (e.g. root unconfigured)
    monkeypatch.setattr(
        box_client,
        "_crawl_experiments_by_category",
        lambda category=None, dir_key=None: [{"name": "crawled"}],
    )
    assert box_client.list_experiments_by_category("bph") == [{"name": "crawled"}]


# --------------------------------------------------------------------------
# parent/child relationship flags
# --------------------------------------------------------------------------


def test_listing_flags_parent_and_children(monkeypatch):
    rows = [
        _row(
            experiment_name="2026-07-06-p-q-BPH-b-iven.chen",
            category="bph",
            directory_key="experiments",
            box_folder_id="1",
        ),
        _row(
            experiment_name="2026-07-07-x-y-BPH-a-jane.doe",
            category="bph",
            directory_key="experiments",
            box_folder_id="2",
            parent_experiment="2026-07-06-p-q-BPH-b-iven.chen",
        ),
    ]
    _index(monkeypatch, rows)
    out = {f["name"]: f for f in box_client.list_experiments_by_category("bph")}
    # the child knows its parent…
    assert out["2026-07-07-x-y-BPH-a-jane.doe"]["has_parent"] is True
    assert out["2026-07-07-x-y-BPH-a-jane.doe"]["has_children"] is False
    # …and the parent is flagged as having children (derived)
    assert out["2026-07-06-p-q-BPH-b-iven.chen"]["has_children"] is True
    assert out["2026-07-06-p-q-BPH-b-iven.chen"]["has_parent"] is False
