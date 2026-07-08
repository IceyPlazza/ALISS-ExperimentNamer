"""Bolt handler registration for the /experiment command.

`register(app)` attaches the slash-command dispatcher, the action handlers
for the multi-step "new" flow, and the two modal-submit handlers onto a Bolt
App. Keeping this separate from `main.py` means the App can be built with
whatever tokens/config the entry point wants, while the handler wiring stays
importable (and testable) without starting Socket Mode.
"""

import json
import logging
import re

from slack_sdk.webhook import WebhookClient

from core.box import box_client
from core.box.box_client import BoxNotConfiguredError  # noqa: F401 (re-export convenience)
from core.slack.commands import SUBCOMMANDS
from core.slack.experiments import (
    AssociationError,
    LegacyConversionError,
    SubexperimentError,
    classify_association,
    convert_legacy_folder,
    finish_new_experiment,
    finish_subexperiment,
    pickup_legacy_children,
    post_legacy_rename,
    resolve_parent,
)
from core.slack.naming import BOX_FOLDER_LINK_RE, detect_category, detect_date
from core.slack.views import (
    USAGE,
    associate_legacy_view,
    details_modal_view,
    directory_buttons,
)


def _user_name(user):
    """The acting user's name for the CSV index's created_by_slack_user column.

    Read straight from the interaction payload's `user` object
    (username → name → id), which needs NO extra scope — unlike users.info,
    which requires `users:read` the bot isn't granted. `user` is the dict Slack
    puts on block-action / view-submission payloads
    (`{"id","username","name",…}`)."""
    if not isinstance(user, dict):
        return ""
    return user.get("username") or user.get("name") or user.get("id") or ""


def _announce_new_experiment(
    client, meta, associations, *, codename, action, num_scans, renamed_list=()
):
    """Post any legacy-rename announcements, then create + announce the new
    experiment. Shared by both new-experiment view-submit handlers.

    `meta` carries value ("category:dir_key"), user, user_name, channel,
    response_url. codename/action/num_scans are the details from the modal."""
    category, dir_key = meta["value"].split(":", 1)

    def update_ephemeral(text):
        try:
            WebhookClient(meta["response_url"]).send(
                text=text, replace_original=True, response_type="ephemeral"
            )
        except Exception:
            logging.warning("could not update ephemeral prompt", exc_info=True)

    created_by = meta.get("user_name", "")
    post_message = lambda **kw: client.chat_postMessage(channel=meta["channel"], **kw)
    for renamed in renamed_list:
        post_legacy_rename(post_message, meta["user"], renamed, created_by=created_by)
    finish_new_experiment(
        category,
        dir_key,
        meta["user"],
        post_message=post_message,
        update_ephemeral=update_ephemeral,
        user_handle=created_by,
        action=action,
        num_scans=num_scans,
        codename=codename,
        associations=associations,
        created_by=created_by,
    )


def _link_assoc(label, url, note=""):
    """Build a {label, url, mrkdwn} association dict for a Box-linked folder."""
    mrkdwn = f"<{url}|{label}>" + (f" {note}" if note else "")
    return {"label": label, "url": url, "mrkdwn": mrkdwn}


def register(app):
    """Attach every /experiment handler onto the given Bolt App."""

    @app.command("/experiment")
    def handle_experiment_command(ack, respond, command, client, body, say):
        """Dispatch /experiment <subcommand> [args]."""
        ack()
        text = (command.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = SUBCOMMANDS.get(sub)
        if handler is None:
            respond(response_type="ephemeral", text=USAGE)
            return
        try:
            handler(respond, arg, client=client, body=body, say=say)
        except Exception as e:  # surface Box/API failures instead of dropping them
            logging.exception("subcommand %r failed", sub)
            respond(text=f":x: `{sub}` failed: {e}")

    @app.action(re.compile(r"pick_category_\w+"))
    def handle_category_pick(ack, body, respond):
        """Step 2 of /experiment new: ask which Box directory to use."""
        ack()
        category = body["actions"][0]["value"]  # e.g. "bph" or "cao"
        respond(
            response_type="ephemeral",
            replace_original=True,
            text=f"Category {category.upper()} — now pick a Box directory.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":test_tube: *New {category.upper()} experiment* — "
                            "where should it live in Box?"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": "experiment_directory",
                    "elements": directory_buttons(category),
                },
            ],
        )

    @app.action(re.compile(r"pick_directory_\w+"))
    def handle_directory_pick(ack, body, client, respond):
        """Step 3 of /experiment new: open the details dialog.

        Every experiment now needs a directory-specific required field (action
        for Experiments, scan count for Scans), so the flow always goes through
        the modal — there's no create-without-details shortcut."""
        ack()
        value = body["actions"][0]["value"]  # "category:dir_key"
        _, dir_key = value.split(":", 1)
        meta = {
            "value": value,
            "user": body["user"]["id"],
            "user_name": _user_name(body["user"]),
            "channel": body["channel"]["id"],
            "response_url": body["response_url"],
        }
        client.views_open(
            trigger_id=body["trigger_id"],
            view=details_modal_view(meta, dir_key),
        )
        respond(
            response_type="ephemeral",
            replace_original=True,
            text=":writing_hand: Enter the details in the dialog "
            "(if you cancel it, run `/experiment new` again).",
        )

    @app.view("new_experiment_details")
    def handle_details_submit(ack, view, client):
        """Details dialog submitted: read the codename override + the required
        directory-specific segment, then classify each association entry.

        Associations that resolve cleanly (named experiments, codenames,
        already-scheme or unreadable links) are applied immediately. If any
        entry is a Box link to a folder NOT named to the scheme, we push the
        `associate_legacy` modal to let the user rename it or keep it as a
        link, and finish there.
        """
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]
        _, dir_key = meta["value"].split(":", 1)

        # Optional codename override (blank → auto-generate later).
        codename = (vals["codename"]["codename_input"].get("value") or "").strip()

        # Directory-specific required segment.
        action = None
        num_scans = None
        if dir_key == "scans":
            num_scans = vals["scan_count"]["count_input"].get("value")
            if not num_scans:
                ack(
                    response_action="errors",
                    errors={"scan_count": "Enter the number of scans."},
                )
                return
        else:
            action = (vals["action"]["action_input"].get("value") or "").strip()
            if not action:
                ack(response_action="errors", errors={"action": "Enter an action."})
                return

        # Classify each association line (one entry per line).
        raw_block = vals["assoc"]["assoc_input"].get("value") or ""
        entries = [line.strip() for line in raw_block.splitlines() if line.strip()]
        ready = []
        legacy = []
        for raw in entries:
            try:
                result = classify_association(raw)
            except AssociationError as e:
                ack(response_action="errors", errors={"assoc": str(e)})
                return
            if result["kind"] == "ready":
                ready.append(result["assoc"])
            else:
                legacy.append(result["legacy"])

        if legacy:
            # Hand off to the legacy dialog, carrying everything needed to
            # finish once the user has chosen rename-or-keep for each folder.
            push_meta = {
                "value": meta["value"],
                "user": meta["user"],
                "user_name": meta.get("user_name", ""),
                "channel": meta["channel"],
                "response_url": meta["response_url"],
                "codename": codename,
                "action": action,
                "num_scans": num_scans,
                "ready": ready,
                "legacy": legacy,
            }
            ack(
                response_action="push",
                view=associate_legacy_view(push_meta, legacy),
            )
            return

        ack()
        _announce_new_experiment(
            client,
            meta,
            ready,
            codename=codename,
            action=action,
            num_scans=num_scans,
        )

    @app.view("associate_legacy")
    def handle_associate_legacy_submit(ack, view, client):
        """The legacy-associations dialog was submitted. For each legacy
        folder the user either renamed it (converted to the scheme) or kept it
        as a link; build the full association list, then create + announce.

        Missing date/category for a rename is validated up front (inline
        errors, before any Box mutation) so a resubmit never double-renames.
        """
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]

        def rename_selected(i):
            block = vals.get(f"act_{i}", {}).get("act", {})
            return bool(block.get("selected_options"))

        def picked_date(i):
            return vals.get(f"date_{i}", {}).get("date", {}).get("selected_date")

        def picked_category(i):
            opt = vals.get(f"cat_{i}", {}).get("cat", {}).get("selected_option")
            return opt["value"] if opt else None

        def picked_action(i):
            return (vals.get(f"action_{i}", {}).get("action", {}).get("value") or "").strip()

        def picked_scans(i):
            return vals.get(f"scan_{i}", {}).get("count", {}).get("value")

        # Pass 1: validate rename choices before mutating anything in Box.
        errors = {}
        for i, lg in enumerate(meta["legacy"]):
            if not rename_selected(i):
                continue
            if not (picked_date(i) or lg.get("detected_date")):
                errors[f"date_{i}"] = (
                    f'Couldn\'t detect a date in "{lg["old_name"]}" — pick one here.'
                )
            if not (picked_category(i) or lg.get("detected_category")):
                errors[f"cat_{i}"] = (
                    f'Couldn\'t detect BPH/CAO in "{lg["old_name"]}" — choose one here.'
                )
        if errors:
            ack(response_action="errors", errors=errors)
            return
        # "clear" closes the WHOLE modal stack — this view was PUSHED on top of
        # the details modal, and a plain ack() would only pop back to it,
        # leaving the parent open to be resubmitted (→ a duplicate experiment).
        ack(response_action="clear")

        # Pass 2: apply. Rename failures here are exceptional (e.g. the folder
        # moved out of a directory) — degrade to a plain link and warn, rather
        # than dropping the whole experiment.
        post_message = lambda **kw: client.chat_postMessage(channel=meta["channel"], **kw)
        associations = list(meta["ready"])
        renamed_list = []
        for i, lg in enumerate(meta["legacy"]):
            if not rename_selected(i):
                associations.append(_link_assoc(lg["old_name"], lg["url"]))
                continue
            try:
                renamed = convert_legacy_folder(
                    lg["folder_id"],
                    picked_date(i),
                    picked_category(i),
                    action=picked_action(i),
                    num_scans=picked_scans(i),
                    user=meta.get("user_name", ""),
                )
            except LegacyConversionError as e:
                post_message(
                    text=f":warning: Couldn't rename `{lg['old_name']}`: {e} "
                    "— associated as a link instead."
                )
                associations.append(_link_assoc(lg["old_name"], lg["url"]))
                continue
            renamed_list.append(renamed)
            associations.append(
                _link_assoc(
                    renamed["name"],
                    renamed["url"],
                    note=f"(renamed from `{renamed['old_name']}`)",
                )
            )

        _announce_new_experiment(
            client,
            meta,
            associations,
            codename=meta.get("codename", ""),
            action=meta.get("action"),
            num_scans=meta.get("num_scans"),
            renamed_list=renamed_list,
        )

    @app.view("legacy_experiment")
    def handle_legacy_submit(ack, view, client):
        """Rename a legacy Box folder to the current naming scheme.

        Date and category are auto-detected from the folder name when possible
        (e.g. "2026-06-17 - FLR: CT" → date; "2026-06-23 - BPH" → date and
        category); the optional modal fields cover what detection misses, with
        explicit user input taking precedence over detection. The action /
        scan-count field for the folder's directory is required (the other is
        ignored); `user` is the converter's Slack handle.
        """
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]
        raw_link = (vals["link"]["link_input"]["value"] or "").strip()
        picked_date = vals["date"]["date_input"].get("selected_date")
        picked_option = vals["category"]["category_input"].get("selected_option")
        picked_category = picked_option["value"] if picked_option else None
        action = (vals["action"]["action_input"].get("value") or "").strip()
        num_scans = vals["scan_count"]["count_input"].get("value")
        register_children = bool(
            vals.get("register_children", {}).get("register", {}).get("selected_options")
        )
        index_children = bool(
            vals.get("index_children", {}).get("index", {}).get("selected_options")
        )

        link = BOX_FOLDER_LINK_RE.search(raw_link)
        if not link:
            ack(
                response_action="errors",
                errors={"link": "Paste a Box folder link (…box.com/folder/<id>)."},
            )
            return

        try:
            renamed = convert_legacy_folder(
                link.group(1),
                picked_date,
                picked_category,
                action=action,
                num_scans=num_scans,
                user=meta.get("user_name", ""),
            )
        except LegacyConversionError as e:
            ack(response_action="errors", errors={e.field: str(e)})
            return
        ack()

        post_message = lambda **kw: client.chat_postMessage(channel=meta["channel"], **kw)
        post_legacy_rename(
            post_message,
            meta["user"],
            renamed,
            created_by=meta.get("user_name", ""),
        )

        # Optionally pick up the folders inside as (linked) sub-experiments,
        # skipping any 'calibration' folder (a one-time robot calibration shared
        # by all children). Best-effort — never fails the rename.
        if register_children:
            try:
                children = [
                    c
                    for c in box_client.list_child_folders(link.group(1))
                    if "calibration" not in c["name"].lower()
                ]
                parent_ref = {
                    "folder_id": renamed["id"],
                    "name": renamed["name"],
                    "url": renamed["url"],
                    "dir_key": renamed["dir_key"],
                }
                linked = pickup_legacy_children(
                    parent_ref,
                    children,
                    index=index_children,
                    created_by=meta.get("user_name", ""),
                    on_date=detect_date(renamed["name"]) or "",
                    category=detect_category(renamed["name"]) or "",
                )
                if linked:
                    indexed = " (added to the index)" if index_children else ""
                    post_message(
                        text=f":deciduous_tree: Linked {linked} sub-experiment(s) "
                        f"under `{renamed['name']}`{indexed}."
                    )
            except Exception:
                logging.warning("could not pick up legacy children", exc_info=True)

    @app.view("subexperiment")
    def handle_subexperiment_submit(ack, view, client):
        """Create a sub-experiment under the pasted parent experiment.

        The child ALWAYS inherits the parent's category (there's no category
        field — they can never diverge), but its date is independent (a
        sub-experiment can be run later than its parent). The name uses the
        action format regardless of placement. Writes the parent→child link
        both ways.
        """
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]
        raw_link = (vals["parent_link"]["link_input"]["value"] or "").strip()
        codename = (vals["codename"]["codename_input"].get("value") or "").strip()
        picked_date = vals["date"]["date_input"].get("selected_date")
        action = (vals["action"]["action_input"].get("value") or "").strip()
        place_opt = vals["placement"]["placement_input"].get("selected_option")
        placement = place_opt["value"] if place_opt else "nested"
        index = bool(vals.get("index", {}).get("index_toggle", {}).get("selected_options"))

        link = BOX_FOLDER_LINK_RE.search(raw_link)
        if not link:
            ack(
                response_action="errors",
                errors={"parent_link": "Paste a Box folder link (…box.com/folder/<id>)."},
            )
            return
        if not action:
            ack(response_action="errors", errors={"action": "Enter an action."})
            return
        try:
            parent = resolve_parent(link.group(1))
        except SubexperimentError as e:
            ack(response_action="errors", errors={e.field: str(e)})
            return
        # A sub-experiment always takes the parent's category. If the parent's
        # category can't be read, we can't guarantee they match — refuse and
        # point the user at fixing the parent first.
        child_category = parent["category"]
        if not child_category:
            ack(
                response_action="errors",
                errors={
                    "parent_link": "Couldn't read the parent's category "
                    "(bph/cao/flr) from its name — convert the parent to the "
                    "naming scheme first (`/experiment legacy`), then retry."
                },
            )
            return
        ack()

        def update_ephemeral(text):
            if not meta.get("response_url"):
                return
            try:
                WebhookClient(meta["response_url"]).send(
                    text=text, replace_original=True, response_type="ephemeral"
                )
            except Exception:
                logging.warning("could not update ephemeral prompt", exc_info=True)

        finish_subexperiment(
            parent,
            child_category,
            placement,
            meta["user"],
            post_message=lambda **kw: client.chat_postMessage(channel=meta["channel"], **kw),
            update_ephemeral=update_ephemeral,
            user_handle=meta.get("user_name", ""),
            action=action,
            codename=codename,
            on_date=picked_date,
            created_by=meta.get("user_name", ""),
            index=index,
        )

    return app
