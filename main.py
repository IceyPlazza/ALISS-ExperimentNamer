"""ExperimentNamer Slack App.

Generates unique experiment names in the format:
    YYYY-MM-DD-{category}-word-word

The two-word combo comes from the `unique-namer` package
(https://github.com/aziele/unique-namer).

Flow:
    1. A user runs /experiment in any channel the bot is in.
    2. The bot replies (ephemerally) asking them to pick a category.
    3. On click, the bot generates the name and posts it to the channel.

Runs over Socket Mode, so no public URL is required.
"""

import logging
import os
import re
from datetime import date

import namer
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

logging.basicConfig(level=logging.INFO)

# Experiment categories offered by the /experiment picker. The code goes
# directly into the generated name: YYYY-MM-DD-{code}-word-word.
EXPERIMENT_CATEGORIES = ["bph", "cao"]

# Word categories that unique-namer draws from when building the word-word
# combo. See namer.list_categories() for the 25 available options.
NAMER_CATEGORIES = ["general"]

app = App(token=os.environ["SLACK_BOT_TOKEN"])


def generate_experiment_name(category: str) -> str:
    """Build a name like 2026-07-04-bph-tattered-flower."""
    today = date.today().isoformat()
    combo = namer.generate(category=NAMER_CATEGORIES, separator="-", style="lowercase")
    return f"{today}-{category}-{combo}"


def category_buttons() -> list:
    return [
        {
            "type": "button",
            "action_id": f"pick_category_{code}",
            "text": {"type": "plain_text", "text": code.upper()},
            "value": code,
        }
        for code in EXPERIMENT_CATEGORIES
    ]


@app.command("/experiment")
def open_category_prompt(ack, respond):
    """Reply to /experiment with the category picker."""
    ack()
    respond(
        response_type="ephemeral",
        text=f"Pick a category for the new experiment: {', '.join(EXPERIMENT_CATEGORIES)}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":test_tube: *New experiment* — pick a category:",
                },
            },
            {
                "type": "actions",
                "block_id": "experiment_category",
                "elements": category_buttons(),
            },
        ],
    )


@app.action(re.compile(r"pick_category_\w+"))
def handle_category_pick(ack, body, respond, say):
    """Generate the experiment name and announce it in the channel."""
    ack()
    category = body["actions"][0]["value"]  # e.g. "bph" or "cao"
    user_id = body["user"]["id"]
    experiment_name = generate_experiment_name(category)

    # Replace the ephemeral picker so the buttons can't be clicked twice.
    respond(
        response_type="ephemeral",
        replace_original=True,
        text=f"Generated: {experiment_name}",
    )

    # Announce to the whole channel so the team sees the new experiment.
    say(
        text=f"New experiment: {experiment_name} (started by <@{user_id}>)",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":sparkles: New experiment started by <@{user_id}>:\n"
                        f"*`{experiment_name}`*"
                    ),
                },
            },
        ],
    )


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
