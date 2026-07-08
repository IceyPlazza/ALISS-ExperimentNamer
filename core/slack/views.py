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
    "• `/experiment subexperiment [box-link]` — create an experiment under a parent experiment\n"
    "• `/experiment track <name | codename>` — find an experiment's Box folder\n"
    "• `/experiment date <YYYY-MM-DD>` — list experiments from a date\n"
    "• `/experiment legacy [box-link]` — rename a legacy folder to the "
    "naming scheme\n"
    "• `/experiment delete <name>` — delete an experiment (must be empty)\n"
    "• `/experiment delete empty` — prune all experiments with no files\n"
    f"• `/experiment category <{'|'.join(EXPERIMENT_CATEGORIES)}>` — list experiments by category (both directories)\n"
    f"• `/experiment scans [{'|'.join(EXPERIMENT_CATEGORIES)}]` — list experiments in the Scans directory\n"
    f"• `/experiment experiments [{'|'.join(EXPERIMENT_CATEGORIES)}]` — list experiments in the Experiments directory\n"
    "• `/experiment database <retrieve|update>` — get a link to the experiment index CSV, or reconcile it against Box\n"
    "• `/experiment help` — a full guide to every command"
)

HELP = (
    ":test_tube: *ExperimentNamer — full guide*\n\n"
    "This app generates and tracks experiment names in Box. Names are built "
    "per directory:\n"
    "• _Experiments_: `YYYY-MM-DD-codename-CATEGORY-action-user` — e.g. "
    "`2026-07-06-decorous-harbor-BPH-segmentation-iven.chen`\n"
    "• _Scans_: `YYYY-MM-DD-codename-CATEGORY-(n)-user` — e.g. "
    "`2026-07-06-decorous-harbor-BPH-(3)-iven.chen`\n"
    "The `codename` (an auto-generated, unique adjective-noun pair, or your own "
    "override) is the lookup key; `user` is your Slack handle. "
    f"Categories: {', '.join('`' + c + '`' for c in EXPERIMENT_CATEGORIES)} "
    "(uppercased in the name). Experiments live in one of two Box directories — "
    "the *Scans* directory (data collection & scans) and the *Experiments* "
    "directory — and every command searches and labels both.\n\n"
    "*Creating experiments*\n"
    "• `/experiment new` — the guided flow: pick a category, pick a Box "
    "directory, then fill in the details dialog:\n"
    "    ◦ an optional *codename* (leave blank to auto-generate a unique one)\n"
    "    ◦ _Scans_: the *number of scans* (required)\n"
    "    ◦ _Experiments_: the *action* (required)\n"
    "    ◦ optional *associated experiments*, one per line — a codename, a full "
    "name, or a Box folder link. A link to a folder that isn't named to the "
    "scheme yet can be renamed on the spot.\n"
    "• `/experiment subexperiment [parent-link]` — create a *sub-experiment* "
    "under a parent: paste the parent's Box link, optionally set a codename, "
    "give an action + date, and choose where the folder goes — *nested inside "
    "the parent* (default) or top-level in Experiments/Scans. It always inherits "
    "the parent's *category*, but its *date* is independent (a sub-experiment "
    "can be run later). The parent↔child link is recorded both ways.\n\n"
    "*Finding experiments*\n"
    "• `/experiment track <name | codename>` — locate a folder and get its full "
    "path, a direct link, and any associated experiments. A codename normally "
    "maps to one experiment; if several share it, all matches are listed.\n"
    f"• `/experiment category <{'|'.join(EXPERIMENT_CATEGORIES)}>` — list every "
    "experiment in a category, across both directories.\n"
    f"• `/experiment scans [{'|'.join(EXPERIMENT_CATEGORIES)}]` — list the Scans "
    "directory, optionally filtered to a category.\n"
    f"• `/experiment experiments [{'|'.join(EXPERIMENT_CATEGORIES)}]` — same, for "
    "the Experiments directory.\n"
    "• `/experiment date <YYYY-MM-DD>` — list everything generated on a date.\n"
    "    _In any listing, an experiment that has associated experiment(s) is "
    "tagged with :link:._\n\n"
    "*Managing experiments*\n"
    "• `/experiment delete <name>` — delete an experiment's Box folder, but only "
    "if it's empty; a folder with files is linked for review instead. "
    "Deletions are announced to the channel.\n"
    "• `/experiment delete empty` — prune every empty experiment folder in both "
    "directories at once.\n"
    "• `/experiment legacy [box-link]` — rename an old, non-scheme folder to the "
    "naming scheme. Date and category are auto-detected from the old name when "
    "possible; you supply the action (Experiments) or scan count (Scans), and "
    "your Slack handle fills the `user` segment. If the folder holds subfolders, "
    "tick *Register sub-experiments* to link them (keeping their names, skipping "
    "any `calibration` folder); they stay out of the index unless you also tick "
    "*Index sub-experiments*.\n\n"
    "*The index (spreadsheet database)*\n"
    "• `/experiment database retrieve` — a link to `experiment_index.csv`, a "
    "spreadsheet mirror of every experiment (kept in a `.experiment-namer` "
    "folder in Box) you can open and read.\n"
    "• `/experiment database update` — reconcile that CSV against Box (add new "
    "folders, refresh renamed ones, drop deleted ones). The index also updates "
    "automatically as you create / delete / convert, so you rarely need this.\n\n"
    "_Tip: `/experiment` with no subcommand shows a short usage summary._"
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
    """Render [{'name', 'url', 'directory'[, 'associated_with']}] as a Slack
    mrkdwn bullet list, noting which Box directory each experiment lives in and
    flagging any that have associated experiment(s)."""
    lines = []
    for e in experiments:
        line = f"• <{e['url']}|{e['name']}> — _{e['directory']}_"
        if e.get("associated_with"):
            line += " · :link: has associated experiment(s)"
        if e.get("has_parent"):
            line += " · :deciduous_tree: sub-experiment"
        if e.get("has_children"):
            line += " · :seedling: has sub-experiment(s)"
        lines.append(line)
    return "\n".join(lines)


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
            {
                "type": "input",
                "block_id": "action",
                "optional": True,
                "label": {"type": "plain_text", "text": "Action"},
                "hint": {
                    "type": "plain_text",
                    "text": "Required if the folder is in the Experiments "
                    "directory (e.g. segmentation).",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "action_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. segmentation"},
                },
            },
            {
                "type": "input",
                "block_id": "scan_count",
                "optional": True,
                "label": {"type": "plain_text", "text": "Number of scans"},
                "hint": {
                    "type": "plain_text",
                    "text": "Required if the folder is in the Scans directory.",
                },
                "element": {
                    "type": "number_input",
                    "action_id": "count_input",
                    "is_decimal_allowed": False,
                    "min_value": "1",
                },
            },
            {
                "type": "input",
                "block_id": "register_children",
                "optional": True,
                "label": {"type": "plain_text", "text": "Sub-experiments inside"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "register",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Register the subfolders as sub-experiments "
                                "(skips any 'calibration' folder)",
                            },
                            "value": "yes",
                        }
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "index_children",
                "optional": True,
                "label": {"type": "plain_text", "text": "Index sub-experiments"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "index",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Also add those sub-experiments to the index "
                                "CSV (off by default)",
                            },
                            "value": "yes",
                        }
                    ],
                },
            },
        ],
    }


def _codename_block() -> dict:
    """Optional codename override; blank means auto-generate a unique one."""
    return {
        "type": "input",
        "block_id": "codename",
        "optional": True,
        "label": {"type": "plain_text", "text": "Codename"},
        "hint": {
            "type": "plain_text",
            "text": "Leave blank to auto-generate a unique adjective-noun "
            "codename (e.g. decorous-harbor).",
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "codename_input",
            "placeholder": {"type": "plain_text", "text": "e.g. decorous-harbor"},
        },
    }


def _required_detail_block(dir_key: str) -> dict:
    """The directory-specific REQUIRED input: number of scans (scans) or the
    action (experiments)."""
    if dir_key == "scans":
        return {
            "type": "input",
            "block_id": "scan_count",
            "label": {"type": "plain_text", "text": "Number of scans"},
            "hint": {
                "type": "plain_text",
                "text": "Required. Goes into the name as …-CATEGORY-(n)-user.",
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
        "block_id": "action",
        "label": {"type": "plain_text", "text": "Action"},
        "hint": {
            "type": "plain_text",
            "text": "Required. What you're doing; goes into the name as "
            "…-CATEGORY-action-user.",
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "action_input",
            "placeholder": {"type": "plain_text", "text": "e.g. segmentation"},
        },
    }


def details_modal_view(meta: dict, dir_key: str) -> dict:
    """The `new_experiment_details` modal for `/experiment new`: an optional
    codename override, the directory-specific required segment (action or scan
    count), and optional association(s).

    The association field is multiline — one entry per line — and accepts
    codenames, full experiment names, or Box folder links. If a link points at
    a folder that isn't named to the scheme, submitting pushes the
    `associate_legacy` modal to handle it (rename or keep)."""
    return {
        "type": "modal",
        "callback_id": "new_experiment_details",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "Experiment details"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _codename_block(),
            _required_detail_block(dir_key),
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
                    "text": "One per line. Each can be a codename, a full "
                    "experiment name, or a Box folder link.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "assoc_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. decorous-harbor\nhttps://…box.com/folder/123",
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
                    "BPH, CAO or FLR.",
                },
                "element": cat_element,
            }
        )
        blocks.append(
            {
                "type": "input",
                "block_id": f"action_{i}",
                "optional": True,
                "label": {"type": "plain_text", "text": "Action (Experiments only)"},
                "hint": {
                    "type": "plain_text",
                    "text": "Required to rename a folder in the Experiments "
                    "directory (e.g. segmentation).",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "action",
                    "placeholder": {"type": "plain_text", "text": "e.g. segmentation"},
                },
            }
        )
        blocks.append(
            {
                "type": "input",
                "block_id": f"scan_{i}",
                "optional": True,
                "label": {"type": "plain_text", "text": "# scans (Scans only)"},
                "hint": {
                    "type": "plain_text",
                    "text": "Required to rename a folder in the Scans directory.",
                },
                "element": {
                    "type": "number_input",
                    "action_id": "count",
                    "is_decimal_allowed": False,
                    "min_value": "1",
                },
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


def subexperiment_modal_view(meta: dict, prefill_link: str = "") -> dict:
    """The `subexperiment` modal for /experiment subexperiment: parent link +
    optional codename + optional date + required action + category + placement.

    The child always uses the action name format; `placement` decides where the
    folder physically lives (nested in the parent, or top-level in either
    directory). The category must match the parent's (validated on submit)."""
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
    placement_options = [
        {
            "text": {"type": "plain_text", "text": "Nested inside the parent's folder"},
            "value": "nested",
        },
        {
            "text": {"type": "plain_text", "text": "Top level of Experiments"},
            "value": "experiments",
        },
        {
            "text": {"type": "plain_text", "text": "Top level of Scans"},
            "value": "scans",
        },
    ]
    return {
        "type": "modal",
        "callback_id": "subexperiment",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "New sub-experiment"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "parent_link",
                "label": {"type": "plain_text", "text": "Parent experiment link"},
                "hint": {
                    "type": "plain_text",
                    "text": "Box folder link to the experiment this runs under.",
                },
                "element": link_element,
            },
            _codename_block(),
            {
                "type": "input",
                "block_id": "date",
                "optional": True,
                "label": {"type": "plain_text", "text": "Experiment date"},
                "hint": {
                    "type": "plain_text",
                    "text": "Can differ from the parent (e.g. a run done later). "
                    "Leave empty for today.",
                },
                "element": {"type": "datepicker", "action_id": "date_input"},
            },
            {
                "type": "input",
                "block_id": "action",
                "label": {"type": "plain_text", "text": "Action"},
                "hint": {
                    "type": "plain_text",
                    "text": "Required. Goes into the name as …-CATEGORY-action-user. "
                    "The category is inherited from the parent.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "action_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. segmentation"},
                },
            },
            {
                "type": "input",
                "block_id": "placement",
                "label": {"type": "plain_text", "text": "Where to create the folder"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "placement_input",
                    "initial_option": placement_options[0],
                    "options": placement_options,
                },
            },
            {
                "type": "input",
                "block_id": "index",
                "optional": True,
                "label": {"type": "plain_text", "text": "Index this sub-experiment"},
                "hint": {
                    "type": "plain_text",
                    "text": "Only affects a nested sub-experiment; top-level ones "
                    "are always indexed.",
                },
                "element": {
                    "type": "checkboxes",
                    "action_id": "index_toggle",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Add it to the index CSV",
                            },
                            "value": "yes",
                        }
                    ],
                },
            },
        ],
    }
