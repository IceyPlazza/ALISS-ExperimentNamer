"""Experiment lifecycle logic shared by the Slack handlers.

Association resolution, legacy-folder conversion, and the create/announce
flows live here so both the slash-command handlers and the modal-submit
handlers can call them. Nothing here talks to Bolt directly — callers pass
in `post_message` / `update_ephemeral` callables.
"""

import logging

from core.box import box_client, csv_index
from core.box.box_client import AmbiguousExperimentError, BoxNotConfiguredError
from core.slack.naming import (
    BOX_FOLDER_LINK_RE,
    CODENAME_RE,
    EXPERIMENT_NAME_RE,
    build_experiment_name,
    detect_category,
    detect_date,
    generate_codename,
    sanitize_segment,
)
from core.slack.views import BOX_NOT_READY


def _taken_codenames():
    """Codenames already in the CSV index, so name generation can re-roll to
    keep codenames unique. Best-effort — an empty set (no uniqueness check) if
    the index isn't reachable, so creation never blocks."""
    try:
        return csv_index.taken_codenames()
    except Exception:
        return set()


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


def convert_legacy_folder(
    folder_id,
    picked_date=None,
    picked_category=None,
    action=None,
    num_scans=None,
    user="",
) -> dict:
    """Rename a legacy Box folder to the current naming scheme.

    Shared by /experiment legacy and the associate-experiment dialog. Date and
    category are auto-detected from the folder name; explicit picks win over
    detection. The new name follows the directory's scheme, so the caller must
    supply the directory-specific segment — `action` for Experiments, `num_scans`
    for Scans — plus `user` (the converter's Slack handle). Returns
    {"id", "name", "url", "old_name", "dir_key"}. Raises LegacyConversionError
    for anything the user must fix.
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
            f'Couldn\'t detect BPH/CAO/FLR in "{old_name}" — choose one here.',
        )

    action = sanitize_segment(action)
    if dir_key == "scans":
        if not num_scans:
            raise LegacyConversionError(
                "scan_count",
                f'"{old_name}" is in the Scans directory — enter the number '
                "of scans.",
            )
    elif not action:
        raise LegacyConversionError(
            "action",
            f'"{old_name}" is in the Experiments directory — enter an action.',
        )

    codename = generate_codename(exclude=_taken_codenames())
    new_name = build_experiment_name(
        dir_key,
        category,
        codename,
        sanitize_segment(user),
        action=action,
        num_scans=num_scans,
        on_date=exp_date,
    )
    try:
        renamed = box_client.rename_folder(folder_id, new_name)
    except Exception as e:
        raise LegacyConversionError("link", f"Rename failed: {e}")
    return {**renamed, "old_name": old_name, "dir_key": dir_key}


class SubexperimentError(ValueError):
    """User-facing problem creating a sub-experiment. `field` names the modal
    block to attach the message to."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _dir_key_for_folder(parent_id):
    """Which top-level experiment directory a folder ultimately lives under,
    walking up its ancestry (so nested sub-experiment parents resolve too).
    None if it isn't inside either directory."""
    cur = parent_id
    for _ in range(12):  # bounded walk-up
        if cur is None:
            break
        dk = box_client.directory_key_for_parent(cur)
        if dk:
            return dk
        try:
            cur = box_client.get_folder_info(cur)["parent_id"]
        except Exception:
            break
    return None


def resolve_parent(folder_id: str) -> dict:
    """Resolve a pasted parent-experiment link to
    {folder_id, name, url, dir_key, category}. `category` may be None if the
    parent's name has no detectable category (the caller then trusts the user's
    picked category). Raises SubexperimentError(field, msg) for problems the
    user must fix."""
    try:
        info = box_client.get_folder_info(folder_id)
    except BoxNotConfiguredError:
        raise SubexperimentError("parent_link", BOX_NOT_READY)
    except Exception as e:
        raise SubexperimentError("parent_link", f"Couldn't read that folder: {e}")

    dir_key = _dir_key_for_folder(info["parent_id"])
    if dir_key is None:
        raise SubexperimentError(
            "parent_link",
            "That folder isn't inside either experiment directory — paste a "
            "link to an experiment folder.",
        )
    return {
        "folder_id": folder_id,
        "name": info["name"],
        "url": f"https://app.box.com/folder/{folder_id}",
        "dir_key": dir_key,
        "category": detect_category(info["name"]),
    }


# --------------------------------------------------------------------------
# Relationships stored in the Box folder description
#
# A folder description holds up to three app-managed sections, each a bullet
# list of `label | url` entries under a header line:
#   - "Associated experiments:" — symmetric peer links (several).
#   - "Parent experiment:"      — the one experiment this is a sub-experiment
#                                 of (directed; at most one entry).
#   - "Sub-experiments:"        — the children of this experiment (directed).
# Parsing is header-scoped and preserves any human-written preamble AND the
# other sections, so writing one never clobbers the rest.
# --------------------------------------------------------------------------

ASSOC_HEADER = "Associated experiments:"
PARENT_HEADER = "Parent experiment:"
CHILDREN_HEADER = "Sub-experiments:"

# All headers the app manages; used to bound each section when parsing.
KNOWN_HEADERS = [ASSOC_HEADER, PARENT_HEADER, CHILDREN_HEADER]


def _ref_mrkdwn(label: str, url: str | None) -> str:
    """A Slack link when we have a URL, else the label in backticks."""
    return f"<{url}|{label}>" if url else f"`{label}`"


def _serialize_section(header: str, entries: list[dict]) -> str:
    """Serialize one section (header + `- label | url` bullets), "" if empty."""
    if not entries:
        return ""
    lines = [header]
    for e in entries:
        lines.append(f"- {e['label']} | {e.get('url') or ''}")
    return "\n".join(lines)


def _parse_section(description: str, header: str) -> list[dict]:
    """Parse the `label | url` entries under `header`, header-scoped: stops at
    the next known section header and tolerates junk lines. [] if absent."""
    lines = (description or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i + 1
            break
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        s = line.strip()
        if s in KNOWN_HEADERS:  # start of the next section
            break
        if not s.startswith("- "):
            continue
        label, _, url = s[2:].partition("|")
        label, url = label.strip(), url.strip()
        if not label:
            continue
        out.append({"label": label, "url": url or None})
    return out


def _preamble(description: str) -> str:
    """The human-written text before the first known section header."""
    lines = (description or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() in KNOWN_HEADERS:
            return "\n".join(lines[:i]).rstrip()
    return "\n".join(lines).rstrip()


def _set_section(existing: str, header: str, entries: list[dict]) -> str:
    """Return `existing` with `header`'s section replaced by `entries` (removed
    if empty), preserving the preamble and every OTHER known section."""
    sections = {h: _parse_section(existing, h) for h in KNOWN_HEADERS}
    sections[header] = entries
    blocks = []
    preamble = _preamble(existing)
    if preamble:
        blocks.append(preamble)
    for h in KNOWN_HEADERS:
        block = _serialize_section(h, sections[h])
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def associations_to_description(associations: list[dict]) -> str:
    """Serialize associations to a standalone description block (used to seed a
    brand-new folder's description). Human-readable and round-trippable."""
    return _serialize_section(ASSOC_HEADER, associations)


def parse_associations(description: str) -> list[dict]:
    """The `Associated experiments:` entries from a folder description ([] if
    none)."""
    return _parse_section(description, ASSOC_HEADER)


def set_associations_in_description(existing: str, associations: list[dict]) -> str:
    """Replace just the associations section, preserving preamble + other
    sections."""
    return _set_section(existing, ASSOC_HEADER, associations)


def parse_parent(description: str) -> dict | None:
    """The single `Parent experiment:` entry, or None."""
    got = _parse_section(description, PARENT_HEADER)
    return got[0] if got else None


def set_parent_in_description(existing: str, parent: dict | None) -> str:
    """Set (or clear, if None) the parent section, preserving everything else."""
    return _set_section(existing, PARENT_HEADER, [parent] if parent else [])


def parse_children(description: str) -> list[dict]:
    """The `Sub-experiments:` entries from a folder description ([] if none)."""
    return _parse_section(description, CHILDREN_HEADER)


def set_children_in_description(existing: str, children: list[dict]) -> str:
    """Replace just the sub-experiments section, preserving everything else."""
    return _set_section(existing, CHILDREN_HEADER, children)


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


def add_child_reference(parent_folder_id: str, child: dict) -> None:
    """Append `child` ({label, url}) to the parent folder's `Sub-experiments:`
    section (deduped by folder id, preserving everything else). Best-effort."""
    child_id = _folder_id_from_url(child.get("url"))
    try:
        existing = box_client.get_folder_description(parent_folder_id)
        current = parse_children(existing)
        if any(_folder_id_from_url(c.get("url")) == child_id for c in current):
            return  # already referenced
        current.append(child)
        box_client.set_folder_description(
            parent_folder_id, set_children_in_description(existing, current)
        )
    except Exception:
        logging.warning(
            "could not add sub-experiment reference to folder %s",
            parent_folder_id,
            exc_info=True,
        )


def purge_references(deleted_ids) -> None:
    """Strip any association pointing at a deleted folder from EVERY other
    experiment folder's description (both directories).

    Called after deleting experiment(s) so nothing is left pointing at a
    now-deleted folder. Unlike a targeted back-reference removal, this crawls
    all folders and doesn't rely on the deleted folder's own association list
    being symmetric — so it also fixes legacy/one-directional links. Best
    effort per folder; pass all ids from a `delete empty` prune at once so the
    crawl happens a single time.
    """
    deleted_ids = {i for i in deleted_ids if i}
    if not deleted_ids:
        return
    try:
        folders = box_client.list_experiment_folders(include_nested=True)
    except Exception:
        logging.warning("could not list folders to purge references", exc_info=True)
        return
    for folder in folders:
        if folder["id"] in deleted_ids:
            continue  # itself deleted
        try:
            existing = box_client.get_folder_description(folder["id"])
            desc = existing
            changed = False

            # Associations pointing at a deleted folder.
            assoc = parse_associations(desc)
            kept = [a for a in assoc if _folder_id_from_url(a.get("url")) not in deleted_ids]
            if len(kept) != len(assoc):
                desc = set_associations_in_description(desc, kept)
                changed = True

            # Parent pointer at a deleted folder → this becomes an orphan.
            parent = parse_parent(desc)
            if parent and _folder_id_from_url(parent.get("url")) in deleted_ids:
                desc = set_parent_in_description(desc, None)
                changed = True

            # Sub-experiment pointers at deleted children.
            children = parse_children(desc)
            kept_kids = [
                c for c in children if _folder_id_from_url(c.get("url")) not in deleted_ids
            ]
            if len(kept_kids) != len(children):
                desc = set_children_in_description(desc, kept_kids)
                changed = True

            if changed:
                box_client.set_folder_description(folder["id"], desc)
        except Exception:
            logging.warning(
                "could not purge references from folder %s",
                folder["id"],
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
    if not (EXPERIMENT_NAME_RE.match(raw) or CODENAME_RE.match(raw)):
        raise AssociationError(
            "Enter a codename, a full experiment name, or a Box folder link."
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


def post_legacy_rename(post_message, user_id, renamed, created_by=""):
    """Announce a legacy-folder conversion to the channel. Called for every
    rename, whether via /experiment legacy or the new-experiment dialog.

    created_by is the name recorded in the CSV index for the person who
    converted the folder (the folder may have had no prior row)."""
    # Update the CSV index: the folder id is unchanged, so this refreshes its
    # row (new name/date/category) and records original_name. Best-effort.
    try:
        csv_index.sync_renamed(renamed, renamed["old_name"], created_by=created_by)
    except Exception:
        logging.warning("could not sync legacy rename to the index", exc_info=True)

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
    user_handle="",
    action=None,
    num_scans=None,
    codename=None,
    associations=None,
    created_by="",
):
    """Build the name, create the Box folder, and announce the result.

    The name follows the directory's scheme:
      Experiments: YYYY-MM-DD-codename-CATEGORY-action-user
      Scans:       YYYY-MM-DD-codename-CATEGORY-(num_scans)-user
    `codename` is used when given (a user override), else a unique one is
    generated; `action`/`num_scans` are the directory-specific segment;
    `user_handle` is the creator's Slack handle placed in the name.
    associations is a list of {"label", "url", "mrkdwn"} dicts: they're
    persisted to the new folder's Box description (so `track` can list them)
    and rendered into the announcement.
    created_by is the Slack handle recorded in the CSV index; "" when
    unavailable. post_message posts to the channel; update_ephemeral replaces
    the picker prompt so it can't be reused.
    """
    associations = associations or []
    directory = box_client.directory_info(dir_key)
    code = sanitize_segment(codename) or generate_codename(exclude=_taken_codenames())
    user_handle = sanitize_segment(user_handle)
    action = sanitize_segment(action)
    experiment_name = build_experiment_name(
        dir_key, category, code, user_handle, action=action, num_scans=num_scans
    )
    # Exact per-segment columns for the index (no name-parsing ambiguity).
    parts = {
        "codename": code,
        "category": category,
        "action": action if dir_key != "scans" else "",
        "num_scans": str(int(num_scans)) if dir_key == "scans" and num_scans else "",
        "slack_user": user_handle,
    }
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

    # Record the new experiment in the CSV index — best-effort so an index
    # hiccup never breaks the (already-announced) creation.
    if created:
        try:
            csv_index.sync_created(
                created,
                created_by,
                associated_with="; ".join(a["label"] for a in associations),
                parts=parts,
            )
        except Exception:
            logging.warning("could not sync new experiment to the index", exc_info=True)

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


def finish_subexperiment(
    parent,
    category,
    placement,
    user_id,
    post_message,
    update_ephemeral,
    user_handle="",
    action=None,
    codename=None,
    on_date=None,
    created_by="",
    index=False,
):
    """Create a sub-experiment folder under `parent` and announce it.

    `parent` is {"folder_id", "name", "url", "dir_key"} (resolved from the
    pasted link). `placement` is "nested" (inside the parent's folder),
    "experiments", or "scans" (top level of that directory). The name always
    uses the action format `YYYY-MM-DD-codename-CATEGORY-action-user`, regardless
    of placement; `category` is the (validated) inherited category. The
    parent→child link is written on both sides: the child's description gets a
    `Parent experiment:` section, the parent's gets a `Sub-experiments:` entry.

    CSV indexing: a top-level placement is always indexed (it's a primary
    experiment). A NESTED child is indexed only if `index` is True — otherwise
    the parent's description is the record and the child stays out of the CSV.
    """
    code = sanitize_segment(codename) or generate_codename(exclude=_taken_codenames())
    uh = sanitize_segment(user_handle)
    act = sanitize_segment(action)
    experiment_name = build_experiment_name(
        "experiments", category, code, uh, action=act, on_date=on_date
    )

    if placement == "nested":
        child_dir_key = parent.get("dir_key") or "experiments"
        parent_folder_id = parent["folder_id"]
    else:
        child_dir_key = placement  # "experiments" or "scans"
        parent_folder_id = None

    directory = box_client.directory_info(child_dir_key)
    parent_ref = {"label": parent["name"], "url": parent["url"]}
    description = set_parent_in_description("", parent_ref)
    parts = {
        "codename": code,
        "category": category,
        "action": act,
        "num_scans": "",
        "slack_user": uh,
        "parent_experiment": parent["name"],
    }

    folder_note = ""
    created = None
    try:
        created = box_client.create_experiment_folder(
            experiment_name,
            child_dir_key,
            description=description,
            parent_folder_id=parent_folder_id,
        )
        folder_note = f"\n:file_folder: <{created['url']}|Open in Box>"
    except BoxNotConfiguredError:
        pass  # Box not set up: name announced without a folder link
    except Exception as e:
        logging.exception("Box folder creation failed for %s", experiment_name)
        folder_note = f"\n:warning: Couldn't create the Box folder: {e}"

    if created:
        # Mirror the child onto the parent's Sub-experiments section (always —
        # the link is the record even when we don't index the child).
        add_child_reference(
            parent["folder_id"], {"label": created["name"], "url": created["url"]}
        )
        # Index a top-level child always; a nested child only if opted in.
        if placement != "nested" or index:
            try:
                csv_index.sync_created(created, created_by, parts=parts)
            except Exception:
                logging.warning(
                    "could not sync sub-experiment to the index", exc_info=True
                )

    where = "Nested inside" if placement == "nested" else "Directory:"
    update_ephemeral(f"Generated sub-experiment: {experiment_name}")
    post_message(
        text=(
            f"New sub-experiment: {experiment_name} under {parent['name']} "
            f"(started by <@{user_id}>)"
        ),
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":sparkles: New *sub-experiment* started by <@{user_id}>:\n"
                        f"*`{experiment_name}`*\n"
                        f":deciduous_tree: Parent: {_ref_mrkdwn(parent['name'], parent['url'])}\n"
                        f":file_folder: {where} _{directory['label']}_"
                        + folder_note
                    ),
                },
            },
        ],
    )


def pickup_legacy_children(
    parent, children, index=False, created_by="", on_date="", category=""
):
    """Register existing subfolders of a just-converted legacy parent as
    sub-experiments — LINK ONLY (the child folders keep their names).

    `parent` is a convert_legacy_folder() result dict (id/name/url/dir_key).
    `children` is a list of {id, name, url}; the caller has already filtered out
    calibration folders and non-experiment items. For each child we write its
    `Parent experiment:` description section and add it to the parent's
    `Sub-experiments:` section (bidirectional). If `index` is True we also add a
    CSV row (kept name; codename derived from the folder name; date/category
    inherited from the parent). Returns the number of children linked. Best
    effort per child."""
    parent_ref = {"label": parent["name"], "url": parent["url"]}
    linked = 0
    for child in children:
        try:
            existing = box_client.get_folder_description(child["id"])
            box_client.set_folder_description(
                child["id"], set_parent_in_description(existing, parent_ref)
            )
        except Exception:
            logging.warning(
                "could not set parent on child folder %s", child["id"], exc_info=True
            )
            continue
        add_child_reference(
            parent["folder_id"], {"label": child["name"], "url": child["url"]}
        )
        linked += 1
        if index:
            try:
                csv_index.sync_created(
                    {
                        "id": child["id"],
                        "name": child["name"],
                        "url": child["url"],
                        "dir_key": parent["dir_key"],
                    },
                    created_by,
                    parts={
                        "codename": sanitize_segment(child["name"]),
                        "category": category,
                        "action": "",
                        "num_scans": "",
                        "slack_user": "",
                        "parent_experiment": parent["name"],
                        "created_date": on_date,
                    },
                )
            except Exception:
                logging.warning(
                    "could not index child folder %s", child["id"], exc_info=True
                )
    return linked
