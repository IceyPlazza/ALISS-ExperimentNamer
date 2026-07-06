"""Experiment-name generation, detection, and validation.

Pure helpers with no Slack or Box dependencies, so they're easy to unit
test. Names look like `YYYY-MM-DD-{category}-word-word` — see CLAUDE.md for
the full scheme (categories, suffixes, and the legacy adverb-verb names).
"""

import re
from datetime import date

import namer

# Experiment categories offered by the /experiment picker. The code goes
# directly into the generated name: YYYY-MM-DD-{code}-word-word.
EXPERIMENT_CATEGORIES = ["bph", "cao"]

# Word categories that unique-namer draws from when building the word-word
# combo. See namer.list_categories() for the 25 available options.
NAMER_CATEGORIES = ["general"]

# Loose shape of a full experiment name (matches new unique-namer names,
# legacy adverb-verb names, and optional suffixes): 2025-10-08-bph-coolly-cut,
# 2026-07-05-bph-sunny-harbor-(3), 2026-07-05-cao-mad-polyphony-modelA
EXPERIMENT_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+-[a-z]+(-[A-Za-z0-9()._]+)+$"
)

# A bare word-word combo with no date/category prefix (e.g. coolly-cut).
# Combos are unique across experiments, so track/delete accept them alone.
WORD_COMBO_RE = re.compile(r"^[a-z]+(-[a-z]+)+$")

# A codename as typed in the "Add details…" dialog: the model label appended
# to an Experiments-directory name (…-word-word-codename), sanitized to
# [A-Za-z0-9._-] at creation. `track` accepts the same shape so codenames are
# lookup-able. Unlike combos, codenames are NOT unique — several experiments
# may share one.
CODENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# A Box folder link, e.g. https://vanderbilt.app.box.com/folder/123456789
BOX_FOLDER_LINK_RE = re.compile(r"box\.com/folder/(\d+)")

# The base scheme through the two-word combo, capturing whatever suffix
# follows it: YYYY-MM-DD-category-word-word-<suffix>.
_NAME_SUFFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+-[a-z]+-[a-z]+-(?P<suffix>.+)$"
)


def generate_word_combo() -> str:
    """A fresh word-word combo from unique-namer, e.g. tattered-flower."""
    return namer.generate(category=NAMER_CATEGORIES, separator="-", style="lowercase")


def generate_experiment_name(category: str) -> str:
    """Build a name like 2026-07-04-bph-tattered-flower."""
    return f"{date.today().isoformat()}-{category}-{generate_word_combo()}"


def detect_date(folder_name: str) -> str | None:
    """Pull a YYYY-MM-DD date out of a legacy folder name, if present.

    Tolerates -, _ or . separators (e.g. "2026-06-17 - FLR: CT").
    Returns the normalized YYYY-MM-DD string, or None if nothing valid.
    """
    m = re.search(r"(\d{4})[-_.](\d{2})[-_.](\d{2})", folder_name)
    if not m:
        return None
    normalized = "-".join(m.groups())
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized


def extract_codename(folder_name: str) -> str | None:
    """Return the codename appended to an experiment folder name, or None.

    Experiments-directory names may carry a model label after the word-word
    combo: YYYY-MM-DD-category-word-word-codename (e.g.
    2026-07-05-cao-mad-polyphony-modelA -> "modelA"). Names with no suffix,
    and scan-count suffixes ("…-(3)", scans directory), return None — only
    real codenames are trackable. Case is preserved (codenames are matched
    case-sensitively).
    """
    m = _NAME_SUFFIX_RE.match(folder_name)
    if not m:
        return None
    suffix = m.group("suffix")
    if re.fullmatch(r"\(\d+\)", suffix):  # scan count, not a codename
        return None
    return suffix


def detect_category(folder_name: str) -> str | None:
    """Pull a category code (bph/cao) out of a legacy folder name."""
    m = re.search(
        r"\b(" + "|".join(EXPERIMENT_CATEGORIES) + r")\b",
        folder_name,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else None
