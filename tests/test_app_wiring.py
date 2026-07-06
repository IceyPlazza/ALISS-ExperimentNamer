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
    }


def test_new_flow_actions_registered():
    app = FakeApp()
    register(app)
    for action_id in [
        "pick_category_bph",
        "pick_category_cao",
        "pick_directory_scans",
        "pick_directory_experiments",
        "create_now",
        "add_details",
    ]:
        assert _action_matches(app, action_id), action_id


def test_all_subcommands_present():
    assert set(SUBCOMMANDS) == {
        "new",
        "track",
        "date",
        "delete",
        "category",
        "scans",
        "experiments",
        "legacy",
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
        "channel": "C1",
        "response_url": "http://example.invalid/hook",
        "suffix": "",
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
