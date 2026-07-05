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

# A Box folder link, e.g. https://vanderbilt.app.box.com/folder/123456789
BOX_FOLDER_LINK_RE = re.compile(r"box\.com/folder/(\d+)")


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


def detect_category(folder_name: str) -> str | None:
    """Pull a category code (bph/cao) out of a legacy folder name."""
    m = re.search(
        r"\b(" + "|".join(EXPERIMENT_CATEGORIES) + r")\b",
        folder_name,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else None
