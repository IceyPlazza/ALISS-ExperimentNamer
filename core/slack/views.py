"""Slack Block Kit builders and user-facing message strings.

Everything here turns app state into the JSON that Slack renders: the
category/directory pickers, the two modals, list formatting, and the static
usage / not-connected notices. Kept free of Bolt handler wiring so the
handlers module reads as pure control flow.
"""

import json

from core.box import box_client
from core.box.box_client import BOX_DIRECTORIES
from core.slack.naming import BOX_FOLDER_LINK_RE, EXPERIMENT_CATEGORIES

USAGE = (
    "*Usage:* `/experiment <subcommand>`\n"
    "• `/experiment new` — generate a name for a new experiment\n"
    "• `/experiment track <name | word-word | codename>` — find an experiment's Box folder\n"
    "• `/experiment date <YYYY-MM-DD>` — list experiments from a date\n"
    "• `/experiment legacy [box-link]` — rename a legacy folder to the "
    "naming scheme\n"
    "• `/experiment delete <name>` — delete an experiment (must be empty)\n"
    "• `/experiment delete empty` — prune all experiments with no files\n"
    f"• `/experiment category <{'|'.join(EXPERIMENT_CATEGORIES)}>` — list experiments by category (both directories)\n"
    f"• `/experiment scans [{'|'.join(EXPERIMENT_CATEGORIES)}]` — list experiments in the Scans directory\n"
    f"• `/experiment experiments [{'|'.join(EXPERIMENT_CATEGORIES)}]` — list experiments in the Experiments directory"
)

BOX_NOT_READY = (
    ":construction: Box isn't connected yet — set the BOX_* variables in "
    ".env (see README) and restart the app."
)


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


def directory_buttons(category: str) -> list:
    return [
        {
            "type": "button",
            "action_id": f"pick_directory_{key}",
            "text": {
                "type": "plain_text",
                "text": box_client.directory_info(key)["label"],
            },
            "value": f"{category}:{key}",
        }
        for key in BOX_DIRECTORIES
    ]


def format_experiment_list(experiments: list[dict]) -> str:
    """Render [{'name', 'url', 'directory'}] as a Slack mrkdwn bullet list,
    noting which Box directory each experiment lives in."""
    return "\n".join(
        f"• <{e['url']}|{e['name']}> — _{e['directory']}_" for e in experiments
    )


def _category_select_options() -> list:
    return [
        {
            "text": {"type": "plain_text", "text": c.upper()},
            "value": c,
        }
        for c in EXPERIMENT_CATEGORIES
    ]


def legacy_modal_view(meta: dict, prefill_link: str = "") -> dict:
    """The `legacy_experiment` modal for /experiment legacy: Box link +
    optional date/category. Pre-fills the link field when the arg already
    looks like a Box link."""
    link_element = {
        "type": "plain_text_input",
        "action_id": "link_input",
        "placeholder": {
            "type": "plain_text",
            "text": "https://…box.com/folder/123456789",
        },
    }
    if BOX_FOLDER_LINK_RE.search(prefill_link):
        link_element["initial_value"] = prefill_link
    return {
        "type": "modal",
        "callback_id": "legacy_experiment",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Convert legacy folder"},
        "submit": {"type": "plain_text", "text": "Rename"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "link",
                "label": {"type": "plain_text", "text": "Box folder link"},
                "element": link_element,
            },
            {
                "type": "input",
                "block_id": "date",
                "optional": True,
                "label": {"type": "plain_text", "text": "Experiment date"},
                "hint": {
                    "type": "plain_text",
                    "text": "Leave empty if the folder name already "
                    "contains the date.",
                },
                "element": {"type": "datepicker", "action_id": "date_input"},
            },
            {
                "type": "input",
                "block_id": "category",
                "optional": True,
                "label": {"type": "plain_text", "text": "Category"},
                "hint": {
                    "type": "plain_text",
                    "text": "Leave empty if the folder name already says "
                    "BPH or CAO.",
                },
                "element": {
                    "type": "static_select",
                    "action_id": "category_input",
                    "options": _category_select_options(),
                },
            },
        ],
    }


def _details_extra_block(dir_key: str) -> dict:
    """The directory-specific suffix input: scan count (scans) or codename
    (experiments)."""
    if dir_key == "scans":
        return {
            "type": "input",
            "block_id": "scan_count",
            "optional": True,
            "label": {"type": "plain_text", "text": "Number of scans"},
            "hint": {
                "type": "plain_text",
                "text": "Appended to the name as …-word-word-(n).",
            },
            "element": {
                "type": "number_input",
                "action_id": "count_input",
                "is_decimal_allowed": False,
                "min_value": "1",
            },
        }
    return {
        "type": "input",
        "block_id": "codename",
        "optional": True,
        "label": {"type": "plain_text", "text": "Codename"},
        "hint": {
            "type": "plain_text",
            "text": "e.g. a model label; appended to the name as "
            "…-word-word-codename.",
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "codename_input",
            "placeholder": {"type": "plain_text", "text": "e.g. modelA"},
        },
    }


def details_modal_view(meta: dict, dir_key: str) -> dict:
    """The `new_experiment_details` modal for the "Add details…" flow:
    directory-specific suffix + optional association(s).

    The association field is multiline — one entry per line — and accepts
    word-word combos, full experiment names, or Box folder links. If a link
    points at a folder that isn't named to the scheme, submitting pushes the
    `associate_legacy` modal to handle it (rename or keep); there are no
    inline legacy-rename fields here anymore."""
    return {
        "type": "modal",
        "callback_id": "new_experiment_details",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Experiment details"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _details_extra_block(dir_key),
            {
                "type": "input",
                "block_id": "assoc",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Associated experiments",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "One per line. Each can be a word-word combo, a "
                    "full experiment name, or a Box folder link.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "assoc_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. coolly-cut\nhttps://…box.com/folder/123",
                    },
                },
            },
        ],
    }


def associate_legacy_view(meta: dict, legacy_entries: list[dict]) -> dict:
    """The `associate_legacy` modal, pushed from the details modal when one
    or more associated Box links point at folders not named to the scheme.

    For each such folder the user can rename it to the naming scheme (the
    default) or keep it as a plain link. Detected date/category pre-fill the
    optional pickers, which only matter when renaming and detection missed.
    Per-folder block ids are suffixed with the entry index."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":card_index_dividers: Some associated folders aren't "
                    "named to the scheme yet. For each, rename it to the "
                    "naming scheme or keep it as a link."
                ),
            },
        }
    ]
    for i, lg in enumerate(legacy_entries):
        rename_option = {
            "text": {
                "type": "plain_text",
                "text": "Rename it to the naming scheme",
            },
            "value": "rename",
        }
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Legacy folder:* <{lg['url']}|{lg['old_name']}>",
                },
            }
        )
        blocks.append(
            {
                "type": "input",
                "block_id": f"act_{i}",
                "optional": True,
                "label": {"type": "plain_text", "text": "Action"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "act",
                    # Default to rename; unticking keeps it as a plain link.
                    "initial_options": [rename_option],
                    "options": [rename_option],
                },
            }
        )
        date_element = {"type": "datepicker", "action_id": "date"}
        if lg.get("detected_date"):
            date_element["initial_date"] = lg["detected_date"]
        blocks.append(
            {
                "type": "input",
                "block_id": f"date_{i}",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Its date (renaming only)",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "Leave empty if the folder name already contains "
                    "the date.",
                },
                "element": date_element,
            }
        )
        cat_element = {
            "type": "static_select",
            "action_id": "cat",
            "options": _category_select_options(),
        }
        if lg.get("detected_category"):
            cat_element["initial_option"] = {
                "text": {
                    "type": "plain_text",
                    "text": lg["detected_category"].upper(),
                },
                "value": lg["detected_category"],
            }
        blocks.append(
            {
                "type": "input",
                "block_id": f"cat_{i}",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Its category (renaming only)",
                },
                "hint": {
                    "type": "plain_text",
                    "text": "Leave empty if the folder name already says "
                    "BPH or CAO.",
                },
                "element": cat_element,
            }
        )
    return {
        "type": "modal",
        "callback_id": "associate_legacy",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Legacy associations"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }
