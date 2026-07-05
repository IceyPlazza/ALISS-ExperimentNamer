"""Shared test fixtures and helpers.

The Slack handlers talk to Slack and Box only through callables passed in
(`respond`, `post_message`, `update_ephemeral`) and the `box_client` module.
These helpers capture the former and make it easy to stub the latter, so the
whole app can be exercised without real tokens or network access.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-box-live",
        action="store_true",
        default=False,
        help="run live Box integration tests (hits the real Box API and "
        "creates/deletes folders; needs BOX_* configured in .env)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "box_live: live Box API test — opt-in via --run-box-live (deselected "
        "by default so plain `pytest` and CI never touch the network).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-box-live"):
        return
    skip_live = pytest.mark.skip(reason="live Box test; pass --run-box-live to run")
    for item in items:
        if "box_live" in item.keywords:
            item.add_marker(skip_live)


class Recorder:
    """A callable that records every call for later inspection.

    Stands in for Slack's `respond` / `post_message` / `update_ephemeral`.
    Handles both keyword calls (respond(text=...)) and the single positional
    call update_ephemeral uses (update_ephemeral("Generated: …")).
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def __len__(self):
        return len(self.calls)

    @property
    def kwargs(self):
        """The kwargs of the most recent call."""
        return self.calls[-1][1]

    @property
    def texts(self):
        """Every call's text, whether passed as text=... or positionally."""
        out = []
        for args, kwargs in self.calls:
            if "text" in kwargs:
                out.append(str(kwargs["text"]))
            elif args:
                out.append(str(args[0]))
        return out

    @property
    def text(self):
        """All recorded text joined — convenient for substring assertions."""
        return "\n".join(self.texts)


@pytest.fixture
def respond():
    return Recorder()


@pytest.fixture
def post_message():
    return Recorder()


@pytest.fixture
def say():
    """Stands in for Bolt's `say` — posts to the channel."""
    return Recorder()


@pytest.fixture
def update_ephemeral():
    return Recorder()
