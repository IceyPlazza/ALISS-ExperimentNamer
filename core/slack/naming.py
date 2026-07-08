"""Experiment-name generation, detection, and validation.

Pure helpers with no Slack or Box dependencies, so they're easy to unit test.

Names follow a directory-specific scheme (see CLAUDE.md):

    Experiments:  YYYY-MM-DD-<codename>-<CATEGORY>-<action>-<user>
    Scans:        YYYY-MM-DD-<codename>-<CATEGORY>-(<num>)-<user>

The category is uppercased in the name (BPH/CAO/FLR). `codename` is the
auto-generated adjective-noun pair (or a user override) and is the unique
lookup key. `action` is free text (Experiments), `num` is the scan count
(Scans), and `user` is the creator's Slack handle (e.g. iven.chen).

Legacy folders created before this app use a different, older scheme
(date-category-word-word with adverb-verb combos); the detection/extraction
helpers still handle them so lookups keep working.
"""

import re
from datetime import date

import namer

# Experiment categories offered by the /experiment picker. Uppercased when
# placed in a name; add a code here to add a category everywhere.
EXPERIMENT_CATEGORIES = ["bph", "cao", "flr"]

# Word categories that unique-namer draws from when building the codename
# (adjective-noun) pair. See namer.list_categories() for the options.
NAMER_CATEGORIES = ["general"]

# The uppercase category token as it appears inside a name.
_CAT_TOKEN = "|".join(c.upper() for c in EXPERIMENT_CATEGORIES)

# A full new-scheme name, anchored on the uppercase CATEGORY token:
#   YYYY-MM-DD-<codename>-<CATEGORY>-<action|(n)>-<user>
# `codename` is non-greedy so it stops at the first category token; `rest`
# captures the action/scan-count + user tail.
NEW_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<codename>.+?)-(?P<cat>"
    + _CAT_TOKEN
    + r")-(?P<rest>.+)$"
)

# Loose shape of a full experiment name — matches BOTH the new scheme and
# legacy names (lowercase category, adverb-verb combo, old -(n)/-codename
# suffixes). Used only to tell "this is a full name" from "this is a bare
# codename"; the leading date is what distinguishes it from a bare codename.
EXPERIMENT_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[A-Za-z0-9]+(-[A-Za-z0-9()._]+)+$"
)

# A bare codename typed on its own (track/associate) — no leading date. The
# unique lookup key. Permissive (letters/digits/._-), so both a single word and
# a word-word pair match; full names are excluded because callers check
# EXPERIMENT_NAME_RE first.
CODENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# A Box folder link, e.g. https://vanderbilt.app.box.com/folder/123456789
BOX_FOLDER_LINK_RE = re.compile(r"box\.com/folder/(\d+)")

# Legacy codename: the word-word combo right after the lowercase category in an
# old-scheme name (date-category-word-word[-suffix]).
_LEGACY_COMBO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+-(?P<combo>[a-z]+-[a-z]+)(?:-|$)"
)


def generate_codename(exclude=None, attempts: int = 20) -> str:
    """A fresh adjective-noun codename from unique-namer, e.g. tattered-flower.

    If `exclude` (a set/collection of already-taken codenames) is given, re-roll
    up to `attempts` times to avoid a collision. Returns the last roll even if
    it couldn't find a free one, so name generation never blocks (codenames are
    a soft-unique key; exhausting the vocabulary is negligibly unlikely)."""
    exclude = exclude or ()

    def roll():
        return namer.generate(
            category=NAMER_CATEGORIES, separator="-", style="lowercase"
        )

    combo = roll()
    for _ in range(attempts):
        if combo not in exclude:
            break
        combo = roll()
    return combo


def sanitize_segment(raw: str) -> str:
    """Normalize a free-text name segment (codename override, action, user
    handle): collapse whitespace to dashes and drop anything outside
    [A-Za-z0-9._-]. Returns "" for blank/None."""
    s = re.sub(r"\s+", "-", (raw or "").strip())
    return re.sub(r"[^A-Za-z0-9._-]", "", s)


def build_experiment_name(
    dir_key: str,
    category: str,
    codename: str,
    user: str,
    action: str | None = None,
    num_scans=None,
    on_date: str | None = None,
) -> str:
    """Assemble a full experiment name for the given directory.

    Experiments: YYYY-MM-DD-codename-CATEGORY-action-user
    Scans:       YYYY-MM-DD-codename-CATEGORY-(num)-user

    Category is uppercased; `user` is the Slack handle. Callers are responsible
    for supplying `action` (experiments) or `num_scans` (scans)."""
    date_str = on_date or date.today().isoformat()
    cat = category.upper()
    mid = f"({int(num_scans)})" if dir_key == "scans" else action
    return f"{date_str}-{codename}-{cat}-{mid}-{user}"


def generate_experiment_name(
    dir_key: str,
    category: str,
    user: str,
    action: str | None = None,
    num_scans=None,
    codename: str | None = None,
    exclude=None,
) -> str:
    """Build a full experiment name, auto-generating a unique codename when one
    isn't supplied. Thin convenience wrapper over build_experiment_name."""
    code = codename or generate_codename(exclude)
    return build_experiment_name(
        dir_key, category, code, user, action=action, num_scans=num_scans
    )


def detect_date(folder_name: str) -> str | None:
    """Pull a YYYY-MM-DD date out of a folder name, if present.

    Tolerates -, _ or . separators (e.g. "2026-06-17 - FLR: CT") and the
    compact 8-digit form (e.g. "20260623_PerceptionTuesday").
    Returns the normalized YYYY-MM-DD string, or None if nothing valid.
    """
    m = re.search(r"(\d{4})[-_.](\d{2})[-_.](\d{2})", folder_name)
    if not m:
        # compact YYYYMMDD, bounded so it doesn't match inside a longer number
        m = re.search(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", folder_name)
    if not m:
        return None
    normalized = "-".join(m.groups())
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized


def detect_category(folder_name: str) -> str | None:
    """Pull a category code (bph/cao/flr) out of a folder name (case-insensitive,
    so it matches both the uppercase token in new names and lowercase legacy
    names). Returns the lowercase code."""
    m = re.search(
        r"\b(" + "|".join(EXPERIMENT_CATEGORIES) + r")\b",
        folder_name,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else None


def extract_codename(folder_name: str) -> str | None:
    """Return the unique lookup token (codename) for a folder, or None.

    New scheme: the segment between the date and the CATEGORY token
    (YYYY-MM-DD-<codename>-CATEGORY-… → codename). Legacy scheme: the word-word
    combo after the lowercase category (date-category-word-word[-suffix]).
    Names that follow neither shape return None."""
    m = NEW_NAME_RE.match(folder_name)
    if m:
        return m.group("codename")
    m = _LEGACY_COMBO_RE.match(folder_name)
    return m.group("combo") if m else None


def extract_name_parts(folder_name: str) -> dict | None:
    """Break a NEW-scheme name into its parts, or None for legacy/other names.

    Returns {codename, category, action, num_scans, slack_user}. `action` is ""
    for scans names; `num_scans` is "" for experiments names. Used to derive the
    CSV index's per-segment columns when they aren't supplied explicitly."""
    m = NEW_NAME_RE.match(folder_name)
    if not m:
        return None
    rest = m.group("rest")
    head, sep, user = rest.rpartition("-")
    if not sep:  # no dash → the whole tail is the user, no action/scan count
        head, user = "", rest
    num_match = re.fullmatch(r"\((\d+)\)", head)
    return {
        "codename": m.group("codename"),
        "category": m.group("cat").lower(),
        "action": "" if num_match else head,
        "num_scans": num_match.group(1) if num_match else "",
        "slack_user": user,
    }
