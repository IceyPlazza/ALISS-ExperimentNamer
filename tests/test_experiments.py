"""Tests for core.slack.experiments — association, legacy conversion, and the
create/announce flows, with box_client stubbed out (no network)."""

import pytest

from core.box import box_client
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack.experiments import (
    AssociationError,
    LegacyConversionError,
    add_back_references,
    associations_to_description,
    classify_association,
    convert_legacy_folder,
    finish_new_experiment,
    parse_associations,
    post_delete,
    post_legacy_rename,
    post_prune,
    purge_references,
    resolve_association,
    set_associations_in_description,
)
from core.slack.views import format_experiment_list


# --------------------------------------------------------------------------
# resolve_association
# --------------------------------------------------------------------------


def test_resolve_association_box_link_with_name(monkeypatch):
    monkeypatch.setattr(box_client, "get_folder_name", lambda fid: "modelA-run")
    out = resolve_association("https://app.box.com/folder/123")
    assert out == "<https://app.box.com/folder/123|modelA-run>"


def test_resolve_association_box_link_unreadable_stays_bare(monkeypatch):
    def boom(fid):
        raise RuntimeError("no access")

    monkeypatch.setattr(box_client, "get_folder_name", boom)
    out = resolve_association("https://app.box.com/folder/123")
    assert out == "https://app.box.com/folder/123"


def test_resolve_association_non_box_http_rejected():
    with pytest.raises(AssociationError):
        resolve_association("https://example.com/folder/1")


def test_resolve_association_garbage_rejected():
    with pytest.raises(AssociationError):
        resolve_association("this is not valid")


def test_resolve_association_combo_found(monkeypatch):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {"url": "https://app.box.com/folder/9", "name": "2026-07-05-bph-x-y"},
    )
    out = resolve_association("x-y")
    assert out == "<https://app.box.com/folder/9|2026-07-05-bph-x-y>"


def test_resolve_association_combo_box_unconfigured_kept_literal(monkeypatch):
    def unconfigured(name):
        raise BoxNotConfiguredError("nope")

    monkeypatch.setattr(box_client, "find_experiment_folder", unconfigured)
    assert resolve_association("coolly-cut") == "`coolly-cut`"


def test_resolve_association_ambiguous(monkeypatch):
    def ambiguous(name):
        raise AmbiguousExperimentError(["2026-01-01-bph-a-b", "2026-02-02-cao-a-b"])

    monkeypatch.setattr(box_client, "find_experiment_folder", ambiguous)
    with pytest.raises(AssociationError) as exc:
        resolve_association("a-b")
    assert "several experiments" in str(exc.value)


def test_resolve_association_not_found(monkeypatch):
    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(box_client, "find_experiment_folder", missing)
    with pytest.raises(AssociationError):
        resolve_association("ghost-town")


# --------------------------------------------------------------------------
# classify_association — routes each entry to ready vs legacy
# --------------------------------------------------------------------------


def test_classify_link_named_to_scheme_is_ready(monkeypatch):
    monkeypatch.setattr(box_client, "get_folder_name", lambda fid: "2026-07-05-bph-a-b")
    result = classify_association("https://app.box.com/folder/9")
    assert result["kind"] == "ready"
    assert result["assoc"] == {
        "label": "2026-07-05-bph-a-b",
        "url": "https://app.box.com/folder/9",
        "mrkdwn": "<https://app.box.com/folder/9|2026-07-05-bph-a-b>",
    }


def test_classify_link_unstructured_is_legacy(monkeypatch):
    monkeypatch.setattr(box_client, "get_folder_name", lambda fid: "2026-06-17 - FLR: CT")
    result = classify_association("https://app.box.com/folder/9")
    assert result["kind"] == "legacy"
    lg = result["legacy"]
    assert lg["folder_id"] == "9"
    assert lg["old_name"] == "2026-06-17 - FLR: CT"
    assert lg["detected_date"] == "2026-06-17"
    assert lg["detected_category"] is None  # "FLR"/"CT" aren't bph/cao


def test_classify_link_unreadable_is_ready_bare(monkeypatch):
    def boom(fid):
        raise RuntimeError("no access")

    monkeypatch.setattr(box_client, "get_folder_name", boom)
    result = classify_association("https://app.box.com/folder/9")
    assert result["kind"] == "ready"
    assert result["assoc"]["url"] == "https://app.box.com/folder/9"
    assert result["assoc"]["mrkdwn"] == "https://app.box.com/folder/9"


def test_classify_name_lookup_is_ready(monkeypatch):
    monkeypatch.setattr(
        box_client,
        "find_experiment_folder",
        lambda name: {"url": "u", "name": "2026-07-05-bph-a-b"},
    )
    result = classify_association("a-b")
    assert result["kind"] == "ready"
    assert result["assoc"]["label"] == "2026-07-05-bph-a-b"


def test_classify_bad_input_raises():
    with pytest.raises(AssociationError):
        classify_association("not a valid entry!!")


# --------------------------------------------------------------------------
# association description serialize / parse round-trip
# --------------------------------------------------------------------------


def test_associations_description_round_trip():
    assocs = [
        {"label": "2026-07-05-bph-a-b", "url": "https://app.box.com/folder/9"},
        {"label": "coolly-cut", "url": None},
    ]
    desc = associations_to_description(assocs)
    assert desc.splitlines()[0] == "Associated experiments:"
    parsed = parse_associations(desc)
    assert parsed == [
        {"label": "2026-07-05-bph-a-b", "url": "https://app.box.com/folder/9"},
        {"label": "coolly-cut", "url": None},
    ]


def test_associations_to_description_empty():
    assert associations_to_description([]) == ""


def test_parse_associations_tolerates_junk():
    desc = "Some human note\n\nAssociated experiments:\n- x-y | u1\ngarbage line\n-  \n- z-w"
    parsed = parse_associations(desc)
    assert parsed == [
        {"label": "x-y", "url": "u1"},
        {"label": "z-w", "url": None},
    ]


def test_parse_associations_requires_header():
    # bullets with no header aren't app-managed associations
    assert parse_associations("- x-y | u1\n- z-w | u2") == []
    assert parse_associations("just some human description") == []


def test_set_associations_preserves_preamble():
    existing = "Human notes about this folder.\n\nAssociated experiments:\n- old | u0"
    updated = set_associations_in_description(
        existing, [{"label": "new", "url": "u1"}]
    )
    assert updated.startswith("Human notes about this folder.")
    # old block replaced, not appended
    assert "u0" not in updated
    assert parse_associations(updated) == [{"label": "new", "url": "u1"}]


def test_set_associations_empty_clears_block_keeps_preamble():
    existing = "Human notes.\n\nAssociated experiments:\n- old | u0"
    assert set_associations_in_description(existing, []) == "Human notes."


# --------------------------------------------------------------------------
# bidirectional back-references
# --------------------------------------------------------------------------


class FakeStore:
    """A stand-in for Box folder descriptions keyed by folder id."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, fid):
        return self.data.get(fid, "")

    def set(self, fid, desc):
        self.data[fid] = desc

    def install(self, monkeypatch):
        monkeypatch.setattr(box_client, "get_folder_description", self.get)
        monkeypatch.setattr(box_client, "set_folder_description", self.set)
        return self


def test_add_back_references_writes_and_dedupes(monkeypatch):
    store = FakeStore({"5": "", "9": ""}).install(monkeypatch)
    x = {"name": "2026-07-05-bph-x", "url": "https://app.box.com/folder/1"}
    associations = [
        {"label": "y", "url": "https://app.box.com/folder/5"},
        {"label": "z", "url": "https://app.box.com/folder/9"},
        {"label": "literal", "url": None},  # skipped — no folder id
    ]
    add_back_references(x, associations)
    assert parse_associations(store.get("5")) == [
        {"label": "2026-07-05-bph-x", "url": "https://app.box.com/folder/1"}
    ]
    assert parse_associations(store.get("9"))[0]["label"] == "2026-07-05-bph-x"

    # idempotent: a second run doesn't duplicate the back-reference
    add_back_references(x, associations)
    assert len(parse_associations(store.get("5"))) == 1


def test_add_back_references_preserves_legacy_human_text(monkeypatch):
    store = FakeStore({"5": "Scan notes from the tech."}).install(monkeypatch)
    x = {"name": "2026-07-05-bph-x", "url": "https://app.box.com/folder/1"}
    add_back_references(x, [{"label": "y", "url": "https://app.box.com/folder/5"}])
    desc = store.get("5")
    assert desc.startswith("Scan notes from the tech.")
    assert parse_associations(desc)[0]["label"] == "2026-07-05-bph-x"


def test_add_back_references_skips_self(monkeypatch):
    store = FakeStore({"1": ""}).install(monkeypatch)
    x = {"name": "2026-07-05-bph-x", "url": "https://app.box.com/folder/1"}
    add_back_references(x, [{"label": "self", "url": "https://app.box.com/folder/1"}])
    assert store.get("1") == ""  # never references itself


def test_purge_references_strips_pointers_to_deleted(monkeypatch):
    # Folder 1 (deleted) is referenced by 5 and 9; 9 also refs an unrelated 7.
    store = FakeStore(
        {
            "5": "Associated experiments:\n- 2026-07-05-bph-x | https://app.box.com/folder/1",
            "9": "Notes.\n\nAssociated experiments:\n- 2026-07-05-bph-x | https://app.box.com/folder/1\n- other | https://app.box.com/folder/7",
        }
    ).install(monkeypatch)
    monkeypatch.setattr(
        box_client,
        "list_experiment_folders",
        lambda: [{"id": "5"}, {"id": "9"}],
    )
    purge_references({"1"})
    # pointer to deleted folder 1 removed everywhere…
    assert parse_associations(store.get("5")) == []
    # …unrelated entry + human text on 9 preserved
    assert store.get("9").startswith("Notes.")
    assert parse_associations(store.get("9")) == [
        {"label": "other", "url": "https://app.box.com/folder/7"}
    ]


def test_purge_references_fixes_asymmetric_link(monkeypatch):
    # The parent (folder 2) points at the child (folder 1), but the child has
    # no back-reference — purge still cleans the parent (the reported bug).
    store = FakeStore(
        {
            "2": "Associated experiments:\n- child | https://app.box.com/folder/1",
        }
    ).install(monkeypatch)
    monkeypatch.setattr(
        box_client, "list_experiment_folders", lambda: [{"id": "2"}]
    )
    purge_references({"1"})
    assert parse_associations(store.get("2")) == []


def test_purge_references_noop_when_nothing_deleted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        box_client, "list_experiment_folders", lambda: calls.append("crawl") or []
    )
    purge_references(set())
    assert calls == []  # no crawl when there's nothing to purge


# --------------------------------------------------------------------------
# convert_legacy_folder
# --------------------------------------------------------------------------


def _stub_convert(monkeypatch, *, name, parent_id="P", dir_key="experiments"):
    """Wire box_client so convert_legacy_folder sees a folder with `name`
    inside `dir_key`, and rename_folder echoes the requested new name."""
    monkeypatch.setattr(
        box_client,
        "get_folder_info",
        lambda fid: {"name": name, "parent_id": parent_id},
    )
    monkeypatch.setattr(
        box_client,
        "directory_key_for_parent",
        lambda pid: dir_key if pid == parent_id else None,
    )
    monkeypatch.setattr(
        box_client,
        "rename_folder",
        lambda fid, new_name: {
            "id": fid,
            "name": new_name,
            "url": f"https://app.box.com/folder/{fid}",
        },
    )


def test_convert_legacy_folder_detects_date_and_category(monkeypatch):
    _stub_convert(monkeypatch, name="2026-06-23 - BPH")
    result = convert_legacy_folder("55")
    assert result["old_name"] == "2026-06-23 - BPH"
    assert result["dir_key"] == "experiments"
    assert result["name"].startswith("2026-06-23-bph-")
    assert result["id"] == "55"


def test_convert_legacy_folder_explicit_picks_win(monkeypatch):
    _stub_convert(monkeypatch, name="2026-06-23 - BPH")
    result = convert_legacy_folder("55", picked_date="2020-01-01", picked_category="cao")
    assert result["name"].startswith("2020-01-01-cao-")


def test_convert_legacy_folder_not_configured(monkeypatch):
    def unconfigured(fid):
        raise BoxNotConfiguredError("nope")

    monkeypatch.setattr(box_client, "get_folder_info", unconfigured)
    with pytest.raises(LegacyConversionError) as exc:
        convert_legacy_folder("55")
    assert exc.value.field == "link"


def test_convert_legacy_folder_outside_directories(monkeypatch):
    _stub_convert(monkeypatch, name="2026-06-23 - BPH", dir_key=None)
    with pytest.raises(LegacyConversionError) as exc:
        convert_legacy_folder("55")
    assert exc.value.field == "link"


def test_convert_legacy_folder_undetectable_date(monkeypatch):
    _stub_convert(monkeypatch, name="BPH experiment no date")
    with pytest.raises(LegacyConversionError) as exc:
        convert_legacy_folder("55")
    assert exc.value.field == "date"


def test_convert_legacy_folder_undetectable_category(monkeypatch):
    _stub_convert(monkeypatch, name="2026-06-23 mystery")
    with pytest.raises(LegacyConversionError) as exc:
        convert_legacy_folder("55")
    assert exc.value.field == "category"


# --------------------------------------------------------------------------
# format_experiment_list / announcements
# --------------------------------------------------------------------------


def test_format_experiment_list():
    out = format_experiment_list(
        [
            {"name": "2026-07-05-bph-a-b", "url": "u1", "directory": "Experiments"},
            {"name": "2026-07-05-cao-c-d", "url": "u2", "directory": "Scans"},
        ]
    )
    assert out == (
        "• <u1|2026-07-05-bph-a-b> — _Experiments_\n"
        "• <u2|2026-07-05-cao-c-d> — _Scans_"
    )


def test_post_legacy_rename(monkeypatch, post_message):
    monkeypatch.setattr(
        box_client,
        "directory_info",
        lambda key: {"label": "Experiments", "path": None},
    )
    renamed = {
        "old_name": "2026-06-23 - BPH",
        "name": "2026-06-23-bph-x-y",
        "url": "https://app.box.com/folder/55",
        "dir_key": "experiments",
    }
    post_legacy_rename(post_message, "U1", renamed)
    body = post_message.kwargs
    assert "2026-06-23 - BPH" in body["text"]
    assert "2026-06-23-bph-x-y" in body["text"]
    # the rich announcement carries blocks, not just fallback text
    assert body["blocks"]


def test_post_delete(post_message):
    folder = {"name": "2026-07-05-bph-a-b", "directory": "Experiments"}
    post_delete(post_message, "U1", folder)
    body = post_message.kwargs
    assert "2026-07-05-bph-a-b" in body["text"]
    assert "Experiments" in body["text"]
    assert "<@U1>" in body["blocks"][0]["text"]["text"]


def test_post_prune(post_message):
    deleted = [
        {"name": "2026-07-05-bph-a-b", "directory": "Experiments"},
        {"name": "2026-07-05-cao-c-d", "directory": "Scans"},
    ]
    post_prune(post_message, "U1", deleted)
    block_text = post_message.kwargs["blocks"][0]["text"]["text"]
    assert "pruned 2" in block_text.lower()
    assert "2026-07-05-bph-a-b" in block_text
    assert "2026-07-05-cao-c-d" in block_text


def test_finish_new_experiment_announces_and_persists(
    monkeypatch, post_message, update_ephemeral
):
    monkeypatch.setattr(
        box_client,
        "directory_info",
        lambda key: {"label": "Scans", "path": None},
    )
    captured = {}

    def fake_create(name, dir_key, description=""):
        captured["name"] = name
        captured["description"] = description
        return {"name": name, "url": "https://app.box.com/folder/77"}

    monkeypatch.setattr(box_client, "create_experiment_folder", fake_create)
    finish_new_experiment(
        "bph",
        "scans",
        "U1",
        post_message=post_message,
        update_ephemeral=update_ephemeral,
        associations=[
            {"label": "coolly-cut", "url": None, "mrkdwn": "`coolly-cut`"},
            {"label": "2026-01-01-cao-x-y", "url": "u2", "mrkdwn": "<u2|2026-01-01-cao-x-y>"},
        ],
        suffix="-(3)",
    )
    assert update_ephemeral.text.startswith("Generated:")
    assert captured["name"].endswith("-(3)")
    block_text = post_message.kwargs["blocks"][0]["text"]["text"]
    assert "-(3)" in block_text
    assert "app.box.com/folder/77" in block_text
    # both associations rendered in the announcement…
    assert "coolly-cut" in block_text
    assert "2026-01-01-cao-x-y" in block_text
    # …and persisted to the folder description for track to read back
    assert "coolly-cut" in captured["description"]
    assert "2026-01-01-cao-x-y" in captured["description"]


def test_finish_new_experiment_box_unconfigured_still_announces(
    monkeypatch, post_message, update_ephemeral
):
    monkeypatch.setattr(
        box_client,
        "directory_info",
        lambda key: {"label": "Scans", "path": None},
    )

    def unconfigured(name, dir_key, description=""):
        raise BoxNotConfiguredError("nope")

    monkeypatch.setattr(box_client, "create_experiment_folder", unconfigured)
    finish_new_experiment(
        "cao",
        "scans",
        "U1",
        post_message=post_message,
        update_ephemeral=update_ephemeral,
    )
    block_text = post_message.kwargs["blocks"][0]["text"]["text"]
    assert "-cao-" in block_text
    assert "Open in Box" not in block_text  # no folder link when unconfigured
