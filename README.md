# Experiment Namer

A Slack app that generates unique names for new lab experiments in the format:

```
YYYY-MM-DD-{category}-word-word
```

e.g. `2026-07-04-bph-tattered-flower`. The categories are `bph` and `cao`,
and the two-word combo comes from
[unique-namer](https://github.com/aziele/unique-namer).

## Commands

| Command | What it does                                                                                                                                                                                                                                                                                                                                                                                                             |
|---|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `/experiment new` | Pick a category (**BPH**/**CAO**) and a Box directory, then "Create now" or "Add details…" — optional scan count (`…-word-word-(3)`) or model codename (`…-word-word-modelA`) depending on the directory, plus an optional associated experiment (by word-word combo, full name, or Box link — with an option to rename a legacy associated folder to the naming scheme on the spot). The name is posted to the channel. |
| `/experiment track <name \| word-word>` | Find the experiment's Box folder — full path plus a direct link. The bare `word-word` combo is enough.                                                                                                                                                                                                                                                                                                                   |
| `/experiment date <YYYY-MM-DD>` | List experiments generated on that date.                                                                                                                                                                                                                                                                                                                                                                                 |
| `/experiment delete <name>` | Delete an experiment — refuses if its Box folder has files.                                                                                                                                                                                                                                                                                                                                                              |
| `/experiment delete empty` | Prune every experiment whose Box folder has no files.                                                                                                                                                                                                                                                                                                                                                                    |
| `/experiment category <bph\|cao>` | List experiments for a category.                                                                                                                                                                                                                                                                                                                                                                                         |
| `/experiment legacy [box-link]` | Rename a legacy folder (e.g. `2026-06-17 - FLR: CT`) to the naming scheme — date/category auto-detected from the old name, prompted for otherwise.                                                                                                                                                                                                                                                                       |

Experiments live in one of two Box directories — `\Box\ARPA-H\Data Collection
and Scans` or `\Box\ARPA-H\Experiments`. `/experiment new` asks which one to
use, and every listing reports which directory each experiment is in.

`/experiment new` is fully working. The other subcommands parse and validate
input but reply ":construction: not connected yet" until the Box integration
(`core/box/box_client.py`) is configured — including access to
experiment names generated before this app existed.

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

## One-time Box setup

1. Go to <https://app.box.com/developers/console> (sign in with your
   Vanderbilt Box account) → **Create Platform App** → **Custom App**.
   Name it e.g. `Experiment Namer`.
2. Quick start (acts as you, token lasts 60 minutes): on the app's
   **Configuration** page, click **Generate Developer Token** and put it in
   `.env` as `BOX_DEVELOPER_TOKEN` with `BOX_AUTH_METHOD=dev_token`.
3. Get the folder IDs: open each folder at app.box.com and copy the number
   from the URL (`.../folder/123456789`):
   - `BOX_SCANS_FOLDER_ID` — ARPA-H → Data Collection and Scans
   - `BOX_EXPERIMENTS_FOLDER_ID` — ARPA-H → Experiments
   - `BOX_ROOT_FOLDER_ID` — the ARPA-H root
4. Verify: `.\.venv\Scripts\python.exe -m core.box.box_setup_check`

**Testing against a sandbox first (recommended):** directory names and paths
are read live from Box based on the folder IDs — nothing about ARPA-H is
hardcoded. To rehearse safely, make e.g. `Experiment Namer Test` with two
subfolders inside it, point the three `*_FOLDER_ID` variables at the test
folders, and run the app; every reply will truthfully show the test folder
names. Swap the IDs back to the real ARPA-H folders when satisfied.

For production (no 60-minute expiry), switch the app to **Client Credentials
Grant** in the dev console, have the Box admin authorize it in the Admin
Console, invite the app's service account as a collaborator on the ARPA-H
folder, and set `BOX_AUTH_METHOD=ccg` with `BOX_CLIENT_ID`,
`BOX_CLIENT_SECRET`, `BOX_ENTERPRISE_ID`.

## Running locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

The app uses Socket Mode, so no public URL or port forwarding is needed — it
just needs to be running somewhere while the team uses it.

## Running tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Tests live in [`tests/`](tests) and run without real Slack/Box tokens — the
handlers talk to Slack and Box only through injected callables and the
`core.box.box_client` module, both of which the tests stub out. The same
suite runs in CI on every push and pull request (see
[`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

**Live Box tests** (`tests/test_box_live.py`) hit the real Box API and are
**opt-in** — plain `pytest` deselects them. To run them, connect Box (see
below) and pass the flag:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_box_live.py --run-box-live -v
```

They **skip** (never fail) when `--run-box-live` is omitted, when `BOX_*`
isn't set (e.g. public CI, which has no committed `.env`), or when the dev
token has expired. Point the `*_FOLDER_ID` vars at a sandbox — these tests
create and delete folders.

## Configuration

- `EXPERIMENT_CATEGORIES` in `core/slack/naming.py` is the list of category
  codes offered by the picker (currently `["bph", "cao"]`). Adding a code
  there adds a button automatically.
- `NAMER_CATEGORIES` in `core/slack/naming.py` controls which unique-namer word lists are
  used for the word-word combo (default `["general"]`). Run
  `python -c "import namer; print(namer.list_categories())"` to see all 25
  options (e.g. `biology`, `chemistry`, `astronomy`, `animals`).
