"""Curated allowlist of well-known activist investor filers.

Matched case-insensitively as a substring against the filer field returned
by EDGAR's Schedule 13D search. The "top tier" subset earns a severity
bump — these are the filers that, historically, move stocks the most on
an initial stake disclosure.
"""

from __future__ import annotations

# Any match here is considered an activist for the detector's purposes.
ACTIVIST_FILERS: tuple[str, ...] = (
    "Carl Icahn",
    "Icahn Capital",
    "Icahn Enterprises",
    "Elliott Management",
    "Elliott Investment",
    "Elliott Associates",
    "Starboard Value",
    "Third Point",
    "ValueAct Capital",
    "Pershing Square",
    "JANA Partners",
    "Trian Fund",
    "Trian Partners",
    "Ancora",
    "Engine No. 1",
    "Engaged Capital",
    "Macellum",
    "Land & Buildings",
    "Mantle Ridge",
    "Nelson Peltz",
)

# The heavy hitters: initial disclosures from these filers have reliably
# produced large single-day stock reactions in recent years.
TOP_TIER: tuple[str, ...] = (
    "Carl Icahn",
    "Icahn Capital",
    "Elliott Management",
    "Elliott Investment",
    "Starboard Value",
    "Pershing Square",
    "Trian Fund",
    "Trian Partners",
    "Nelson Peltz",
)


def match(filer: str | None) -> str | None:
    """Return the matched allowlist entry (lowercased) or None."""
    if not filer:
        return None
    lower = filer.lower()
    for name in ACTIVIST_FILERS:
        if name.lower() in lower:
            return name
    return None


def is_top_tier(filer: str | None) -> bool:
    if not filer:
        return False
    lower = filer.lower()
    return any(name.lower() in lower for name in TOP_TIER)
