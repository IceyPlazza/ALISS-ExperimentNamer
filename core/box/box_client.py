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


def _folder_url(folder_id: str) -> str:
    return f"https://app.box.com/folder/{folder_id}"


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


def _iter_subfolders(dir_key: str):
    """Yield every folder item directly inside a directory (paginated)."""
    client = get_client()
    folder_id = _directory_folder_id(dir_key)
    offset = 0
    while True:
        page = client.folders.get_folder_items(
            folder_id, limit=1000, offset=offset
        )
        for item in page.entries:
            if _item_type(item) == "folder":
                yield item
        offset += len(page.entries)
        if offset >= page.total_count or not page.entries:
            return


def _experiment_dict(item, dir_key: str) -> dict:
    info = directory_info(dir_key)
    return {
        "id": item.id,
        "name": item.name,
        "url": _folder_url(item.id),
        "directory": info["label"],
        "path": (info["path"] or info["label"]) + "\\" + item.name,
    }


def find_experiment_folder(experiment_name: str) -> dict:
    """Locate the Box folder for an experiment, searching both directories.

    `experiment_name` is either a full name (2026-07-04-bph-decorous-harbor)
    or a bare word-word combo (decorous-harbor); combos match the combo part
    of the name, including names with a scan-count/codename suffix
    (2026-07-05-bph-sunny-harbor-(3) matches combo sunny-harbor).

    Raises: KeyError if no folder matches, AmbiguousExperimentError if a
    combo matches several.
    """
    is_full_name = experiment_name[0].isdigit()
    combo_re = re.compile(r"-" + re.escape(experiment_name) + r"($|-)")
    matches = []
    for dir_key in BOX_DIRECTORIES:
        for item in _iter_subfolders(dir_key):
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
    with the given YYYY-MM-DD. Non-folder siblings (e.g. annotation.json
    files) are excluded by construction."""
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
    directory.

    `category` (a code like "bph") keeps only names matching *-bph-*; None
    keeps every folder. `dir_key` (a BOX_DIRECTORIES key, "scans"/
    "experiments") scopes the crawl to that one directory; None searches both.
    With both None this lists every experiment in both directories."""
    dir_keys = [dir_key] if dir_key else list(BOX_DIRECTORIES)
    return [
        _experiment_dict(item, dk)
        for dk in dir_keys
        for item in _iter_subfolders(dk)
        if category is None or f"-{category}-" in item.name
    ]


def list_experiment_folders() -> list[dict]:
    """List every experiment folder in both directories, all
    dates/categories. Used by `/experiment delete empty`."""
    return [
        _experiment_dict(item, dir_key)
        for dir_key in BOX_DIRECTORIES
        for item in _iter_subfolders(dir_key)
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
    experiment_name: str, directory_key: str, description: str = ""
) -> dict:
    """Create the Box folder for a newly generated experiment name inside
    the chosen directory ("scans" or "experiments").

    `description` seeds the folder's Box description — used to persist the
    experiment's associations (see the experiments module)."""
    from box_sdk_gen import CreateFolderParent

    client = get_client()
    parent_id = _directory_folder_id(directory_key)
    folder = client.folders.create_folder(
        name=experiment_name, parent=CreateFolderParent(id=parent_id)
    )
    if description:
        client.folders.update_folder_by_id(folder.id, description=description)
    return _experiment_dict(folder, directory_key)
