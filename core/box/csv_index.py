"""The experiment_index.csv "database" — writer half.

`experiment_index.csv` is a single CSV that mirrors every experiment folder in
both Box directories, so lookups don't have to crawl the (large) working
directory. It lives in a dedicated `.experiment-namer` folder under
BOX_ROOT_FOLDER_ID (production: \\Box\\ARPA-H\\.experiment-namer) — the bot is
its only writer; the team may read it to inspect/verify.

This module owns the index's *shape* and lifecycle:
- `ensure_index()`  — create the folder/CSV if missing (backfill crawl); else no-op.
- `rebuild()`       — full reconcile against a live crawl (self-healing). Backs
                      `/experiment database update`.
- `index_link()`    — the CSV's URL. Backs `/experiment database retrieve`.
- `sync_created` / `sync_deleted` / `sync_renamed` — incremental upserts/removes
  called from the file-modifying commands so the CSV stays current WITHOUT a
  crawl on the happy path.

Box I/O goes entirely through `box_client`, so this module is unit-testable
with `box_client` monkeypatched. All read-modify-write sequences are guarded by
a module-level lock (Bolt dispatches handlers on threads, and Box has no atomic
append — every write is download-modify-upload).

Membership rule: a crawled folder is indexed only if `detect_date` finds a date
in its name — this keeps new + legacy experiment folders and skips
non-experiment siblings (stray folders, annotation files).

NOTE (scope): the read side (track/date/category reading the CSV instead of
crawling, and combo-uniqueness re-roll in `new`) is a later pass — those
lookups still crawl Box today.
"""

import csv
import io
import threading

from core.box import box_client
from core.slack.naming import (
    detect_category,
    detect_date,
    extract_codename,
    extract_name_parts,
)

INDEX_FOLDER_NAME = ".experiment-namer"
INDEX_CSV_NAME = "experiment_index.csv"

# Fixed column order — matches experiment_index.example.csv.
COLUMNS = [
    "experiment_name",
    "category",
    "codename",
    "directory_key",
    "box_folder_id",
    "box_url",
    "created_date",
    "created_by_slack_user",
    "action",
    "num_scans",
    "slack_user",
    "associated_with",
    "original_name",
    "parent_experiment",
]

# Marker for rows seeded by a crawl rather than by /experiment new.
BACKFILL = "backfill"

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Row derivation and (de)serialization
# --------------------------------------------------------------------------


def _row_from_folder(
    folder,
    created_by,
    original_name="",
    associated_with="",
    parent_experiment="",
    parts=None,
):
    """Build an index row from a folder dict (id/name/url/dir_key).

    The per-segment columns (codename/category/action/num_scans/slack_user) come
    from `parts` when supplied (the create/rename flow passes them explicitly, so
    there's no name-parsing ambiguity); otherwise they're derived from the name,
    which also handles legacy folders. `parent_experiment` (the parent's name for
    a sub-experiment) comes from `parts` if present, else the caller-supplied
    value (read from the folder's description). The rest are supplied by the
    caller."""
    name = folder["name"]
    p = parts if parts is not None else (extract_name_parts(name) or {})
    return {
        "experiment_name": name,
        "category": detect_category(name) or "",
        "codename": p.get("codename") or extract_codename(name) or "",
        "directory_key": folder.get("dir_key", ""),
        "box_folder_id": folder["id"],
        "box_url": folder["url"],
        "created_date": p.get("created_date") or detect_date(name) or "",
        "created_by_slack_user": created_by,
        "action": p.get("action", ""),
        "num_scans": p.get("num_scans", ""),
        "slack_user": p.get("slack_user", ""),
        "associated_with": associated_with,
        "original_name": original_name,
        "parent_experiment": p.get("parent_experiment") or parent_experiment,
    }


def _associated_with(folder_id):
    """The folder's associations as a "; "-joined label list, read from its Box
    description (the source of truth). Best-effort — "" on any failure.

    parse_associations is imported lazily to avoid a circular import
    (experiments imports csv_index)."""
    try:
        from core.slack.experiments import parse_associations

        assocs = parse_associations(box_client.get_folder_description(folder_id))
        return "; ".join(a["label"] for a in assocs)
    except Exception:
        return ""


def _parent_experiment(folder_id):
    """The folder's parent experiment name, read from its Box description's
    `Parent experiment:` section (the source of truth). "" if none / on error."""
    try:
        from core.slack.experiments import parse_parent

        parent = parse_parent(box_client.get_folder_description(folder_id))
        return parent["label"] if parent else ""
    except Exception:
        return ""


def _serialize(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in COLUMNS})
    return out.getvalue()


def _parse(text):
    """Parse CSV text into rows keyed by exactly COLUMNS (missing/None → "")."""
    reader = csv.DictReader(io.StringIO(text))
    return [{col: (r.get(col) or "") for col in COLUMNS} for r in reader]


# --------------------------------------------------------------------------
# Locate / load / write (all callers hold _lock)
# --------------------------------------------------------------------------


def _locate():
    """Return (folder_id, csv_file_id|None), creating the .experiment-namer
    folder if it doesn't exist yet. Raises BoxNotConfiguredError if Box/root
    aren't configured."""
    root = box_client._root_folder_id()
    folder_id = box_client.find_child_folder(root, INDEX_FOLDER_NAME)
    if folder_id is None:
        folder_id = box_client.create_child_folder(root, INDEX_FOLDER_NAME)
    csv_id = box_client.find_child_file(folder_id, INDEX_CSV_NAME)
    return folder_id, csv_id


def _load_locked():
    """Locate the index and return (folder_id, csv_id, rows). If the CSV is
    missing, backfill it from a live crawl and return the seeded rows."""
    folder_id, csv_id = _locate()
    if csv_id is None:
        rows = _backfill_rows()
        result = box_client.upload_file_text(
            folder_id, INDEX_CSV_NAME, _serialize(rows)
        )
        return folder_id, result["id"], rows
    rows = _parse(box_client.download_file_text(csv_id))
    return folder_id, csv_id, rows


def _write_locked(folder_id, csv_id, rows):
    """Upload `rows` as the CSV; returns the file id."""
    result = box_client.upload_file_text(
        folder_id, INDEX_CSV_NAME, _serialize(rows), existing_file_id=csv_id
    )
    return result["id"]


def _backfill_rows():
    """Crawl both directories and build a full set of rows (crawl-seeded)."""
    rows = []
    for folder in box_client.list_experiment_folders():
        if not detect_date(folder["name"]):
            continue
        rows.append(
            _row_from_folder(
                folder,
                BACKFILL,
                "",
                _associated_with(folder["id"]),
                _parent_experiment(folder["id"]),
            )
        )
    rows.sort(key=lambda r: r["experiment_name"])
    return rows


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def ensure_index():
    """Make sure the folder + CSV exist (backfill if the CSV is missing).
    No crawl when the CSV is already present."""
    with _lock:
        _load_locked()


def query_rows():
    """Return the current index rows, backfilling the CSV first if it's
    missing. The read side's entry point — one CSV download, no crawl once the
    index exists."""
    with _lock:
        _folder_id, _csv_id, rows = _load_locked()
        return rows


def taken_codenames():
    """The set of codename values already in the index (non-empty only), so
    `new` can re-roll a codename that's taken."""
    return {r["codename"] for r in query_rows() if r["codename"]}


def self_heal(folders):
    """Upsert crawled folder dicts into the index (by box_folder_id), so a
    fallback crawl leaves the CSV correct. Preserves any existing row's
    authored columns (created_by_slack_user, original_name); writes only if
    something actually changed."""
    if not folders:
        return
    with _lock:
        folder_id, csv_id, rows = _load_locked()
        by_id = {r["box_folder_id"]: r for r in rows}
        changed = False
        for f in folders:
            prev = by_id.get(f["id"])
            created_by = (prev.get("created_by_slack_user") or BACKFILL) if prev else BACKFILL
            original_name = prev.get("original_name", "") if prev else ""
            row = _row_from_folder(
                f,
                created_by,
                original_name,
                _associated_with(f["id"]),
                _parent_experiment(f["id"]),
            )
            if row != prev:
                by_id[f["id"]] = row
                changed = True
        if changed:
            _write_locked(folder_id, csv_id, list(by_id.values()))


def index_link():
    """Ensure the index exists and return the CSV's Box URL (for
    `/experiment database retrieve`)."""
    with _lock:
        _folder_id, csv_id, _rows = _load_locked()
        return box_client._file_url(csv_id)


def rebuild():
    """Full reconcile of PRIMARY (top-level) experiments against a live crawl
    (for `/experiment database update`).

    Adds top-level folders missing from the CSV, refreshes rows whose derived
    columns changed, and drops parentless rows whose folder no longer exists.
    Nested sub-experiments are NOT auto-added (they're opt-in), and existing
    sub-experiment rows (those with a `parent_experiment`) are PRESERVED — they
    aren't in the top-level crawl and are managed incrementally (create /
    delete / purge). Preserves human-authored columns a crawl can't know
    (created_by_slack_user, original_name). Returns
    {"added","removed","updated","total","url"}.
    """
    with _lock:
        folder_id, csv_id = _locate()
        existing = _parse(box_client.download_file_text(csv_id)) if csv_id else []
        by_id = {r["box_folder_id"]: r for r in existing}

        live = [
            f for f in box_client.list_experiment_folders() if detect_date(f["name"])
        ]

        added = updated = 0
        new_rows = []
        seen = set()
        for f in live:
            prev = by_id.get(f["id"])
            created_by = (prev.get("created_by_slack_user") or BACKFILL) if prev else BACKFILL
            original_name = prev.get("original_name", "") if prev else ""
            row = _row_from_folder(
                f,
                created_by,
                original_name,
                _associated_with(f["id"]),
                _parent_experiment(f["id"]),
            )
            new_rows.append(row)
            seen.add(f["id"])
            if prev is None:
                added += 1
            elif row != prev:
                updated += 1

        # Preserve existing sub-experiment rows (opt-in / nested): they aren't in
        # the top-level crawl, so a crawl-absence must not drop them.
        for r in existing:
            if r["box_folder_id"] not in seen and r["parent_experiment"]:
                new_rows.append(r)

        removed = sum(
            1
            for r in existing
            if r["box_folder_id"] not in seen and not r["parent_experiment"]
        )
        new_rows.sort(key=lambda r: r["experiment_name"])
        new_id = _write_locked(folder_id, csv_id, new_rows)
        return {
            "added": added,
            "removed": removed,
            "updated": updated,
            "total": len(new_rows),
            "url": box_client._file_url(new_id),
        }


def sync_created(folder, created_by, original_name="", associated_with="", parts=None):
    """Upsert the row for a just-created experiment folder (no crawl). `parts`
    (codename/category/action/num_scans/slack_user) is passed straight through
    from the create flow so the row's segments are exact rather than parsed."""
    with _lock:
        folder_id, csv_id, rows = _load_locked()
        rows = [r for r in rows if r["box_folder_id"] != folder["id"]]
        rows.append(
            _row_from_folder(
                folder, created_by, original_name, associated_with, parts=parts
            )
        )
        _write_locked(folder_id, csv_id, rows)


def sync_deleted(folder_ids):
    """Drop the rows for deleted folder id(s) (no crawl). Skips the write if
    nothing matched."""
    ids = {i for i in folder_ids if i}
    if not ids:
        return
    with _lock:
        folder_id, csv_id, rows = _load_locked()
        kept = [r for r in rows if r["box_folder_id"] not in ids]
        if len(kept) == len(rows):
            return
        _write_locked(folder_id, csv_id, kept)


def sync_renamed(folder, original_name, created_by=""):
    """Upsert the row for a renamed (legacy-converted) folder, recording its
    pre-rename name. The folder id is unchanged by a rename, so any existing
    row's associations are preserved. `created_by` names the converter and wins
    when given; otherwise an existing row's creator is kept, else "backfill"."""
    with _lock:
        folder_id, csv_id, rows = _load_locked()
        prev = next((r for r in rows if r["box_folder_id"] == folder["id"]), None)
        created_by = (
            created_by
            or (prev.get("created_by_slack_user") if prev else "")
            or BACKFILL
        )
        associated_with = prev.get("associated_with", "") if prev else _associated_with(folder["id"])
        parent_experiment = (
            prev.get("parent_experiment", "") if prev else _parent_experiment(folder["id"])
        )
        rows = [r for r in rows if r["box_folder_id"] != folder["id"]]
        rows.append(
            _row_from_folder(
                folder, created_by, original_name, associated_with, parent_experiment
            )
        )
        _write_locked(folder_id, csv_id, rows)
