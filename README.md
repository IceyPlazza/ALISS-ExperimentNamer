# Experiment Namer

A Slack app that generates unique names for new lab experiments in the format:

```
YYYY-MM-DD-{category}-word-word
```

e.g. `2026-07-04-bph-tattered-flower`. The categories are `bph` and `cao`,
and the two-word combo comes from
[unique-namer](https://github.com/aziele/unique-namer).

## How it works

1. Run `/experiment` in any channel the bot has been added to.
2. The bot asks you (privately) to pick a category (**BPH** or **CAO**).
3. On click, the bot posts the generated experiment name to the channel for
   the whole team to see.

Box integration for tracking the state of past experiments (including names
generated before this app existed) is planned but not implemented yet.

## One-time Slack setup

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace, then paste the contents of [`manifest.json`](manifest.json).
3. After the app is created:
   - **Basic Information → App-Level Tokens** → *Generate Token* with the
     `connections:write` scope. This is your `SLACK_APP_TOKEN` (`xapp-...`).
   - **Install App** (or *OAuth & Permissions*) → install to the workspace and
     copy the **Bot User OAuth Token** (`xoxb-...`). This is your
     `SLACK_BOT_TOKEN`.
4. Copy `.env.example` to `.env` and paste in both tokens.
5. In Slack, invite the bot to a channel: `/invite @Experiment Namer`.

## Running locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

The app uses Socket Mode, so no public URL or port forwarding is needed — it
just needs to be running somewhere while the team uses it.

## Configuration

- `EXPERIMENT_CATEGORIES` in `main.py` is the list of category codes offered
  by the picker (currently `["bph", "cao"]`). Adding a code there adds a
  button automatically.
- `NAMER_CATEGORIES` in `main.py` controls which unique-namer word lists are
  used for the word-word combo (default `["general"]`). Run
  `python -c "import namer; print(namer.list_categories())"` to see all 25
  options (e.g. `biology`, `chemistry`, `astronomy`, `animals`).
