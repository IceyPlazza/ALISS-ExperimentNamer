"""Tests for core.box.csv_index — the experiment_index.csv writer.

Box I/O is replaced by an in-memory FakeBox so the whole index lifecycle
(backfill, reconcile, incremental sync) runs without tokens or network. The
fake counts crawls and uploads so tests can assert the cheap paths don't crawl.
"""

import pytest

from core.box import box_client, csv_index


class FakeBox:
    def __init__(self):
        self.folders = {}  # (parent_id, name) -> folder_id
        self.files = {}  # (parent_id, name) -> file_id
        self.text = {}  # file_id -> str
        self.descriptions = {}  # folder_id -> str
        self.experiments = []  # list_experiment_folders() result
        self.crawl_count = 0
        self.upload_count = 0
        self._n = 1000

    def _newid(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    # ---- patched box_client surface ----
    def root_folder_id(self):
        return "ROOT"

    def find_child_folder(self, parent, name):
        return self.folders.get((parent, name))

    def create_child_folder(self, parent, name):
        fid = self._newid("D")
        self.folders[(parent, name)] = fid
        return fid

    def find_child_file(self, parent, name):
        return self.files.get((parent, name))

    def download_file_text(self, file_id):
        return self.text[file_id]

    def upload_file_text(self, parent, name, text, existing_file_id=None):
        self.upload_count += 1
        if existing_file_id:
            self.text[existing_file_id] = text
            return {"id": existing_file_id, "url": f"url/{existing_file_id}"}
        fid = self._newid("f")
        self.files[(parent, name)] = fid
        self.text[fid] = text
        return {"id": fid, "url": f"url/{fid}"}

    def list_experiment_folders(self):
        self.crawl_count += 1
        return list(self.experiments)

    def get_folder_description(self, folder_id):
        return self.descriptions.get(folder_id, "")

    def file_url(self, file_id):
        return f"url/{file_id}"

    # ---- test helpers ----
    def current_csv(self):
        for (_parent, name), fid in self.files.items():
            if name == csv_index.INDEX_CSV_NAME:
                return self.text[fid]
        return None

    def current_rows(self):
        text = self.current_csv()
        return csv_index._parse(text) if text is not None else []

    def seed_csv(self, rows):
        """Pre-create the folder + CSV (as if the index already existed),
        then reset the counters so a test measures only its own calls."""
        folder_id = self.create_child_folder("ROOT", csv_index.INDEX_FOLDER_NAME)
        self.upload_file_text(
            folder_id, csv_index.INDEX_CSV_NAME, csv_index._serialize(rows)
        )
        self.upload_count = 0
        self.crawl_count = 0


@pytest.fixture
def fb(monkeypatch):
    fake = FakeBox()
    monkeypatch.setattr(box_client, "_root_folder_id", fake.root_folder_id)
    monkeypatch.setattr(box_client, "find_child_folder", fake.find_child_folder)
    monkeypatch.setattr(box_client, "create_child_folder", fake.create_child_folder)
    monkeypatch.setattr(box_client, "find_child_file", fake.find_child_file)
    monkeypatch.setattr(box_client, "download_file_text", fake.download_file_text)
    monkeypatch.setattr(box_client, "upload_file_text", fake.upload_file_text)
    monkeypatch.setattr(box_client, "list_experiment_folders", fake.list_experiment_folders)
    monkeypatch.setattr(box_client, "get_folder_description", fake.get_folder_description)
    monkeypatch.setattr(box_client, "_file_url", fake.file_url)
    return fake


def _exp(folder_id, name, dir_key="scans"):
    return {"id": folder_id, "name": name, "url": f"url/{folder_id}", "dir_key": dir_key}


def _row(**kw):
    base = {c: "" for c in csv_index.COLUMNS}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# Row derivation + (de)serialization (no Box)
# --------------------------------------------------------------------------


def test_row_from_folder_derives_legacy_columns():
    # A legacy-style name (lowercase category, no action/user) → codename comes
    # from the word-word combo; the new per-segment columns stay empty.
    row = csv_index._row_from_folder(
        _exp("101", "2026-07-05-bph-decorous-harbor", "experiments"),
        "iven.chen",
        original_name="2026-06-17 - FLR: CT",
        associated_with="2026-01-01-cao-x-y",
    )
    assert row == {
        "experiment_name": "2026-07-05-bph-decorous-harbor",
        "category": "bph",
        "codename": "decorous-harbor",
        "directory_key": "experiments",
        "box_folder_id": "101",
        "box_url": "url/101",
        "created_date": "2026-07-05",
        "created_by_slack_user": "iven.chen",
        "action": "",
        "num_scans": "",
        "slack_user": "",
        "associated_with": "2026-01-01-cao-x-y",
        "original_name": "2026-06-17 - FLR: CT",
        "parent_experiment": "",
    }


def test_row_from_folder_derives_new_scheme_columns():
    # A new-scheme experiments name → action/slack_user derived from the name.
    row = csv_index._row_from_folder(
        _exp("102", "2026-07-06-glad-river-CAO-segmentation-iven.chen", "experiments"),
        "iven.chen",
    )
    assert row["codename"] == "glad-river"
    assert row["category"] == "cao"
    assert row["action"] == "segmentation"
    assert row["num_scans"] == ""
    assert row["slack_user"] == "iven.chen"


def test_row_from_folder_uses_explicit_parts():
    # sync_created passes exact parts (no name-parsing ambiguity).
    row = csv_index._row_from_folder(
        _exp("103", "2026-07-06-x-y-BPH-(4)-iven.chen", "scans"),
        "iven.chen",
        parts={
            "codename": "x-y",
            "category": "bph",
            "action": "",
            "num_scans": "4",
            "slack_user": "iven.chen",
        },
    )
    assert row["codename"] == "x-y"
    assert row["num_scans"] == "4"
    assert row["slack_user"] == "iven.chen"


def test_serialize_parse_round_trip():
    rows = [
        _row(
            experiment_name="2026-07-05-bph-a-b",
            category="bph",
            codename="a-b",
            directory_key="scans",
            box_folder_id="101",
            box_url="url/101",
            created_date="2026-07-05",
            created_by_slack_user="iven.chen",
            associated_with="2026-01-01-cao-x-y; coolly-cut",  # multi-association
            original_name="",
        ),
        _row(experiment_name="2025-10-08-bph-coolly-cut", box_folder_id="102"),
    ]
    assert csv_index._parse(csv_index._serialize(rows)) == rows


# --------------------------------------------------------------------------
# ensure_index
# --------------------------------------------------------------------------


def test_ensure_index_backfills_when_missing(fb):
    fb.experiments = [
        _exp("101", "2026-07-05-bph-a-b", "scans"),
        _exp("102", "2025-10-08-bph-coolly-cut", "experiments"),
        _exp("103", "not-an-experiment"),  # no date → skipped
    ]
    csv_index.ensure_index()
    assert fb.crawl_count == 1  # backfill crawls once
    rows = fb.current_rows()
    names = {r["experiment_name"] for r in rows}
    assert names == {"2026-07-05-bph-a-b", "2025-10-08-bph-coolly-cut"}
    seeded = next(r for r in rows if r["box_folder_id"] == "101")
    assert seeded["created_by_slack_user"] == "backfill"
    assert seeded["codename"] == "a-b"
    assert seeded["directory_key"] == "scans"


def test_ensure_index_noop_when_present(fb):
    fb.experiments = [_exp("101", "2026-07-05-bph-a-b")]
    csv_index.ensure_index()  # backfills: 1 crawl, 1 upload
    csv_index.ensure_index()  # already present → no crawl, no upload
    assert fb.crawl_count == 1
    assert fb.upload_count == 1


# --------------------------------------------------------------------------
# rebuild (full reconcile)
# --------------------------------------------------------------------------


def test_rebuild_add_remove_update_and_preserve(fb):
    fb.seed_csv(
        [
            _row(
                experiment_name="2026-07-05-bph-old-combo",  # renamed since seeding
                category="bph",
                codename="old-combo",
                directory_key="scans",
                box_folder_id="101",
                box_url="url/101",
                created_date="2026-07-05",
                created_by_slack_user="iven.chen",  # must survive the refresh
                original_name="Legacy X",  # must survive the refresh
            ),
            _row(
                experiment_name="2026-01-01-cao-x-y",
                box_folder_id="999",  # not live anymore → removed
                created_by_slack_user="backfill",
            ),
        ]
    )
    fb.experiments = [
        _exp("101", "2026-07-05-bph-a-b", "scans"),  # renamed → updated
        _exp("202", "2026-07-06-bph-new-one", "experiments"),  # new → added
    ]
    summary = csv_index.rebuild()
    assert fb.crawl_count == 1
    assert summary["added"] == 1
    assert summary["removed"] == 1
    assert summary["updated"] == 1
    assert summary["total"] == 2

    rows = {r["box_folder_id"]: r for r in fb.current_rows()}
    assert set(rows) == {"101", "202"}
    # derived columns refreshed, human-authored columns preserved
    assert rows["101"]["experiment_name"] == "2026-07-05-bph-a-b"
    assert rows["101"]["codename"] == "a-b"
    assert rows["101"]["created_by_slack_user"] == "iven.chen"
    assert rows["101"]["original_name"] == "Legacy X"
    assert rows["202"]["created_by_slack_user"] == "backfill"


def test_rebuild_reads_associations_from_description(fb):
    fb.seed_csv([])
    fb.experiments = [_exp("101", "2026-07-05-bph-a-b", "scans")]
    fb.descriptions["101"] = (
        "Associated experiments:\n"
        "- 2026-01-01-cao-x-y | https://app.box.com/folder/5\n"
        "- coolly-cut | "
    )
    csv_index.rebuild()
    row = fb.current_rows()[0]
    assert row["associated_with"] == "2026-01-01-cao-x-y; coolly-cut"


# --------------------------------------------------------------------------
# incremental sync
# --------------------------------------------------------------------------


def test_index_link_returns_csv_url(fb):
    fb.experiments = []
    assert csv_index.index_link().startswith("url/")


def test_sync_created_upserts_row(fb):
    fb.experiments = []  # ensure backfills an empty CSV first
    csv_index.sync_created(
        _exp("300", "2026-07-07-cao-fresh-combo", "experiments"),
        "jane.doe",
        associated_with="2026-01-01-bph-p-q",
    )
    rows = fb.current_rows()
    assert len(rows) == 1
    assert rows[0]["box_folder_id"] == "300"
    assert rows[0]["created_by_slack_user"] == "jane.doe"
    assert rows[0]["codename"] == "fresh-combo"
    assert rows[0]["associated_with"] == "2026-01-01-bph-p-q"


def test_sync_deleted_removes_matching_row(fb):
    fb.seed_csv(
        [
            _row(experiment_name="2026-07-05-bph-a-b", box_folder_id="101"),
            _row(experiment_name="2026-07-06-cao-c-d", box_folder_id="102"),
        ]
    )
    csv_index.sync_deleted({"101"})
    assert fb.upload_count == 1
    assert {r["box_folder_id"] for r in fb.current_rows()} == {"102"}


def test_sync_deleted_no_match_skips_write(fb):
    fb.seed_csv([_row(experiment_name="2026-07-05-bph-a-b", box_folder_id="101")])
    csv_index.sync_deleted({"nonexistent"})
    assert fb.upload_count == 0  # nothing matched → no upload


def test_sync_renamed_updates_and_preserves_creator(fb):
    fb.seed_csv(
        [
            _row(
                experiment_name="2026-07-05-bph-a-b",
                codename="a-b",
                box_folder_id="101",
                created_by_slack_user="iven.chen",
            )
        ]
    )
    csv_index.sync_renamed(
        _exp("101", "2026-07-05-bph-renamed-combo", "scans"),
        "2026-06-17 - FLR: CT",
    )
    rows = fb.current_rows()
    assert len(rows) == 1
    assert rows[0]["experiment_name"] == "2026-07-05-bph-renamed-combo"
    assert rows[0]["codename"] == "renamed-combo"
    assert rows[0]["original_name"] == "2026-06-17 - FLR: CT"
    assert rows[0]["created_by_slack_user"] == "iven.chen"  # preserved


def test_sync_renamed_records_converter_when_no_prior_row(fb):
    fb.experiments = []  # ensure backfills an empty CSV; folder has no prior row
    csv_index.sync_renamed(
        _exp("500", "2026-07-05-bph-fresh-combo", "experiments"),
        "2026-06-17 - FLR: CT",
        created_by="jane.doe",
    )
    row = fb.current_rows()[0]
    assert row["created_by_slack_user"] == "jane.doe"
    assert row["original_name"] == "2026-06-17 - FLR: CT"


def test_sync_renamed_explicit_creator_overrides_existing(fb):
    fb.seed_csv(
        [
            _row(
                experiment_name="2026-07-05-bph-a-b",
                box_folder_id="101",
                created_by_slack_user="backfill",
            )
        ]
    )
    csv_index.sync_renamed(
        _exp("101", "2026-07-05-bph-a-b", "scans"), "old", created_by="iven.chen"
    )
    assert fb.current_rows()[0]["created_by_slack_user"] == "iven.chen"


# --------------------------------------------------------------------------
# read helpers: query_rows / taken_combos / self_heal
# --------------------------------------------------------------------------


def test_query_rows_backfills_when_missing(fb):
    fb.experiments = [_exp("101", "2026-07-05-bph-a-b", "scans")]
    rows = csv_index.query_rows()
    assert fb.crawl_count == 1  # backfilled once
    assert [r["box_folder_id"] for r in rows] == ["101"]


def test_query_rows_reads_without_crawl_when_present(fb):
    fb.seed_csv([_row(experiment_name="2026-07-05-bph-a-b", box_folder_id="101")])
    rows = csv_index.query_rows()
    assert fb.crawl_count == 0  # present → no crawl
    assert [r["box_folder_id"] for r in rows] == ["101"]


def test_taken_codenames_returns_nonempty(fb):
    fb.seed_csv(
        [
            _row(experiment_name="2026-07-05-bph-a-b", codename="a-b", box_folder_id="1"),
            _row(experiment_name="2026-07-06-cao-c-d", codename="c-d", box_folder_id="2"),
            _row(experiment_name="2026-06-17 - FLR", codename="", box_folder_id="3"),  # no codename
        ]
    )
    assert csv_index.taken_codenames() == {"a-b", "c-d"}


def test_self_heal_upserts_and_preserves_authored_columns(fb):
    fb.seed_csv(
        [
            _row(
                experiment_name="2026-07-05-bph-old",
                category="bph",
                codename="old",
                directory_key="scans",
                box_folder_id="101",
                created_date="2026-07-05",
                created_by_slack_user="iven.chen",
                original_name="Legacy X",
            )
        ]
    )
    csv_index.self_heal(
        [
            _exp("101", "2026-07-05-bph-a-b", "scans"),  # renamed → refresh
            _exp("202", "2026-07-06-cao-c-d", "experiments"),  # new → add
        ]
    )
    rows = {r["box_folder_id"]: r for r in fb.current_rows()}
    assert set(rows) == {"101", "202"}
    assert rows["101"]["experiment_name"] == "2026-07-05-bph-a-b"
    assert rows["101"]["codename"] == "a-b"
    assert rows["101"]["created_by_slack_user"] == "iven.chen"  # preserved
    assert rows["101"]["original_name"] == "Legacy X"  # preserved
    assert rows["202"]["created_by_slack_user"] == "backfill"


def test_self_heal_no_write_when_unchanged(fb):
    folder = _exp("101", "2026-07-05-bph-a-b", "scans")
    fb.seed_csv([csv_index._row_from_folder(folder, "backfill", "", "")])
    csv_index.self_heal([folder])  # identical row → nothing to write
    assert fb.upload_count == 0


# --------------------------------------------------------------------------
# parent_experiment column (sub-experiments)
# --------------------------------------------------------------------------


def test_row_from_folder_parent_from_parts():
    row = csv_index._row_from_folder(
        _exp("1", "2026-07-07-x-y-BPH-a-jane.doe", "experiments"),
        "jane.doe",
        parts={
            "codename": "x-y",
            "category": "bph",
            "action": "a",
            "num_scans": "",
            "slack_user": "jane.doe",
            "parent_experiment": "2026-07-06-p-q-BPH-b-iven.chen",
        },
    )
    assert row["parent_experiment"] == "2026-07-06-p-q-BPH-b-iven.chen"


def test_rebuild_reads_parent_from_description(fb):
    fb.seed_csv([])
    fb.experiments = [_exp("101", "2026-07-07-x-y-BPH-a-jane.doe", "experiments")]
    fb.descriptions["101"] = (
        "Parent experiment:\n"
        "- 2026-07-06-p-q-BPH-b-iven.chen | https://app.box.com/folder/50"
    )
    csv_index.rebuild()
    assert fb.current_rows()[0]["parent_experiment"] == "2026-07-06-p-q-BPH-b-iven.chen"


def test_sync_created_records_parent(fb):
    fb.experiments = []
    csv_index.sync_created(
        _exp("300", "2026-07-07-fresh-combo-CAO-anno-jane.doe", "experiments"),
        "jane.doe",
        parts={
            "codename": "fresh-combo",
            "category": "cao",
            "action": "anno",
            "num_scans": "",
            "slack_user": "jane.doe",
            "parent_experiment": "2026-07-06-p-q-CAO-b-iven.chen",
        },
    )
    assert fb.current_rows()[0]["parent_experiment"] == "2026-07-06-p-q-CAO-b-iven.chen"


# --------------------------------------------------------------------------
# rebuild: index primary (top-level) only, preserve sub-experiment rows
# --------------------------------------------------------------------------


def test_rebuild_preserves_child_rows_and_drops_parentless(fb):
    # A child row (has parent_experiment) whose folder isn't in the top-level
    # crawl must survive; a parentless row that's gone must be dropped.
    fb.seed_csv(
        [
            _row(
                experiment_name="2026-07-07-x-y-BPH-a-jane.doe",
                codename="x-y",
                directory_key="experiments",
                box_folder_id="child1",
                created_by_slack_user="jane.doe",
                parent_experiment="2026-07-06-p-q-BPH-b-iven.chen",
            ),
            _row(
                experiment_name="2026-01-01-cao-gone",
                directory_key="scans",
                box_folder_id="gone1",  # parentless + not live → dropped
            ),
        ]
    )
    fb.experiments = [_exp("top1", "2026-07-06-p-q-BPH-b-iven.chen", "experiments")]
    summary = csv_index.rebuild()
    ids = {r["box_folder_id"] for r in fb.current_rows()}
    assert "child1" in ids  # nested child preserved
    assert "gone1" not in ids  # parentless missing folder dropped
    assert "top1" in ids  # new primary added
    assert summary["removed"] == 1
    assert summary["added"] == 1
