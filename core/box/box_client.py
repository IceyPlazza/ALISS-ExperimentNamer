"""Box integration layer for ExperimentNamer.

Experiments live in one of TWO Box directories (see BOX_DIRECTORIES below);
every lookup searches both. Configuration comes from .env:

    BOX_AUTH_METHOD = dev_token | ccg
    BOX_DEVELOPER_TOKEN            (dev_token: 60-min token from the Box
                                    developer console — quick start/testing)
    BOX_CLIENT_ID / BOX_CLIENT_SECRET / BOX_ENTERPRISE_ID
                                   (ccg: production service account; needs
                                    admin authorization + the service
                                    account invited to the ARPA-H folders)
    BOX_SCANS_FOLDER_ID            folder id of "Data Collection and Scans"
    BOX_EXPERIMENTS_FOLDER_ID      folder id of "Experiments"
    BOX_ROOT_FOLDER_ID            folder id of the ARPA-H root (future home
                                    of experiment_index.csv)

Folder ids are the number in the browser URL when the folder is open:
https://vanderbilt.app.box.com/folder/<id>

Missing/incomplete config raises BoxNotConfiguredError, which main.py turns
into a friendly "not connected" Slack reply — so the app runs fine without
Box while this is being set up.

Current implementation crawls the directories live. The CSV index described
in CLAUDE.md (experiment_index.csv at the ARPA-H root) is the next layer:
read it first, crawl only on miss/mismatch, self-heal after crawling.

NOTE (legacy data): experiment folders created before this app existed use
adverb-verb names not drawn from unique-namer's vocabulary (e.g.
2025-10-08-bph-coolly-cut). Lookups match on literal folder names only.
"""

import io
import os
import re

# The two Box directories an experiment can live in. Keys are stable ids
# used in Slack button values and the CSV's directory_key column. Labels and
# paths are NOT hardcoded — they're resolved from Box via the configured
# folder ids (see directory_info), so pointing the *_FOLDER_ID env vars at a
# test folder makes every reply reflect the test folder truthfully. The
# fallback_label is only used to render Slack buttons while Box is
# unconfigured/unreachable. In production the ids point at:
#   scans       -> \Box\ARPA-H\Data Collection and Scans
#   experiments -> \Box\ARPA-H\Experiments
BOX_DIRECTORIES = {
    "scans": {
        "fallback_label": "Data Collection and Scans",
        "folder_id_env": "BOX_SCANS_FOLDER_ID",
    },
    "experiments": {
        "fallback_label": "Experiments",
        "folder_id_env": "BOX_EXPERIMENTS_FOLDER_ID",
    },
}


class BoxNotConfiguredError(RuntimeError):
    """Raised when the Box connection isn't configured (missing .env vars)."""


class AmbiguousExperimentError(LookupError):
    """A bare word-word combo matched more than one experiment.

    `candidates` holds the full names of every match so the user can retry
    with an exact name.
    """

    def __init__(self, candidates: list[str]):
        super().__init__(candidates)
        self.candidates = candidates


_client = None


def get_client():
    """Build (once) and return an authenticated BoxClient."""
    global _client
    if _client is not None:
        return _client

    from box_sdk_gen import (
        BoxCCGAuth,
        BoxClient,
        BoxDeveloperTokenAuth,
        CCGConfig,
    )

    method = os.environ.get("BOX_AUTH_METHOD", "").strip().lower()
    if method == "dev_token":
        token = os.environ.get("BOX_DEVELOPER_TOKEN", "").strip()
        if not token:
            raise BoxNotConfiguredError("BOX_DEVELOPER_TOKEN is not set")
        auth = BoxDeveloperTokenAuth(token=token)
    elif method == "ccg":
        client_id = os.environ.get("BOX_CLIENT_ID", "").strip()
        client_secret = os.environ.get("BOX_CLIENT_SECRET", "").strip()
        enterprise_id = os.environ.get("BOX_ENTERPRISE_ID", "").strip()
        if not (client_id and client_secret and enterprise_id):
            raise BoxNotConfiguredError(
                "BOX_CLIENT_ID / BOX_CLIENT_SECRET / BOX_ENTERPRISE_ID must "
                "all be set for ccg auth"
            )
        auth = BoxCCGAuth(
            config=CCGConfig(
                client_id=client_id,
                client_secret=client_secret,
                enterprise_id=enterprise_id,
            )
        )
    else:
        raise BoxNotConfiguredError(
            "BOX_AUTH_METHOD must be 'dev_token' or 'ccg'"
        )

    _client = BoxClient(auth=auth)
    return _client


def _directory_folder_id(dir_key: str) -> str:
    env_var = BOX_DIRECTORIES[dir_key]["folder_id_env"]
    folder_id = os.environ.get(env_var, "").strip()
    if not folder_id:
        raise BoxNotConfiguredError(f"{env_var} is not set")
    return folder_id


def _root_folder_id() -> str:
    """Folder id of the ARPA-H root — parent of the .experiment-namer folder
    that holds experiment_index.csv (see csv_index)."""
    folder_id = os.environ.get("BOX_ROOT_FOLDER_ID", "").strip()
    if not folder_id:
        raise BoxNotConfiguredError("BOX_ROOT_FOLDER_ID is not set")
    return folder_id


def _folder_url(folder_id: str) -> str:
    return f"https://app.box.com/folder/{folder_id}"


def _file_url(file_id: str) -> str:
    return f"https://app.box.com/file/{file_id}"


_directory_info_cache = {}


def directory_info(dir_key: str) -> dict:
    """Resolve {"label", "path"} for a directory from Box itself.

    label is the folder's actual name; path is rebuilt from its ancestry
    (e.g. \\Box\\ARPA-H\\Experiments). Cached after the first successful
    resolution. If Box is unconfigured/unreachable, returns the static
    fallback label (uncached) so Slack buttons still render.
    """
    if dir_key in _directory_info_cache:
        return _directory_info_cache[dir_key]
    try:
        client = get_client()
        folder = client.folders.get_folder_by_id(_directory_folder_id(dir_key))
        ancestors = [
            e.name
            for e in folder.path_collection.entries
            if e.name != "All Files"
        ]
        info = {
            "label": folder.name,
            "path": "\\".join(["\\Box", *ancestors, folder.name]),
        }
    except Exception:
        return {
            "label": BOX_DIRECTORIES[dir_key]["fallback_label"],
            "path": None,
        }
    _directory_info_cache[dir_key] = info
    return info


def _item_type(item) -> str:
    # SDK type fields are str-based enums; normalize to a plain string.
    return getattr(item.type, "value", str(item.type))


def _iter_subfolders_by_id(parent_id: str):
    """Yield every folder item directly inside a given folder id (paginated)."""
    client = get_client()
    offset = 0
    while True:
        page = client.folders.get_folder_items(parent_id, limit=1000, offset=offset)
        for item in page.entries:
            if _item_type(item) == "folder":
                yield item
        offset += len(page.entries)
        if offset >= page.total_count or not page.entries:
            return


def _iter_subfolders(dir_key: str):
    """Yield every folder item directly inside a directory (top level only)."""
    yield from _iter_subfolders_by_id(_directory_folder_id(dir_key))


def _iter_experiment_folders(dir_key: str):
    """Yield (folder_item, is_nested) for a directory, INCLUDING sub-experiments
    nested inside other experiment folders.

    Top-level folders are all yielded (is_nested=False; callers filter
    non-experiments by name). We then descend, but ONLY through experiment-named
    folders (a detectable date in the name), and yield only the nested folders
    that are themselves experiment-named (is_nested=True) — so arbitrary data
    subfolders are never surfaced as experiments."""
    from core.slack.naming import detect_date

    tops = list(_iter_subfolders(dir_key))
    for item in tops:
        yield item, False
    stack = [it for it in tops if detect_date(it.name)]
    while stack:
        parent = stack.pop()
        for child in _iter_subfolders_by_id(parent.id):
            if detect_date(child.name):  # a nested sub-experiment
                yield child, True
                stack.append(child)  # allow deeper nesting


def _experiment_dict(item, dir_key: str) -> dict:
    info = directory_info(dir_key)
    return {
        "id": item.id,
        "name": item.name,
        "url": _folder_url(item.id),
        "directory": info["label"],
        "dir_key": dir_key,
        "path": (info["path"] or info["label"]) + "\\" + item.name,
    }


# --------------------------------------------------------------------------
# Lookups — CSV-index-first (read experiment_index.csv, filter in memory), with
# a live crawl fallback. Callers see the same signatures/exceptions as the old
# crawl-only versions; the crawl bodies live on as the _crawl_* fallbacks.
# --------------------------------------------------------------------------


def _row_to_experiment(row: dict) -> dict:
    """Map a CSV index row to the experiment dict callers expect. The
    directory label/full path are resolved (cached) from Box via
    directory_info — not a crawl."""
    dir_key = row["directory_key"]
    info = directory_info(dir_key)
    name = row["experiment_name"]
    return {
        "id": row["box_folder_id"],
        "name": name,
        "url": row["box_url"] or _folder_url(row["box_folder_id"]),
        "directory": info["label"],
        "dir_key": dir_key,
        "path": (info["path"] or info["label"]) + "\\" + name,
        # "; "-joined association labels (empty if none) — lets listings flag
        # experiments that have associated experiments.
        "associated_with": row["associated_with"],
        # The parent experiment's name, if this is a sub-experiment (empty
        # otherwise) — lets listings/track flag the parent/child relationship.
        "parent_experiment": row.get("parent_experiment", ""),
    }


def _mark_relationships(results: list[dict], all_rows: list[dict]) -> list[dict]:
    """Flag each result with has_parent / has_children, computed against the
    FULL index (a child may have children that fall outside the current
    filter). Lets listings note parent/child experiments."""
    parent_names = {r["parent_experiment"] for r in all_rows if r.get("parent_experiment")}
    for e in results:
        e["has_parent"] = bool(e.get("parent_experiment"))
        e["has_children"] = e["name"] in parent_names
    return results


def _index_rows():
    """Current index rows, or None if the index isn't usable (root not
    configured) so the caller can fall back to a crawl."""
    from core.box import csv_index

    try:
        return csv_index.query_rows()
    except BoxNotConfiguredError:
        return None


def find_experiment_folder(experiment_name: str) -> dict:
    """Locate the Box folder for an experiment, searching both directories.

    `experiment_name` is either a full name
    (2026-07-06-decorous-harbor-BPH-seg-iven.chen) or a bare codename
    (decorous-harbor); a codename matches the codename part of the name in
    either directory.

    Reads the CSV index first; on a miss, falls back to a live crawl and
    self-heals the index with what it finds. Raises: KeyError if no folder
    matches, AmbiguousExperimentError if a codename matches several.
    """
    from core.slack.naming import EXPERIMENT_NAME_RE

    rows = _index_rows()
    if rows is not None:
        is_full_name = bool(EXPERIMENT_NAME_RE.match(experiment_name))
        matches = [
            _row_to_experiment(r)
            for r in rows
            if (
                r["experiment_name"] == experiment_name
                if is_full_name
                else r["codename"] == experiment_name
            )
        ]
        if len(matches) > 1:
            raise AmbiguousExperimentError([m["name"] for m in matches])
        if matches:
            return matches[0]

    # Miss (or unusable index): crawl live, then heal the index.
    folder = _crawl_find_experiment_folder(experiment_name)
    from core.box import csv_index

    try:
        csv_index.self_heal([folder])
    except Exception:
        pass  # best-effort; a heal failure must not fail the lookup
    return folder


def _crawl_find_experiment_folder(experiment_name: str) -> dict:
    from core.slack.naming import EXPERIMENT_NAME_RE

    is_full_name = bool(EXPERIMENT_NAME_RE.match(experiment_name))
    combo_re = re.compile(r"-" + re.escape(experiment_name) + r"($|-)")
    matches = []
    for dir_key in BOX_DIRECTORIES:
        for item, _nested in _iter_experiment_folders(dir_key):
            if is_full_name:
                if item.name == experiment_name:
                    matches.append(_experiment_dict(item, dir_key))
            elif combo_re.search(item.name):
                matches.append(_experiment_dict(item, dir_key))
    if not matches:
        raise KeyError(experiment_name)
    if len(matches) > 1:
        raise AmbiguousExperimentError([m["name"] for m in matches])
    return matches[0]


def list_experiments_by_date(date_str: str) -> list[dict]:
    """List experiment folders (from both directories) whose name starts
    with the given YYYY-MM-DD. Reads the CSV index (crawls only if the index
    isn't usable). Non-experiment siblings are excluded by construction."""
    rows = _index_rows()
    if rows is None:
        return _crawl_experiments_by_date(date_str)
    return _mark_relationships(
        [
            _row_to_experiment(r)
            for r in rows
            if r["experiment_name"].startswith(date_str)
        ],
        rows,
    )


def _crawl_experiments_by_date(date_str: str) -> list[dict]:
    # Listings cover primary (top-level) experiments; nested sub-experiments
    # live under their parent (seen via track), so this stays top-level.
    return [
        _experiment_dict(item, dir_key)
        for dir_key in BOX_DIRECTORIES
        for item in _iter_subfolders(dir_key)
        if item.name.startswith(date_str)
    ]


def list_experiments_by_category(
    category: str | None = None, dir_key: str | None = None
) -> list[dict]:
    """List experiment folders, optionally filtered by category and/or
    directory. Reads the CSV index (crawls only if the index isn't usable).

    `category` (a code like "bph") keeps only that category; None keeps every
    folder. `dir_key` (a BOX_DIRECTORIES key) scopes to one directory; None
    searches both. With both None this lists every indexed experiment."""
    rows = _index_rows()
    if rows is None:
        return _crawl_experiments_by_category(category, dir_key)
    return _mark_relationships(
        [
            _row_to_experiment(r)
            for r in rows
            if (category is None or r["category"] == category)
            and (dir_key is None or r["directory_key"] == dir_key)
        ],
        rows,
    )


def _crawl_experiments_by_category(
    category: str | None = None, dir_key: str | None = None
) -> list[dict]:
    dir_keys = [dir_key] if dir_key else list(BOX_DIRECTORIES)
    # Category appears uppercased in new names (-BPH-) and lowercased in legacy
    # ones (-bph-); match case-insensitively. Top-level only (see by_date).
    return [
        _experiment_dict(item, dk)
        for dk in dir_keys
        for item in _iter_subfolders(dk)
        if category is None or f"-{category}-" in item.name.lower()
    ]


def list_experiment_folders(include_nested: bool = False) -> list[dict]:
    """List experiment folders in both directories.

    By default lists only the TOP LEVEL of each directory (primary experiments)
    — this is what backfill/reconcile index. Pass `include_nested=True` to also
    recurse into experiment-named folders and include nested sub-experiments
    (used by `delete empty` and reference-purging, which must reach them)."""
    out = []
    for dir_key in BOX_DIRECTORIES:
        if include_nested:
            for item, nested in _iter_experiment_folders(dir_key):
                out.append(_experiment_dict(item, dir_key))
        else:
            for item in _iter_subfolders(dir_key):
                out.append(_experiment_dict(item, dir_key))
    return out


def list_child_folders(parent_id: str) -> list[dict]:
    """List the immediate subfolders of a folder as {id, name, url} dicts.
    Used by legacy child pickup to find the experiments nested in a parent."""
    return [
        {"id": item.id, "name": item.name, "url": _folder_url(item.id)}
        for item in _iter_subfolders_by_id(parent_id)
    ]


def get_folder_name(folder_id: str) -> str:
    """Return a folder's display name (labels association links)."""
    client = get_client()
    return client.folders.get_folder_by_id(folder_id).name


def get_folder_description(folder_id: str) -> str:
    """Return a folder's description (where associations are stored), or ""
    if it has none."""
    client = get_client()
    return client.folders.get_folder_by_id(folder_id).description or ""


def set_folder_description(folder_id: str, description: str) -> None:
    """Write a folder's description (used to persist experiment
    associations)."""
    client = get_client()
    client.folders.update_folder_by_id(folder_id, description=description)


def get_folder_info(folder_id: str) -> dict:
    """Return {"name", "parent_id"} for a folder (used by /experiment
    legacy to inspect the folder behind a pasted link)."""
    client = get_client()
    folder = client.folders.get_folder_by_id(folder_id)
    parent = getattr(folder, "parent", None)
    return {"name": folder.name, "parent_id": parent.id if parent else None}


def directory_key_for_parent(parent_id: str) -> str | None:
    """Map a parent folder id to its BOX_DIRECTORIES key, or None if the
    folder isn't directly inside either experiment directory."""
    for dir_key in BOX_DIRECTORIES:
        try:
            if _directory_folder_id(dir_key) == parent_id:
                return dir_key
        except BoxNotConfiguredError:
            continue
    return None


def rename_folder(folder_id: str, new_name: str) -> dict:
    """Rename a folder in place; returns {"id", "name", "url"}."""
    client = get_client()
    folder = client.folders.update_folder_by_id(folder_id, name=new_name)
    return {"id": folder.id, "name": folder.name, "url": _folder_url(folder.id)}


def folder_has_files(folder_id: str) -> bool:
    """True if the experiment folder contains any files/subfolders."""
    client = get_client()
    page = client.folders.get_folder_items(folder_id, limit=1)
    return page.total_count > 0


def delete_experiment_folder(folder_id: str) -> None:
    """Delete an experiment folder. Non-recursive on purpose: Box refuses
    if the folder isn't empty, a second safety net behind the
    folder_has_files() check in main.py."""
    client = get_client()
    client.folders.delete_folder_by_id(folder_id)


def create_experiment_folder(
    experiment_name: str,
    directory_key: str,
    description: str = "",
    parent_folder_id: str | None = None,
) -> dict:
    """Create the Box folder for a newly generated experiment name.

    By default the folder is created at the top level of the chosen directory
    ("scans" or "experiments"). Pass `parent_folder_id` to nest it inside an
    existing folder instead (used for sub-experiments nested under their
    parent); `directory_key` is still recorded as the folder's logical
    directory. `description` seeds the folder's Box description — used to persist
    the experiment's associations / parent link (see the experiments module)."""
    from box_sdk_gen import CreateFolderParent

    client = get_client()
    parent_id = parent_folder_id or _directory_folder_id(directory_key)
    folder = client.folders.create_folder(
        name=experiment_name, parent=CreateFolderParent(id=parent_id)
    )
    if description:
        client.folders.update_folder_by_id(folder.id, description=description)
    return _experiment_dict(folder, directory_key)


# --------------------------------------------------------------------------
# Generic folder/file helpers — used by csv_index to store experiment_index.csv
# inside a .experiment-namer folder. Kept generic (no index-specific naming) so
# box_client stays a thin Box-API layer.
# --------------------------------------------------------------------------


def _iter_children(parent_id: str):
    """Yield every item (files and folders) directly inside a folder."""
    client = get_client()
    offset = 0
    while True:
        page = client.folders.get_folder_items(parent_id, limit=1000, offset=offset)
        for item in page.entries:
            yield item
        offset += len(page.entries)
        if offset >= page.total_count or not page.entries:
            return


def find_child_folder(parent_id: str, name: str) -> str | None:
    """Return the id of a subfolder named `name`, or None if absent."""
    for item in _iter_children(parent_id):
        if _item_type(item) == "folder" and item.name == name:
            return item.id
    return None


def create_child_folder(parent_id: str, name: str) -> str:
    """Create a subfolder and return its id."""
    from box_sdk_gen import CreateFolderParent

    client = get_client()
    folder = client.folders.create_folder(
        name=name, parent=CreateFolderParent(id=parent_id)
    )
    return folder.id


def find_child_file(parent_id: str, name: str) -> str | None:
    """Return the id of a file named `name` directly inside a folder, or
    None if absent."""
    for item in _iter_children(parent_id):
        if _item_type(item) == "file" and item.name == name:
            return item.id
    return None


def download_file_text(file_id: str) -> str:
    """Download a (text) file's full contents as a UTF-8 string."""
    client = get_client()
    stream = client.downloads.download_file(file_id)
    return stream.read().decode("utf-8")


def upload_file_text(
    parent_id: str, name: str, text: str, existing_file_id: str | None = None
) -> dict:
    """Upload `text` as a file. Creates a new file under `parent_id`, or a new
    version of `existing_file_id` when given. Returns {"id", "url"}."""
    from box_sdk_gen import (
        UploadFileAttributes,
        UploadFileAttributesParentField,
        UploadFileVersionAttributes,
    )

    client = get_client()
    data = io.BytesIO(text.encode("utf-8"))
    if existing_file_id:
        files = client.uploads.upload_file_version(
            existing_file_id,
            attributes=UploadFileVersionAttributes(name=name),
            file=data,
        )
    else:
        files = client.uploads.upload_file(
            attributes=UploadFileAttributes(
                name=name, parent=UploadFileAttributesParentField(id=parent_id)
            ),
            file=data,
        )
    uploaded = files.entries[0]
    return {"id": uploaded.id, "url": _file_url(uploaded.id)}
