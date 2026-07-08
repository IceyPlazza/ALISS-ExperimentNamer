"""`/experiment <subcommand>` handlers and the dispatch table.

Each handler receives (respond, arg, **context) where arg is everything
after the subcommand word, stripped (may be ""), and context carries
client/body for handlers that open modals. SUBCOMMANDS maps the subcommand
word to its handler; the Bolt command handler in handlers.py dispatches
through it.
"""

import logging
from datetime import date

from core.box import box_client, csv_index
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack.naming import (
    CODENAME_RE,
    EXPERIMENT_CATEGORIES,
    EXPERIMENT_NAME_RE,
)
from core.slack.experiments import (
    parse_associations,
    parse_children,
    parse_parent,
    post_delete,
    post_prune,
    purge_references,
)
from core.slack.views import (
    BOX_NOT_READY,
    HELP,
    category_buttons,
    format_experiment_list,
    legacy_modal_view,
    subexperiment_modal_view,
)


def _sync_deleted(folder_ids):
    """Drop the CSV index rows for deleted folders — best-effort so an index
    hiccup never breaks the (already-completed) deletion."""
    try:
        csv_index.sync_deleted(folder_ids)
    except Exception:
        logging.warning("could not sync deletion to the index", exc_info=True)


def _read_description(folder_id):
    """A folder's Box description, tolerating an unreadable/unconfigured folder
    (returns "")."""
    try:
        return box_client.get_folder_description(folder_id)
    except Exception:
        return ""


def cmd_new(respond, arg, **_):
    """Show the category picker; the button click finishes the flow."""
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


def cmd_subexperiment(respond, arg, client=None, body=None, **_):
    """Open the sub-experiment dialog to create an experiment under a parent.

    If arg already looks like a Box link
    (/experiment subexperiment <link>), it pre-fills the parent-link field.
    """
    meta = {
        "user": body["user_id"],
        "user_name": body.get("user_name", ""),
        "channel": body["channel_id"],
        "response_url": body.get("response_url", ""),
    }
    client.views_open(
        trigger_id=body["trigger_id"],
        view=subexperiment_modal_view(meta, arg),
    )
    respond(
        response_type="ephemeral",
        text=":writing_hand: Fill in the sub-experiment details in the dialog.",
    )


def cmd_track(respond, arg, **_):
    """Find an experiment's Box folder and link to it.

    Accepts a full name (2026-07-06-decorous-harbor-BPH-seg-iven.chen) or a
    bare codename (decorous-harbor). The codename is the unique lookup key, so
    it normally resolves to a single folder; if several folders share it, they
    are listed as candidates.
    """
    if not arg:
        respond(text="Usage: `/experiment track <experiment-name | codename>`")
        return
    if not (EXPERIMENT_NAME_RE.match(arg) or CODENAME_RE.match(arg)):
        respond(
            text=f"`{arg}` doesn't look like an experiment name "
            "(expected `YYYY-MM-DD-codename-CATEGORY-…` or a bare `codename`)."
        )
        return
    _track_single(respond, arg)


def _render_experiment_detail(respond, label, folder):
    """Reply with a single experiment's location, link, and associations.
    `label` is echoed in the header (the user's query — name/combo/codename)."""
    lines = [
        f":file_folder: `{label}`",
        f"• Location: `{folder['path']}`",
        f"• <{folder['url']}|Open in Box>",
    ]
    # Relationships live in the folder's Box description (source of truth).
    desc = _read_description(folder["id"])

    def _ref(entry):
        return f"<{entry['url']}|{entry['label']}>" if entry.get("url") else f"`{entry['label']}`"

    parent = parse_parent(desc)
    if parent:
        lines.append(f"• Parent experiment: {_ref(parent)}")
    children = parse_children(desc)
    if children:
        lines.append("• Sub-experiments:")
        for c in children:
            lines.append(f"    ◦ {_ref(c)}")
    associations = parse_associations(desc)
    if associations:
        lines.append("• Associated experiments:")
        for a in associations:
            lines.append(f"    ◦ {_ref(a)}")
    respond(text="\n".join(lines))


def _track_single(respond, arg):
    """Track by full name or word-word combo — resolves to one folder."""
    try:
        folder = box_client.find_experiment_folder(arg)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    except AmbiguousExperimentError as e:
        respond(
            text=f":warning: `{arg}` matches more than one experiment:\n"
            + "\n".join(f"• `{name}`" for name in e.candidates)
        )
        return
    except KeyError:
        respond(text=f":mag: No Box folder found for `{arg}`.")
        return
    _render_experiment_detail(respond, arg, folder)


def cmd_date(respond, arg, **_):
    """List experiments generated on a given date."""
    if not arg:
        respond(text="Usage: `/experiment date <YYYY-MM-DD>`")
        return
    try:
        date.fromisoformat(arg)
    except ValueError:
        respond(text=f"`{arg}` isn't a valid date (expected `YYYY-MM-DD`).")
        return
    try:
        experiments = box_client.list_experiments_by_date(arg)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    if not experiments:
        respond(text=f":mag: No experiments found for {arg}.")
        return
    respond(
        text=f"*Experiments from {arg}:*\n" + format_experiment_list(experiments)
    )


def cmd_delete(respond, arg, say=None, body=None, **_):
    """Delete an experiment's Box folder, but only if it's empty.

    Successful deletions are announced to the channel (via `say`); usage and
    error replies stay ephemeral (via `respond`). `delete empty` prunes every
    experiment folder that has no files. Safe to special-case: real
    experiment names always start with a date, so a bare "empty" can never be
    one.
    """
    if not arg:
        respond(text="Usage: `/experiment delete <experiment-name>` or `/experiment delete empty`")
        return
    if arg.lower() == "empty":
        prune_empty_experiments(respond, say, body)
        return
    try:
        folder = box_client.find_experiment_folder(arg)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    except AmbiguousExperimentError as e:
        respond(
            text=f":warning: `{arg}` matches more than one experiment — "
            "specify the full name:\n"
            + "\n".join(f"• `{name}`" for name in e.candidates)
        )
        return
    except KeyError:
        respond(text=f":mag: No Box folder found for `{arg}` — nothing to delete.")
        return
    if box_client.folder_has_files(folder["id"]):
        respond(
            text=f":no_entry: `{arg}` has files in its Box folder — "
            f"not deleting. Review it first: <{folder['url']}|open in Box>"
        )
        return
    box_client.delete_experiment_folder(folder["id"])
    # Strip any association pointing at this now-deleted folder (both dirs).
    purge_references({folder["id"]})
    _sync_deleted({folder["id"]})
    post_delete(say, body["user_id"], folder)


def prune_empty_experiments(respond, say=None, body=None):
    """Delete every experiment folder (in both Box directories) that contains
    no files, and announce the prune to the channel."""
    try:
        folders = box_client.list_experiment_folders(include_nested=True)
        deleted = []
        for folder in folders:
            if not box_client.folder_has_files(folder["id"]):
                box_client.delete_experiment_folder(folder["id"])
                deleted.append(folder)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    if not deleted:
        respond(text=":broom: No empty experiments found — nothing to prune.")
        return
    # One crawl strips references to every folder we just pruned.
    purge_references({f["id"] for f in deleted})
    _sync_deleted({f["id"] for f in deleted})
    post_prune(say, body["user_id"], deleted)


def cmd_category(respond, arg, **_):
    """List experiments belonging to a category code (across both
    directories)."""
    code = arg.lower()
    if code not in EXPERIMENT_CATEGORIES:
        respond(
            text=f"Usage: `/experiment category <{'|'.join(EXPERIMENT_CATEGORIES)}>`"
        )
        return
    try:
        experiments = box_client.list_experiments_by_category(code)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    if not experiments:
        respond(text=f":mag: No experiments found for category `{code}`.")
        return
    respond(
        text=f"*{code.upper()} experiments:*\n" + format_experiment_list(experiments)
    )


def _list_directory(respond, arg, dir_key):
    """Shared body for /experiment scans and /experiment experiments: list
    that one directory's experiments, optionally filtered to a category code.
    No category → everything in the directory."""
    code = arg.strip().lower() or None
    if code is not None and code not in EXPERIMENT_CATEGORIES:
        respond(
            text=f"Usage: `/experiment {dir_key} [{'|'.join(EXPERIMENT_CATEGORIES)}]`"
        )
        return
    try:
        experiments = box_client.list_experiments_by_category(code, dir_key)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    label = box_client.directory_info(dir_key)["label"]
    if not experiments:
        who = f"{code.upper()} " if code else ""
        respond(text=f":mag: No {who}experiments found in _{label}_.")
        return
    heading = f"{code.upper()} experiments" if code else "All experiments"
    respond(
        text=f"*{heading} in _{label}_:*\n" + format_experiment_list(experiments)
    )


def cmd_scans(respond, arg, **_):
    """List experiments in the Scans directory, optionally by category."""
    _list_directory(respond, arg, "scans")


def cmd_experiments(respond, arg, **_):
    """List experiments in the Experiments directory, optionally by category."""
    _list_directory(respond, arg, "experiments")


def cmd_legacy(respond, arg, client=None, body=None, **_):
    """Open the dialog that converts a legacy folder to the naming scheme.

    If arg already looks like a Box link (/experiment legacy <link>), it
    pre-fills the link field.
    """
    meta = {
        "user": body["user_id"],
        "user_name": body.get("user_name", ""),
        "channel": body["channel_id"],
    }
    client.views_open(
        trigger_id=body["trigger_id"],
        view=legacy_modal_view(meta, arg),
    )
    respond(
        response_type="ephemeral",
        text=":writing_hand: Fill in the legacy folder details in the dialog.",
    )


def cmd_database(respond, arg, **_):
    """Inspect or refresh the experiment_index.csv "database".

    `retrieve` returns a link to the CSV so a human can review it (creating +
    backfilling it first if it doesn't exist yet). `update` fully reconciles
    the CSV against a live crawl of both directories (adds new folders,
    refreshes changed rows, drops folders that no longer exist).
    """
    action = arg.strip().lower()
    if action == "retrieve":
        try:
            url = csv_index.index_link()
        except BoxNotConfiguredError:
            respond(text=BOX_NOT_READY)
            return
        respond(
            text=f":card_index_dividers: Experiment index: "
            f"<{url}|{csv_index.INDEX_CSV_NAME}> "
            f"(in _{csv_index.INDEX_FOLDER_NAME}_)."
        )
    elif action == "update":
        try:
            summary = csv_index.rebuild()
        except BoxNotConfiguredError:
            respond(text=BOX_NOT_READY)
            return
        respond(
            text=(
                f":card_index_dividers: Index updated — "
                f"{summary['added']} added, {summary['updated']} refreshed, "
                f"{summary['removed']} removed ({summary['total']} total).\n"
                f"<{summary['url']}|{csv_index.INDEX_CSV_NAME}>"
            )
        )
    else:
        respond(text="Usage: `/experiment database <retrieve | update>`")


def cmd_help(respond, arg, **_):
    """Show the full command guide (the long-form counterpart to USAGE)."""
    respond(text=HELP)


SUBCOMMANDS = {
    "new": cmd_new,
    "subexperiment": cmd_subexperiment,
    "track": cmd_track,
    "date": cmd_date,
    "delete": cmd_delete,
    "category": cmd_category,
    "scans": cmd_scans,
    "experiments": cmd_experiments,
    "legacy": cmd_legacy,
    "database": cmd_database,
    "help": cmd_help,
}
