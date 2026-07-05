"""Experiment lifecycle logic shared by the Slack handlers.

Association resolution, legacy-folder conversion, and the create/announce
flows live here so both the slash-command handlers and the modal-submit
handlers can call them. Nothing here talks to Bolt directly — callers pass
in `post_message` / `update_ephemeral` callables.
"""

import logging

from core.box import box_client
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack.naming import (
    BOX_FOLDER_LINK_RE,
    EXPERIMENT_NAME_RE,
    WORD_COMBO_RE,
    detect_category,
    detect_date,
    generate_experiment_name,
    generate_word_combo,
)
from core.slack.views import BOX_NOT_READY


class AssociationError(ValueError):
    """User-facing problem with an associated-experiment input; the message
    is shown as a validation error inside the modal."""


class LegacyConversionError(Exception):
    """A legacy-folder conversion couldn't proceed. `field` names the
    logical input at fault ("link", "date" or "category") so each modal can
    attach the message to its own matching block."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def convert_legacy_folder(folder_id, picked_date=None, picked_category=None) -> dict:
    """Rename a legacy Box folder to YYYY-MM-DD-category-word-word.

    Shared by /experiment legacy and the associate-experiment dialog. Date
    and category are auto-detected from the folder name; explicit picks win
    over detection. Returns {"id", "name", "url", "old_name", "dir_key"}.
    Raises LegacyConversionError for anything the user must fix.
    """
    try:
        info = box_client.get_folder_info(folder_id)
    except BoxNotConfiguredError:
        raise LegacyConversionError("link", BOX_NOT_READY)
    except Exception as e:
        raise LegacyConversionError("link", f"Couldn't read that folder: {e}")

    old_name = info["name"]
    dir_key = box_client.directory_key_for_parent(info["parent_id"])
    if dir_key is None:
        raise LegacyConversionError(
            "link",
            "That folder isn't inside either experiment directory — move "
            "it there first.",
        )

    exp_date = picked_date or detect_date(old_name)
    if not exp_date:
        raise LegacyConversionError(
            "date", f'Couldn\'t detect a date in "{old_name}" — pick one here.'
        )
    category = picked_category or detect_category(old_name)
    if not category:
        raise LegacyConversionError(
            "category",
            f'Couldn\'t detect BPH/CAO in "{old_name}" — choose one here.',
        )

    new_name = f"{exp_date}-{category}-{generate_word_combo()}"
    try:
        renamed = box_client.rename_folder(folder_id, new_name)
    except Exception as e:
        raise LegacyConversionError("link", f"Rename failed: {e}")
    return {**renamed, "old_name": old_name, "dir_key": dir_key}


# --------------------------------------------------------------------------
# Associations
#
# An experiment can be associated with several others. Each association is a
# dict {"label", "url", "mrkdwn"}: `label`/`url` are persisted to the new
# folder's Box description (see serialize/parse below) so `/experiment track`
# can list them; `mrkdwn` is the rendered reference for the announcement.
# --------------------------------------------------------------------------

ASSOC_HEADER = "Associated experiments:"


def _ref_mrkdwn(label: str, url: str | None) -> str:
    """A Slack link when we have a URL, else the label in backticks."""
    return f"<{url}|{label}>" if url else f"`{label}`"


def associations_to_description(associations: list[dict]) -> str:
    """Serialize associations into a Box folder description — human-readable
    (the team may read it in Box) and round-trippable by parse_associations.

        Associated experiments:
        - 2026-07-05-bph-a-b | https://app.box.com/folder/9
    """
    if not associations:
        return ""
    lines = [ASSOC_HEADER]
    for a in associations:
        lines.append(f"- {a['label']} | {a.get('url') or ''}")
    return "\n".join(lines)


def parse_associations(description: str) -> list[dict]:
    """Parse the association list out of a folder description.

    Only reads the block AFTER the `Associated experiments:` header, so any
    human-written text before it is ignored (and preserved by
    set_associations_in_description). Tolerant of hand-edits: skips lines that
    aren't association bullets. Returns [] if there's no header."""
    lines = (description or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == ASSOC_HEADER:
            start = i + 1
            break
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        line = line.strip()
        if not line.startswith("- "):
            continue
        label, _, url = line[2:].partition("|")
        label, url = label.strip(), url.strip()
        if not label:
            continue
        out.append({"label": label, "url": url or None})
    return out


def set_associations_in_description(existing: str, associations: list[dict]) -> str:
    """Return `existing` with its associations block replaced by
    `associations`, preserving any human-written text before the header.

    Used for the write-back onto associated folders, whose descriptions may
    contain real content we must not clobber."""
    lines = (existing or "").splitlines()
    preamble = lines
    for i, line in enumerate(lines):
        if line.strip() == ASSOC_HEADER:
            preamble = lines[:i]
            break
    preamble_text = "\n".join(preamble).rstrip()
    block = associations_to_description(associations)
    if preamble_text and block:
        return f"{preamble_text}\n\n{block}"
    return preamble_text or block


def _folder_id_from_url(url: str | None) -> str | None:
    """Extract the Box folder id from an association URL, or None."""
    if not url:
        return None
    m = BOX_FOLDER_LINK_RE.search(url)
    return m.group(1) if m else None


def add_back_references(experiment: dict, associations: list[dict]) -> None:
    """Make associations bidirectional: for each association that points at a
    known Box folder, append a back-reference to `experiment` onto that
    folder's description (preserving its existing content, deduped by folder
    id). Best-effort — a folder we can't read/write is logged and skipped.

    `experiment` is a create_experiment_folder() dict (name + url)."""
    back = {"label": experiment["name"], "url": experiment["url"]}
    back_id = _folder_id_from_url(back["url"])
    for a in associations:
        folder_id = _folder_id_from_url(a.get("url"))
        if not folder_id or folder_id == back_id:
            continue  # url-less association, or a self-reference
        try:
            existing = box_client.get_folder_description(folder_id)
            current = parse_associations(existing)
            if any(_folder_id_from_url(c.get("url")) == back_id for c in current):
                continue  # already back-referenced
            current.append(back)
            box_client.set_folder_description(
                folder_id, set_associations_in_description(existing, current)
            )
        except Exception:
            logging.warning(
                "could not add back-reference to folder %s", folder_id, exc_info=True
            )


def remove_back_references(experiment: dict, associations: list[dict]) -> None:
    """Undo add_back_references: drop the back-reference to `experiment` from
    each associated folder's description. Called before deleting an
    experiment so the other side isn't left with a dangling link.

    `experiment` is a find/list dict (name + url + id)."""
    back_id = _folder_id_from_url(experiment.get("url"))
    if not back_id:
        return
    for a in associations:
        folder_id = _folder_id_from_url(a.get("url"))
        if not folder_id or folder_id == back_id:
            continue
        try:
            existing = box_client.get_folder_description(folder_id)
            current = parse_associations(existing)
            kept = [c for c in current if _folder_id_from_url(c.get("url")) != back_id]
            if len(kept) == len(current):
                continue  # nothing pointed back at us
            box_client.set_folder_description(
                folder_id, set_associations_in_description(existing, kept)
            )
        except Exception:
            logging.warning(
                "could not remove back-reference from folder %s",
                folder_id,
                exc_info=True,
            )


def classify_association(raw: str) -> dict:
    """Classify one associated-experiment entry.

    Returns either:
      {"kind": "ready", "assoc": {"label", "url", "mrkdwn"}}
          — a fully resolved association, or
      {"kind": "legacy", "legacy": {"folder_id", "old_name", "url",
                                    "detected_date", "detected_category"}}
          — a Box link to a folder that does NOT follow the naming scheme, so
            the caller should offer to rename it (see the legacy dialog).

    Raises AssociationError for input the user must fix.
    """
    link = BOX_FOLDER_LINK_RE.search(raw)
    if link:
        folder_id = link.group(1)
        url = f"https://app.box.com/folder/{folder_id}"
        try:
            name = box_client.get_folder_name(folder_id)
        except Exception:
            name = None  # no access / Box not configured → keep bare link
        if name is None:
            return {"kind": "ready", "assoc": {"label": url, "url": url, "mrkdwn": url}}
        if EXPERIMENT_NAME_RE.match(name):
            return {
                "kind": "ready",
                "assoc": {"label": name, "url": url, "mrkdwn": f"<{url}|{name}>"},
            }
        # Readable but not named to the scheme → a legacy folder.
        return {
            "kind": "legacy",
            "legacy": {
                "folder_id": folder_id,
                "old_name": name,
                "url": url,
                "detected_date": detect_date(name),
                "detected_category": detect_category(name),
            },
        }
    if raw.startswith("http"):
        raise AssociationError(
            "Only Box folder links are supported (…box.com/folder/<id>)."
        )
    if not (EXPERIMENT_NAME_RE.match(raw) or WORD_COMBO_RE.match(raw)):
        raise AssociationError(
            "Enter a word-word combo, a full experiment name, or a Box "
            "folder link."
        )
    try:
        folder = box_client.find_experiment_folder(raw)
        return {
            "kind": "ready",
            "assoc": {
                "label": folder["name"],
                "url": folder["url"],
                "mrkdwn": f"<{folder['url']}|{folder['name']}>",
            },
        }
    except BoxNotConfiguredError:
        return {"kind": "ready", "assoc": {"label": raw, "url": None, "mrkdwn": f"`{raw}`"}}
    except AmbiguousExperimentError as e:
        raise AssociationError(
            "That combo matches several experiments: "
            + ", ".join(e.candidates[:5])
            + " — use the full name."
        )
    except KeyError:
        raise AssociationError(
            "No experiment found with that name — check it, or paste the "
            "Box folder link instead."
        )


def resolve_association(raw: str) -> str:
    """Resolve a single association entry to an mrkdwn reference.

    Thin wrapper over classify_association: a legacy (unstructured) Box link
    resolves to a link labelled with its current name rather than triggering
    the rename dialog — used where there's no interactive follow-up.
    """
    entry = classify_association(raw)
    if entry["kind"] == "ready":
        return entry["assoc"]["mrkdwn"]
    lg = entry["legacy"]
    return f"<{lg['url']}|{lg['old_name']}>"


def post_legacy_rename(post_message, user_id, renamed):
    """Announce a legacy-folder conversion to the channel. Called for every
    rename, whether via /experiment legacy or the new-experiment dialog."""
    directory = box_client.directory_info(renamed["dir_key"])
    post_message(
        text=(
            f"Legacy experiment renamed: {renamed['old_name']} -> "
            f"{renamed['name']} (by <@{user_id}>)"
        ),
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":card_index_dividers: Legacy folder converted by "
                        f"<@{user_id}>:\n"
                        f"• Was: `{renamed['old_name']}`\n"
                        f"• Now: *`{renamed['name']}`*\n"
                        f":file_folder: Directory: _{directory['label']}_ — "
                        f"<{renamed['url']}|Open in Box>"
                    ),
                },
            },
        ],
    )


def post_delete(post_message, user_id, folder):
    """Announce a single experiment deletion to the channel. `folder` is a
    find_experiment_folder() dict (name + directory)."""
    post_message(
        text=(
            f"Experiment deleted: {folder['name']} from "
            f"{folder['directory']} (by <@{user_id}>)"
        ),
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":wastebasket: Experiment deleted by <@{user_id}>:\n"
                        f"*`{folder['name']}`*\n"
                        f":file_folder: Directory: _{folder['directory']}_"
                    ),
                },
            },
        ],
    )


def post_prune(post_message, user_id, deleted):
    """Announce a `delete empty` prune (possibly several folders) to the
    channel. `deleted` is a list of find/list dicts (name + directory)."""
    lines = "\n".join(
        f"• `{f['name']}` — _{f['directory']}_" for f in deleted
    )
    post_message(
        text=(
            f"Pruned {len(deleted)} empty experiment(s) (by <@{user_id}>)"
        ),
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":wastebasket: <@{user_id}> pruned {len(deleted)} "
                        f"empty experiment(s):\n{lines}"
                    ),
                },
            },
        ],
    )


def finish_new_experiment(
    category,
    dir_key,
    user_id,
    post_message,
    update_ephemeral,
    associations=None,
    suffix="",
):
    """Generate the name, create the Box folder, and announce the result.

    suffix is appended verbatim to the generated name: "-(3)" for a scan
    count, "-codename" for a model codename.
    associations is a list of {"label", "url", "mrkdwn"} dicts: they're
    persisted to the new folder's Box description (so `track` can list them)
    and rendered into the announcement.
    post_message posts to the channel; update_ephemeral replaces the picker
    prompt so it can't be reused.
    """
    associations = associations or []
    directory = box_client.directory_info(dir_key)
    experiment_name = generate_experiment_name(category) + suffix
    description = associations_to_description(associations)

    folder_note = ""
    created = None
    try:
        created = box_client.create_experiment_folder(
            experiment_name, dir_key, description=description
        )
        folder_note = f"\n:file_folder: <{created['url']}|Open in Box>"
    except BoxNotConfiguredError:
        pass  # Box not set up: name is announced without a folder link
    except Exception as e:
        logging.exception("Box folder creation failed for %s", experiment_name)
        folder_note = f"\n:warning: Couldn't create the Box folder: {e}"

    # Mirror the association onto each associated folder (bidirectional).
    if created and associations:
        add_back_references(created, associations)

    if associations:
        assoc_lines = "\n".join(f"  • {a['mrkdwn']}" for a in associations)
        assoc_note = f"\n:link: Associated with:\n{assoc_lines}"
    else:
        assoc_note = ""

    update_ephemeral(f"Generated: {experiment_name} in {directory['label']}")
    post_message(
        text=(
            f"New experiment: {experiment_name} in {directory['label']} "
            f"(started by <@{user_id}>)"
        ),
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":sparkles: New experiment started by <@{user_id}>:\n"
                        f"*`{experiment_name}`*\n"
                        f":file_folder: Directory: _{directory['label']}_"
                        + folder_note
                        + assoc_note
                    ),
                },
            },
        ],
    )
