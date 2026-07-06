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
    CODENAME_RE,
    EXPERIMENT_CATEGORIES,
    EXPERIMENT_NAME_RE,
    WORD_COMBO_RE,
    extract_codename,
)
from core.slack.experiments import (
    parse_associations,
    post_delete,
    post_prune,
    purge_references,
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

    Accepts the full name (2026-07-04-bph-decorous-harbor), the word-word
    combo (decorous-harbor), or a codename (the model label appended to an
    Experiments-directory name). Names/combos resolve to a single folder
    (combos are unique); a codename may be shared by several experiments, in
    which case all of them are listed.
    """
    if not arg:
        respond(
            text="Usage: `/experiment track <experiment-name | word-word | codename>`"
        )
        return
    is_name_or_combo = EXPERIMENT_NAME_RE.match(arg) or WORD_COMBO_RE.match(arg)
    if not (is_name_or_combo or CODENAME_RE.match(arg)):
        respond(
            text=f"`{arg}` doesn't look like an experiment name "
            "(expected `YYYY-MM-DD-category-word-word`, a `word-word` combo, "
            "or a codename)."
        )
        return
    if is_name_or_combo:
        _track_single(respond, arg)
    else:
        _track_by_codename(respond, arg)


def _render_experiment_detail(respond, label, folder):
    """Reply with a single experiment's location, link, and associations.
    `label` is echoed in the header (the user's query — name/combo/codename)."""
    lines = [
        f":file_folder: `{label}`",
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


def _track_by_codename(respond, arg):
    """Track by codename. Codenames aren't unique: one match renders in full,
    several are listed. Filters the folder crawl through extract_codename so
    scan-count suffixes and combo-only names never match."""
    try:
        folders = box_client.list_experiment_folders()
    except BoxNotConfiguredError:
        respond(text=BOX_NOT_READY)
        return
    matches = [f for f in folders if extract_codename(f["name"]) == arg]
    if not matches:
        respond(text=f":mag: No experiment found with name or codename `{arg}`.")
        return
    if len(matches) == 1:
        _render_experiment_detail(respond, arg, matches[0])
        return
    respond(
        text=f"*{len(matches)} experiments use codename `{arg}`:*\n"
        + format_experiment_list(matches)
    )


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
    post_delete(say, body["user_id"], folder)


def prune_empty_experiments(respond, say=None, body=None):
    """Delete every experiment folder (in both Box directories) that contains
    no files, and announce the prune to the channel."""
    try:
        folders = box_client.list_experiment_folders()
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
    "scans": cmd_scans,
    "experiments": cmd_experiments,
    "legacy": cmd_legacy,
}
