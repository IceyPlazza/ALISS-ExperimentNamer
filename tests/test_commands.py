"""Tests for core.slack.commands — /experiment subcommand handlers, with
box_client stubbed and a Recorder standing in for Slack's `respond`."""

import pytest

from core.box import box_client
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack import commands
from core.slack.commands import (
    cmd_category,
    cmd_date,
    cmd_delete,
    cmd_experiments,
    cmd_new,
    cmd_scans,
    cmd_track,
)


# --------------------------------------------------------------------------
# cmd_new — pure UI, no Box
# --------------------------------------------------------------------------


def test_cmd_new_shows_category_buttons(respond):
    cmd_new(respond, "")
    blocks = respond.kwargs["blocks"]
    actions = [b for b in blocks if b["type"] == "actions"][0]
    action_ids = {el["action_id"] for el in actions["elements"]}
    assert action_ids == {"pick_category_bph", "pick_category_cao"}


# --------------------------------------------------------------------------
# cmd_track
# --------------------------------------------------------------------------


def test_cmd_track_no_arg_shows_usage(respond):
    cmd_track(respond, "")
    assert "Usage" in respond.text


def test_cmd_track_bad_name(respond):
    cmd_track(respond, "not a name!!")
    assert "doesn't look like" in respond.text


def test_cmd_track_box_not_configured(monkeypatch, respond):
    def unconfigured(name):
        raise BoxNotConfiguredError("nope")

    monkeypatch.setattr(box_client, "find_experiment_folder", unconfigured)
    cmd_track(respond, "coolly-cut")
    assert "isn't connected" in respond.text


def test_cmd_track_found(monkeypatch, respond):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {
            "id": "9",
            "path": "\\Box\\ARPA-H\\Experiments\\2026-07-05-bph-a-b",
            "url": "https://app.box.com/folder/9",
            "name": "2026-07-05-bph-a-b",
        },
    )
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: "")
    cmd_track(respond, "a-b")
    assert "\\Box\\ARPA-H\\Experiments\\2026-07-05-bph-a-b" in respond.text
    assert "app.box.com/folder/9" in respond.text


def test_cmd_track_lists_associations(monkeypatch, respond):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {
            "id": "9",
            "path": "\\Box\\X\\2026-07-05-bph-a-b",
            "url": "https://app.box.com/folder/9",
            "name": "2026-07-05-bph-a-b",
        },
    )
    monkeypatch.setattr(
        box_client,
        "get_folder_description",
        lambda fid: (
            "Associated experiments:\n"
            "- 2026-01-01-cao-x-y | https://app.box.com/folder/5\n"
            "- coolly-cut | "
        ),
    )
    cmd_track(respond, "a-b")
    text = respond.text
    assert "Associated experiments:" in text
    # linked association keeps its URL; the URL-less one is shown in backticks
    assert "<https://app.box.com/folder/5|2026-01-01-cao-x-y>" in text
    assert "`coolly-cut`" in text


def test_cmd_track_no_associations(monkeypatch, respond):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {
            "id": "9",
            "path": "\\Box\\X\\2026-07-05-bph-a-b",
            "url": "u",
            "name": "2026-07-05-bph-a-b",
        },
    )
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: "")
    cmd_track(respond, "a-b")
    assert "Associated experiments" not in respond.text


def test_cmd_track_ambiguous(monkeypatch, respond):
    def ambiguous(name):
        raise AmbiguousExperimentError(["2026-01-01-bph-a-b", "2026-02-02-cao-a-b"])

    monkeypatch.setattr(box_client, "find_experiment_folder", ambiguous)
    cmd_track(respond, "a-b")
    assert "more than one" in respond.text
    assert "2026-01-01-bph-a-b" in respond.text


def test_cmd_track_not_found(monkeypatch, respond):
    monkeypatch.setattr(
        box_client, "find_experiment_folder", lambda name: (_ for _ in ()).throw(KeyError(name))
    )
    cmd_track(respond, "ghost-town")
    assert "No Box folder found" in respond.text


# ---- track by codename (not a full name / word-word combo) --------------


def _codename_folders(category=None, dir_key=None):
    # Stands in for list_experiments_by_category(dir_key="experiments"): the
    # codename crawl is scoped to the Experiments directory.
    return [
        {
            "id": "1",
            "name": "2026-07-05-cao-mad-polyphony-modelA",
            "url": "https://app.box.com/folder/1",
            "directory": "Experiments",
            "path": "\\Box\\ARPA-H\\Experiments\\2026-07-05-cao-mad-polyphony-modelA",
        },
        {
            "id": "2",
            "name": "2026-07-06-bph-glad-river-modelA",
            "url": "https://app.box.com/folder/2",
            "directory": "Experiments",
            "path": "\\Box\\ARPA-H\\Experiments\\2026-07-06-bph-glad-river-modelA",
        },
        {
            "id": "3",
            "name": "2026-07-07-cao-wry-oak-solo",
            "url": "https://app.box.com/folder/3",
            "directory": "Experiments",
            "path": "\\Box\\ARPA-H\\Experiments\\2026-07-07-cao-wry-oak-solo",
        },
        # no codename — must never match a codename query
        {"id": "4", "name": "2026-07-08-bph-plain-combo", "url": "u", "directory": "Experiments", "path": "p"},
    ]


def test_cmd_track_codename_single_shows_detail(monkeypatch, respond):
    monkeypatch.setattr(box_client, "list_experiments_by_category", _codename_folders)
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: "")
    cmd_track(respond, "solo")  # single-token, not a combo
    assert "2026-07-07-cao-wry-oak-solo" in respond.text  # full path/detail view
    assert "app.box.com/folder/3" in respond.text
    assert "use codename" not in respond.text  # not the multi-match listing


def test_cmd_track_codename_duplicated_lists_all(monkeypatch, respond):
    monkeypatch.setattr(box_client, "list_experiments_by_category", _codename_folders)
    cmd_track(respond, "modelA")
    assert "2 experiments use codename `modelA`" in respond.text
    assert "2026-07-05-cao-mad-polyphony-modelA" in respond.text
    assert "2026-07-06-bph-glad-river-modelA" in respond.text
    assert "2026-07-07-cao-wry-oak-solo" not in respond.text  # different codename


def test_cmd_track_codename_not_found(monkeypatch, respond):
    monkeypatch.setattr(box_client, "list_experiments_by_category", _codename_folders)
    cmd_track(respond, "nomatch")
    assert "No experiment found with name or codename" in respond.text


def test_cmd_track_codename_box_not_configured(monkeypatch, respond):
    def unconfigured(category=None, dir_key=None):
        raise BoxNotConfiguredError("nope")

    monkeypatch.setattr(box_client, "list_experiments_by_category", unconfigured)
    cmd_track(respond, "modelA")
    assert "isn't connected" in respond.text


# --------------------------------------------------------------------------
# cmd_date
# --------------------------------------------------------------------------


def test_cmd_date_no_arg(respond):
    cmd_date(respond, "")
    assert "Usage" in respond.text


def test_cmd_date_invalid(respond):
    cmd_date(respond, "2026-13-40")
    assert "isn't a valid date" in respond.text


def test_cmd_date_empty_result(monkeypatch, respond):
    monkeypatch.setattr(box_client, "list_experiments_by_date", lambda d: [])
    cmd_date(respond, "2026-07-05")
    assert "No experiments found" in respond.text


def test_cmd_date_found(monkeypatch, respond):
    monkeypatch.setattr(
        box_client,
        "list_experiments_by_date",
        lambda d: [{"name": "2026-07-05-bph-a-b", "url": "u", "directory": "Scans"}],
    )
    cmd_date(respond, "2026-07-05")
    assert "2026-07-05-bph-a-b" in respond.text


# --------------------------------------------------------------------------
# cmd_category
# --------------------------------------------------------------------------


def test_cmd_category_invalid_code(respond):
    cmd_category(respond, "xyz")
    assert "Usage" in respond.text


def test_cmd_category_found(monkeypatch, respond):
    monkeypatch.setattr(
        box_client,
        "list_experiments_by_category",
        lambda c=None, dk=None: [{"name": "2026-07-05-bph-a-b", "url": "u", "directory": "Scans"}],
    )
    cmd_category(respond, "BPH")  # case-insensitive
    assert "2026-07-05-bph-a-b" in respond.text


# --------------------------------------------------------------------------
# cmd_scans / cmd_experiments — per-directory listings
# --------------------------------------------------------------------------


def test_cmd_scans_lists_whole_directory(monkeypatch, respond):
    seen = {}

    def stub(c=None, dk=None):
        seen["code"], seen["dir_key"] = c, dk
        return [{"name": "2026-07-05-cao-a-b", "url": "u", "directory": "Data Collection and Scans"}]

    monkeypatch.setattr(box_client, "list_experiments_by_category", stub)
    monkeypatch.setattr(
        box_client, "directory_info", lambda dk: {"label": "Data Collection and Scans", "path": None}
    )
    cmd_scans(respond, "")  # no category → everything in the Scans directory
    assert seen == {"code": None, "dir_key": "scans"}
    assert "All experiments in _Data Collection and Scans_" in respond.text
    assert "2026-07-05-cao-a-b" in respond.text


def test_cmd_scans_filtered_by_category(monkeypatch, respond):
    seen = {}

    def stub(c=None, dk=None):
        seen["code"], seen["dir_key"] = c, dk
        return [{"name": "2026-07-05-bph-a-b", "url": "u", "directory": "Data Collection and Scans"}]

    monkeypatch.setattr(box_client, "list_experiments_by_category", stub)
    monkeypatch.setattr(
        box_client, "directory_info", lambda dk: {"label": "Data Collection and Scans", "path": None}
    )
    cmd_scans(respond, "BPH")  # case-insensitive
    assert seen == {"code": "bph", "dir_key": "scans"}
    assert "BPH experiments in _Data Collection and Scans_" in respond.text


def test_cmd_scans_invalid_category(respond):
    cmd_scans(respond, "xyz")
    assert "Usage" in respond.text
    assert "scans" in respond.text  # usage names the subcommand


def test_cmd_experiments_scopes_to_experiments_dir(monkeypatch, respond):
    seen = {}

    def stub(c=None, dk=None):
        seen["dir_key"] = dk
        return []

    monkeypatch.setattr(box_client, "list_experiments_by_category", stub)
    monkeypatch.setattr(
        box_client, "directory_info", lambda dk: {"label": "Experiments", "path": None}
    )
    cmd_experiments(respond, "")
    assert seen["dir_key"] == "experiments"
    assert "No experiments found in _Experiments_" in respond.text


# --------------------------------------------------------------------------
# cmd_delete + prune
# --------------------------------------------------------------------------


def test_cmd_delete_no_arg(respond):
    cmd_delete(respond, "")
    assert "Usage" in respond.text


def test_cmd_delete_refuses_nonempty(monkeypatch, respond, say):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {"id": "1", "url": "u", "directory": "Scans"},
    )
    monkeypatch.setattr(box_client, "folder_has_files", lambda fid: True)
    deleted = []
    monkeypatch.setattr(box_client, "delete_experiment_folder", lambda fid: deleted.append(fid))
    cmd_delete(respond, "2026-07-05-bph-a-b", say=say, body={"user_id": "U1"})
    assert "not deleting" in respond.text
    assert deleted == []  # never deleted a folder with files
    assert len(say) == 0  # nothing announced when we refuse


def test_cmd_delete_empty_folder_announces(monkeypatch, respond, say):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {"id": "1", "name": "2026-07-05-bph-a-b", "url": "u", "directory": "Scans"},
    )
    monkeypatch.setattr(box_client, "folder_has_files", lambda fid: False)
    deleted = []
    monkeypatch.setattr(box_client, "delete_experiment_folder", lambda fid: deleted.append(fid))
    cmd_delete(respond, "2026-07-05-bph-a-b", say=say, body={"user_id": "U1"})
    assert deleted == ["1"]
    # announcement goes to the channel (say), mentions the actor + folder
    assert "deleted" in say.text.lower()
    assert "2026-07-05-bph-a-b" in say.text
    assert "<@U1>" in say.text


def test_cmd_delete_empty_keyword_prunes_and_announces(monkeypatch, respond, say):
    folders = [
        {"id": "1", "name": "empty-one", "directory": "Scans"},
        {"id": "2", "name": "has-files", "directory": "Scans"},
    ]
    monkeypatch.setattr(box_client, "list_experiment_folders", lambda: folders)
    monkeypatch.setattr(box_client, "folder_has_files", lambda fid: fid == "2")
    deleted = []
    monkeypatch.setattr(box_client, "delete_experiment_folder", lambda fid: deleted.append(fid))
    cmd_delete(respond, "empty", say=say, body={"user_id": "U1"})
    assert deleted == ["1"]  # only the empty one pruned
    assert "Pruned 1" in say.text
    # the pruned names are listed in the block body, not the fallback text
    assert "empty-one" in say.kwargs["blocks"][0]["text"]["text"]


def test_cmd_delete_purges_references(monkeypatch, respond, say):
    # X (folder 1) is deleted; Y (folder 5) still points at it — even though X
    # has no back-reference to Y (asymmetric), the purge crawl cleans Y.
    store = {
        "1": "",  # X has no association to Y — the reported asymmetric case
        "5": "Associated experiments:\n- 2026-07-05-bph-x | https://app.box.com/folder/1",
    }
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {
            "id": "1",
            "name": "2026-07-05-bph-x",
            "url": "https://app.box.com/folder/1",
            "directory": "Experiments",
        },
    )
    monkeypatch.setattr(box_client, "folder_has_files", lambda fid: False)
    monkeypatch.setattr(box_client, "delete_experiment_folder", lambda fid: store.pop(fid, None))
    # after delete, the crawl sees the remaining folder(s)
    monkeypatch.setattr(box_client, "list_experiment_folders", lambda: [{"id": "5"}])
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: store.get(fid, ""))
    monkeypatch.setattr(box_client, "set_folder_description", store.__setitem__)
    cmd_delete(respond, "2026-07-05-bph-x", say=say, body={"user_id": "U1"})
    # Y no longer links to the deleted X, despite the missing back-reference
    assert "folder/1" not in store["5"]
    assert "deleted" in say.text.lower()


def test_cmd_delete_empty_keyword_nothing_to_prune(monkeypatch, respond, say):
    monkeypatch.setattr(box_client, "list_experiment_folders", lambda: [])
    cmd_delete(respond, "empty", say=say, body={"user_id": "U1"})
    assert "nothing to prune" in respond.text.lower()
    assert len(say) == 0  # nothing pruned → no channel noise
