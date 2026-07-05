"""`/experiment <subcommand>` handlers and the dispatch table.

Each handler receives (respond, arg, **context) where arg is everything
after the subcommand word, stripped (may be ""), and context carries
client/body for handlers that open modals. SUBCOMMANDS maps the subcommand
word to its handler; the Bolt command handler in handlers.py dispatches
through it.
"""

from datetime import date

from core.box import box_client
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack.naming import (
    BOX_FOLDER_LINK_RE,
    EXPERIMENT_CATEGORIES,
    EXPERIMENT_NAME_RE,
    WORD_COMBO_RE,
)
from core.slack.experiments import (
    parse_associations,
    post_delete,
    post_prune,
    remove_back_references,
)
from core.slack.views import (
    BOX_NOT_READY,
    category_buttons,
    format_experiment_list,
    legacy_modal_view,
)


def _read_associations(folder_id):
    """Parse the associations stored on a folder's Box description, tolerating
    an unreadable/unconfigured folder (returns [])."""
    try:
        return parse_associations(box_client.get_folder_description(folder_id))
    except Exception:
        return []


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


def cmd_track(respond, arg, **_):
    """Find an experiment's Box folder and link to it.

    Accepts either the full name (2026-07-04-bph-decorous-harbor) or just
    the word-word combo (decorous-harbor).
    """
    if not arg:
        respond(text="Usage: `/experiment track <experiment-name | word-word>`")
        return
    if not (EXPERIMENT_NAME_RE.match(arg) or WORD_COMBO_RE.match(arg)):
        respond(
            text=f"`{arg}` doesn't look like an experiment name "
            "(expected `YYYY-MM-DD-category-word-word`, or just `word-word`)."
        )
        return
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

    lines = [
        f":file_folder: `{arg}`",
        f"• Location: `{folder['path']}`",
        f"• <{folder['url']}|Open in Box>",
    ]
    # Associated experiments are stored in the folder's Box description.
    associations = _read_associations(folder["id"])
    if associations:
        lines.append("• Associated experiments:")
        for a in associations:
            ref = f"<{a['url']}|{a['label']}>" if a["url"] else f"`{a['label']}`"
            lines.append(f"    ◦ {ref}")
    respond(text="\n".join(lines))


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
    # Read the associations off the folder before deleting so we can strip
    # the matching back-references from the other side.
    associations = _read_associations(folder["id"])
    box_client.delete_experiment_folder(folder["id"])
    remove_back_references(folder, associations)
    post_delete(say, body["user_id"], folder)


def prune_empty_experiments(respond, say=None, body=None):
    """Delete every experiment folder (in both Box directories) that contains
    no files, and announce the prune to the channel."""
    try:
        folders = box_client.list_experiment_folders()
        deleted = []
        for folder in folders:
            if not box_client.folder_has_files(folder["id"]):
                associations = _read_associations(folder["id"])
                box_client.delete_experiment_folder(folder["id"])
                remove_back_references(folder, associations)
                deleted.append(folder)
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    if not deleted:
        respond(text=":broom: No empty experiments found — nothing to prune.")
        return
    post_prune(say, body["user_id"], deleted)


def cmd_category(respond, arg, **_):
    """List experiments belonging to a category code."""
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


def cmd_legacy(respond, arg, client=None, body=None, **_):
    """Open the dialog that converts a legacy folder to the naming scheme.

    If arg already looks like a Box link (/experiment legacy <link>), it
    pre-fills the link field.
    """
    meta = {"user": body["user_id"], "channel": body["channel_id"]}
    client.views_open(
        trigger_id=body["trigger_id"],
        view=legacy_modal_view(meta, arg),
    )
    respond(
        response_type="ephemeral",
        text=":writing_hand: Fill in the legacy folder details in the dialog.",
    )


SUBCOMMANDS = {
    "new": cmd_new,
    "track": cmd_track,
    "date": cmd_date,
    "delete": cmd_delete,
    "category": cmd_category,
    "legacy": cmd_legacy,
}
