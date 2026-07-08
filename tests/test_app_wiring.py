"""Tests that the Bolt handlers are wired up correctly.

`register(app)` should attach the slash command, the multi-step "new" action
handlers, and the two modal-submit views. We use a fake App that records
every registration so we can assert the wiring without real Slack tokens.
"""

import json
import re

from core.box import box_client
from core.slack import handlers as handlers_mod
from core.slack.commands import SUBCOMMANDS
from core.slack.handlers import register


class FakeApp:
    """Records handler registrations instead of talking to Slack."""

    def __init__(self):
        self.commands = {}
        self.actions = []  # list of (matcher, fn)
        self.views = {}

    def command(self, name):
        def deco(fn):
            self.commands[name] = fn
            return fn

        return deco

    def action(self, matcher):
        def deco(fn):
            self.actions.append((matcher, fn))
            return fn

        return deco

    def view(self, callback_id):
        def deco(fn):
            self.views[callback_id] = fn
            return fn

        return deco


def _action_matches(app, action_id):
    """True if some registered action matcher accepts `action_id`."""
    for matcher, _ in app.actions:
        if isinstance(matcher, str) and matcher == action_id:
            return True
        if isinstance(matcher, re.Pattern) and matcher.match(action_id):
            return True
    return False


def test_register_returns_app():
    app = FakeApp()
    assert register(app) is app


def test_slash_command_registered():
    app = FakeApp()
    register(app)
    assert "/experiment" in app.commands


def test_views_registered():
    app = FakeApp()
    register(app)
    assert set(app.views) == {
        "new_experiment_details",
        "legacy_experiment",
        "associate_legacy",
        "subexperiment",
    }


def test_new_flow_actions_registered():
    from core.slack.naming import EXPERIMENT_CATEGORIES

    app = FakeApp()
    register(app)
    category_actions = [f"pick_category_{c}" for c in EXPERIMENT_CATEGORIES]
    for action_id in [
        *category_actions,
        "pick_directory_scans",
        "pick_directory_experiments",
    ]:
        assert _action_matches(app, action_id), action_id


def test_all_subcommands_present():
    assert set(SUBCOMMANDS) == {
        "new",
        "subexperiment",
        "track",
        "date",
        "delete",
        "category",
        "scans",
        "experiments",
        "legacy",
        "database",
        "help",
    }


def test_associate_legacy_submit_clears_stack_and_creates_once(monkeypatch):
    """Regression: submitting the pushed legacy dialog must close the WHOLE
    modal stack (response_action='clear') and create exactly one experiment —
    a plain ack() left the parent details modal open and allowed a duplicate.
    """
    app = FakeApp()
    register(app)
    submit = app.views["associate_legacy"]

    # Stub out network / Box side effects.
    monkeypatch.setattr(
        handlers_mod,
        "WebhookClient",
        lambda url: type("W", (), {"send": lambda self, **k: None})(),
    )
    monkeypatch.setattr(
        box_client, "directory_info", lambda k: {"label": "Experiments", "path": None}
    )
    created = []

    def fake_create(name, dir_key, description=""):
        created.append(name)
        return {"name": name, "url": "https://app.box.com/folder/99"}

    monkeypatch.setattr(box_client, "create_experiment_folder", fake_create)
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: "")
    monkeypatch.setattr(box_client, "set_folder_description", lambda fid, d: None)

    client = type("C", (), {"chat_postMessage": lambda self, **kw: None})()
    acks = []

    meta = {
        "value": "bph:experiments",
        "user": "U1",
        "user_name": "iven.chen",
        "channel": "C1",
        "response_url": "http://example.invalid/hook",
        "codename": "",
        "action": "segmentation",
        "num_scans": None,
        "ready": [],
        "legacy": [
            {
                "folder_id": "5",
                "old_name": "2026-06-17 - FLR: CT",
                "url": "https://app.box.com/folder/5",
                "detected_date": "2026-06-17",
                "detected_category": "bph",
            }
        ],
    }
    # "keep as link" (rename unchecked) so no convert_legacy_folder / real rename.
    view = {
        "private_metadata": json.dumps(meta),
        "state": {
            "values": {
                "act_0": {"act": {"selected_options": []}},
                "date_0": {"date": {}},
                "cat_0": {"cat": {}},
            }
        },
    }
    submit(ack=lambda **kw: acks.append(kw), view=view, client=client)

    assert acks == [{"response_action": "clear"}]  # whole stack closed
    assert len(created) == 1  # exactly one experiment, no duplicate


def test_main_module_imports(monkeypatch):
    """main.py builds the App and registers handlers on import; with fake
    tokens and verification disabled it must import without network."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_TOKEN_VERIFY", "0")
    import importlib

    import main

    importlib.reload(main)
    assert main.app is not None


def test_subexperiment_submit_creates_and_links(monkeypatch):
    """Submitting the sub-experiment dialog creates the child folder (nested
    under the parent) and writes the parent->child link both ways."""
    app = FakeApp()
    register(app)
    submit = app.views["subexperiment"]

    monkeypatch.setattr(
        handlers_mod,
        "WebhookClient",
        lambda url: type("W", (), {"send": lambda self, **k: None})(),
    )
    monkeypatch.setattr(
        box_client, "directory_info", lambda k: {"label": k.capitalize(), "path": None}
    )
    # parent link resolves to a top-level Experiments folder named to the scheme
    monkeypatch.setattr(
        box_client,
        "get_folder_info",
        lambda fid: {"name": "2026-07-06-decorous-harbor-BPH-seg-iven.chen", "parent_id": "DIR"},
    )
    monkeypatch.setattr(
        box_client, "directory_key_for_parent", lambda pid: "experiments" if pid == "DIR" else None
    )
    created = []

    def fake_create(name, dir_key, description="", parent_folder_id=None):
        created.append((name, parent_folder_id))
        return {"id": "CHILD", "name": name, "url": "https://app.box.com/folder/CHILD"}

    monkeypatch.setattr(box_client, "create_experiment_folder", fake_create)
    store = {"5": ""}
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: store.get(fid, ""))
    monkeypatch.setattr(box_client, "set_folder_description", store.__setitem__)

    client = type("C", (), {"chat_postMessage": lambda self, **kw: None})()
    acks = []
    meta = {
        "user": "U1",
        "user_name": "jane.doe",
        "channel": "C1",
        "response_url": "http://example.invalid/hook",
    }
    view = {
        "private_metadata": json.dumps(meta),
        "state": {
            "values": {
                "parent_link": {"link_input": {"value": "https://app.box.com/folder/5"}},
                "codename": {"codename_input": {"value": "myco"}},
                "date": {"date_input": {"selected_date": "2026-08-01"}},  # later than parent
                "action": {"action_input": {"value": "ablation"}},
                "placement": {"placement_input": {"selected_option": {"value": "nested"}}},
            }
        },
    }
    submit(ack=lambda **kw: acks.append(kw), view=view, client=client)

    assert acks == [{}]  # success ack (no response_action)
    assert len(created) == 1
    name, parent_folder_id = created[0]
    assert parent_folder_id == "5"  # nested under the parent folder
    # date is the child's own (later); category inherited from the parent (BPH)
    assert name == "2026-08-01-myco-BPH-ablation-jane.doe"
    # the parent folder got a Sub-experiments back-reference to the child
    assert "Sub-experiments:" in store["5"]
    assert "CHILD" in store["5"]


def test_subexperiment_submit_refuses_uncategorized_parent(monkeypatch):
    # The child inherits the parent's category — there's no category field. If
    # the parent's category can't be read, we refuse (can't guarantee a match).
    app = FakeApp()
    register(app)
    submit = app.views["subexperiment"]
    monkeypatch.setattr(
        box_client,
        "get_folder_info",
        lambda fid: {"name": "20260623_PerceptionTuesday", "parent_id": "DIR"},  # no category
    )
    monkeypatch.setattr(
        box_client, "directory_key_for_parent", lambda pid: "experiments" if pid == "DIR" else None
    )
    acks = []
    meta = {"user": "U1", "user_name": "jane.doe", "channel": "C1", "response_url": ""}
    view = {
        "private_metadata": json.dumps(meta),
        "state": {
            "values": {
                "parent_link": {"link_input": {"value": "https://app.box.com/folder/5"}},
                "codename": {"codename_input": {"value": ""}},
                "date": {"date_input": {}},
                "action": {"action_input": {"value": "ablation"}},
                "placement": {"placement_input": {"selected_option": {"value": "nested"}}},
            }
        },
    }
    submit(ack=lambda **kw: acks.append(kw), view=view, client=None)
    assert acks[-1]["response_action"] == "errors"
    assert "parent_link" in acks[-1]["errors"]


def test_legacy_submit_picks_up_children_skipping_calibration(monkeypatch):
    """Legacy convert with 'register children' checked links the non-calibration
    subfolders as sub-experiments of the renamed parent."""
    from core.slack.experiments import parse_children, parse_parent

    app = FakeApp()
    register(app)
    submit = app.views["legacy_experiment"]

    monkeypatch.setattr(
        box_client, "directory_info", lambda k: {"label": "Experiments", "path": None}
    )
    monkeypatch.setattr(
        box_client,
        "get_folder_info",
        lambda fid: {"name": "20260623_PerceptionTuesday", "parent_id": "DIR"},
    )
    monkeypatch.setattr(
        box_client, "directory_key_for_parent", lambda pid: "experiments" if pid == "DIR" else None
    )
    monkeypatch.setattr(
        box_client,
        "rename_folder",
        lambda fid, new: {"id": fid, "name": new, "url": f"https://app.box.com/folder/{fid}"},
    )
    monkeypatch.setattr(
        box_client,
        "list_child_folders",
        lambda pid: [
            {"id": "501", "name": "Underwater_1", "url": "https://app.box.com/folder/501"},
            {"id": "777", "name": "calibration", "url": "https://app.box.com/folder/777"},
            {"id": "502", "name": "On_Air_long", "url": "https://app.box.com/folder/502"},
        ],
    )
    store = {"501": "", "502": "", "777": "", "900": ""}
    monkeypatch.setattr(box_client, "get_folder_description", lambda fid: store.get(fid, ""))
    monkeypatch.setattr(box_client, "set_folder_description", store.__setitem__)

    posts = []
    client = type("C", (), {"chat_postMessage": lambda self, **kw: posts.append(kw)})()
    acks = []
    meta = {"user": "U1", "user_name": "iven.chen", "channel": "C1"}
    view = {
        "private_metadata": json.dumps(meta),
        "state": {
            "values": {
                "link": {"link_input": {"value": "https://app.box.com/folder/900"}},
                "date": {"date_input": {"selected_date": "2026-06-23"}},
                "category": {"category_input": {"selected_option": {"value": "bph"}}},
                "action": {"action_input": {"value": "perception"}},
                "scan_count": {"count_input": {}},
                "register_children": {"register": {"selected_options": [{"value": "yes"}]}},
                "index_children": {"index": {"selected_options": []}},
            }
        },
    }
    submit(ack=lambda **kw: acks.append(kw), view=view, client=client)

    # non-calibration children linked back to the renamed parent
    assert parse_parent(store["501"]) is not None
    assert parse_parent(store["502"]) is not None
    assert parse_parent(store["777"]) is None  # calibration skipped
    # renamed parent (folder 900) lists exactly the two real children
    assert {c["label"] for c in parse_children(store["900"])} == {"Underwater_1", "On_Air_long"}
    # a follow-up message reports the count
    assert any("Linked 2" in (p.get("text") or "") for p in posts)
