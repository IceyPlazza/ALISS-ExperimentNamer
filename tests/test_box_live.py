"""Live Box integration tests — hit the real Box API.

Opt-in only: run with `--run-box-live` and BOX_* configured in .env, e.g.

    .venv\\Scripts\\python.exe -m pytest tests/test_box_live.py --run-box-live -v

These create and delete folders, so point the *_FOLDER_ID vars at a SANDBOX
folder tree, not the real ARPA-H folders. Every folder made here carries a
"-pytesttmp" codename suffix and is cleaned up in fixture teardown; a stray
one just means a run was interrupted.

dev_token auth expires after 60 minutes — if the token is stale these tests
skip with a clear message rather than erroring. Generate a fresh token and
re-run.
"""

import os

import pytest
from dotenv import load_dotenv

from core.box import box_client
from core.box.box_client import BOX_DIRECTORIES
from core.slack.experiments import (
    add_back_references,
    associations_to_description,
    parse_associations,
    purge_references,
)
from core.slack.naming import generate_experiment_name

pytestmark = pytest.mark.box_live

# A codename suffix marking every folder these tests create, so leaked
# folders are obvious in the sandbox and never collide with real data.
TEST_SUFFIX = "-pytesttmp"


def _missing_box_config():
    """Human-readable reason BOX_* isn't configured enough to connect, or
    None if it is. Keeps the skip reason crisp and independent of the SDK, so
    on a machine with no .env (e.g. public CI) these tests skip cleanly rather
    than failing."""
    load_dotenv()
    method = os.environ.get("BOX_AUTH_METHOD", "").strip().lower()
    if method == "dev_token":
        if not os.environ.get("BOX_DEVELOPER_TOKEN", "").strip():
            return "BOX_DEVELOPER_TOKEN is not set"
        return None
    if method == "ccg":
        missing = [
            k
            for k in ("BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "BOX_ENTERPRISE_ID")
            if not os.environ.get(k, "").strip()
        ]
        return f"missing {', '.join(missing)}" if missing else None
    return "BOX_AUTH_METHOD is not set to 'dev_token' or 'ccg'"


@pytest.fixture(scope="module")
def box():
    """Confirm Box is configured AND reachable; skip (don't fail) otherwise so
    `--run-box-live` degrades gracefully when there's no .env or the dev token
    has expired."""
    reason = _missing_box_config()
    if reason:
        pytest.skip(f"Box not configured ({reason}) - set BOX_* in .env for live tests")
    try:
        box_client.get_client().users.get_user_me()  # real authed round-trip
    except Exception as e:  # expired dev token, network, permissions…
        pytest.skip(f"Box not reachable (expired token?): {e}")
    return box_client


@pytest.fixture
def temp_experiment(box, request):
    """Create a uniquely-named empty experiment folder in the requested
    directory (parametrize indirectly with a dir_key) and delete it on
    teardown. Yields the {'id','name','url','directory','path','dir_key'}."""
    dir_key = request.param
    name = generate_experiment_name("bph") + TEST_SUFFIX
    folder = box.create_experiment_folder(name, dir_key)
    try:
        yield {**folder, "dir_key": dir_key}
    finally:
        try:
            box.delete_experiment_folder(folder["id"])
        except Exception:
            pass  # already deleted by the test, or gone — teardown is best-effort


def test_auth_whoami(box):
    me = box.get_client().users.get_user_me()
    assert me.id
    print(f"\nConnected to Box as: {me.name} <{me.login}>")


@pytest.mark.parametrize("dir_key", list(BOX_DIRECTORIES))
def test_directory_info_resolves(box, dir_key):
    """Both directories resolve to a real label + path from Box (not the
    static fallback)."""
    info = box.directory_info(dir_key)
    assert info["label"]
    assert info["path"], "path should resolve from folder ancestry"
    assert info["path"].startswith("\\Box"), info["path"]
    print(f"\n[{dir_key}] {info['label']} -> {info['path']}")


def test_list_experiment_folders_read_only(box):
    """Listing every experiment folder across both directories succeeds and
    returns well-formed dicts (read-only; tolerates an empty sandbox)."""
    folders = box.list_experiment_folders()
    assert isinstance(folders, list)
    for f in folders:
        assert {"id", "name", "url", "directory", "path"} <= f.keys()


def test_lookup_by_date_and_category_read_only(box):
    """Date/category lookups run without error against live data."""
    assert isinstance(box.list_experiments_by_date("2026-07-05"), list)
    assert isinstance(box.list_experiments_by_category("bph"), list)


@pytest.mark.parametrize("temp_experiment", list(BOX_DIRECTORIES), indirect=True)
def test_create_find_delete_roundtrip(box, temp_experiment):
    """Full lifecycle in each directory: a freshly created folder is empty,
    findable by its full name in the right directory, and gone after
    deletion."""
    name = temp_experiment["name"]

    # Freshly created → no files.
    assert box.folder_has_files(temp_experiment["id"]) is False

    # Findable by full name, reporting the directory it was created in.
    found = box.find_experiment_folder(name)
    assert found["id"] == temp_experiment["id"]
    expected_label = box.directory_info(temp_experiment["dir_key"])["label"]
    assert found["directory"] == expected_label

    # Delete it and confirm it's really gone (teardown then no-ops).
    box.delete_experiment_folder(temp_experiment["id"])
    with pytest.raises(KeyError):
        box.find_experiment_folder(name)


def test_association_description_round_trip(box):
    """Associations written to a folder's Box description survive a real
    write/read round-trip and parse back correctly."""
    dir_key = "experiments"
    name = generate_experiment_name("bph") + TEST_SUFFIX
    assocs = [
        {"label": "2026-01-01-cao-x-y", "url": "https://app.box.com/folder/1"},
        {"label": "coolly-cut", "url": None},
    ]
    folder = box.create_experiment_folder(
        name, dir_key, description=associations_to_description(assocs)
    )
    try:
        parsed = parse_associations(box.get_folder_description(folder["id"]))
        assert parsed == [
            {"label": "2026-01-01-cao-x-y", "url": "https://app.box.com/folder/1"},
            {"label": "coolly-cut", "url": None},
        ]
    finally:
        box.delete_experiment_folder(folder["id"])


def test_bidirectional_association_round_trip(box):
    """Associating X→Y writes a back-reference X onto Y's folder, and removing
    it (as on delete) strips it again — all against real Box folders."""
    y_name = generate_experiment_name("cao") + TEST_SUFFIX
    y = box.create_experiment_folder(y_name, "experiments")
    x_name = generate_experiment_name("bph") + TEST_SUFFIX
    associations = [{"label": y_name, "url": y["url"], "mrkdwn": f"<{y['url']}|{y_name}>"}]
    x = box.create_experiment_folder(
        x_name, "experiments", description=associations_to_description(associations)
    )
    try:
        # forward link on X…
        assert any(
            a["label"] == y_name
            for a in parse_associations(box.get_folder_description(x["id"]))
        )
        # …and the back-reference lands on Y
        add_back_references(x, associations)
        y_assocs = parse_associations(box.get_folder_description(y["id"]))
        assert any(a["label"] == x_name for a in y_assocs)

        # deleting X (simulated via purge) strips the reference from Y, even
        # though we don't rely on X's own association list
        purge_references({x["id"]})
        assert not parse_associations(box.get_folder_description(y["id"]))
    finally:
        box.delete_experiment_folder(x["id"])
        box.delete_experiment_folder(y["id"])
