"""ExperimentNamer Slack App — entry point.

Generates unique experiment names in the format:
    YYYY-MM-DD-{category}-word-word

The two-word combo comes from the `unique-namer` package
(https://github.com/aziele/unique-namer).

Subcommands (see CLAUDE.md for the full spec):
    /experiment new                    — pick a category, generate a new name
    /experiment track <name|combo|codename>
                                       — find the experiment's Box folder + link
    /experiment date <YYYY-MM-DD>      — list experiments generated on a date
    /experiment delete <name>          — delete an experiment (only if empty)
    /experiment delete empty           — prune every empty experiment folder
    /experiment category <code>        — list experiments for bph, cao, or flr (both dirs)
    /experiment scans [code]           — list experiments in the Scans directory
    /experiment experiments [code]     — list experiments in the Experiments directory
    /experiment legacy [box-link]      — rename a legacy folder to the scheme

The app is assembled from the modules under `core/slack`:
    naming       — name generation, detection, validation (pure helpers)
    views        — Block Kit builders and user-facing message strings
    experiments  — association / legacy-conversion / create-announce logic
    commands     — the /experiment subcommand handlers + dispatch table
    handlers     — Bolt wiring (register(app))

Box-backed subcommands are wired through core.box.box_client, which degrades
to a "not connected yet" notice until the BOX_* variables are set.

Runs over Socket Mode, so no public URL is required.
"""

import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from core.slack.handlers import register

load_dotenv()

logging.basicConfig(level=logging.INFO)

# SLACK_TOKEN_VERIFY=0 skips Bolt's startup auth.test call so the module can
# be imported in tests without real tokens.
app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    token_verification_enabled=os.environ.get("SLACK_TOKEN_VERIFY", "1") != "0",
)

register(app)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
