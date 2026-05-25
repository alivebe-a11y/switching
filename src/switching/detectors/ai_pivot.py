from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from switching.detectors.base import Detector
from switching.registry import register
from switching import detection_funnel
from switching.signal import Signal
from switching.sources import rss

log = logging.getLogger(__name__)

# Words that flag an AI-related release. \bAI\b catches "AI" but avoids "PAIR".
_AI_TERMS = re.compile(
    r"(?i)(\bAI\b|artificial[- ]intelligence|generative[- ]ai|\bLLM\b|\bllms?\b"
    r"|agentic|machine[- ]learning|deep[- ]learning|neural[- ]network"
    r"|foundation[- ]model|chatgpt|gpt-\d)"
)

# Pivot verbs — something more than a routine product mention.
_PIVOT_TERMS = re.compile(
    r"(?i)(pivot|relaunch|rebrand|unveil|introduc|launch|announc|transform|"
    r"reposition|shift[s]?\s+(?:focus|strategy)|enter[s]?\s+the\s+ai|"
    r"new\s+(?:strategy|direction|chapter)|ai-?first|ai-?native|ai-?powered"
    r"|ai-?driven)"
)

# Strong pivot verbs required when company name already contains AI.
# Generic verbs like "announces/launches" are too noisy for AI-native companies.
_STRONG_PIVOT_TERMS = re.compile(
    r"(?i)(pivot|relaunch|rebrand|transform|reposition|"
    r"shift[s]?\s+(?:focus|strategy)|enter[s]?\s+the\s+ai|"
    r"new\s+(?:strategy|direction|chapter)|ai-?first|ai-?native)"
)

# Amplifiers — boost severity.
_GUIDANCE_TERMS = re.compile(
    r"(?i)(raises?\s+guidance|updates?\s+outlook|restructur|layoff|cost\s+cut)"
)


@register
class AIPivotDetector(Detector):
    name = "ai_pivot"
    description = (
        "Public companies pivoting, rebranding, or launching around AI. Combines "
        "AI vocabulary with pivot verbs to filter routine AI product mentions."
    )

    def __init__(self, feeds: tuple[str, ...] | None = None) -> None:
        self._feeds = feeds

    def scan(self, since: datetime) -> Iterable[Signal]:
        items = rss.fetch(self._feeds or rss.DEFAULT_FEEDS, since=since)
        classified = 0
        with_ticker = 0
        for item in items:
            match = classify(item.title, item.summary)
            if match is None:
                continue
            classified += 1
            ticker = item.extract_ticker()
            if not ticker:
                detection_funnel.record_drop(self.name, item)
                continue
            with_ticker += 1
            yield Signal(
                detector=self.name,
                ticker=ticker,
                company=_company_from_headline(item.title),
                event_dt=item.published,
                headline=item.title,
                url=item.url,
                evidence=match["evidence"],
                severity=match["severity"],
            )
        log.info(
            "%s: %d items, %d classified, %d with ticker",
            self.name, len(items), classified, with_ticker,
        )


def classify(title: str, summary: str = "") -> dict | None:
    """Return match metadata if the text looks like an AI-pivot, else None.

    Strict rule: both an AI term and a pivot verb must appear in the headline.
    Press releases that bury an AI mention in the body are too noisy to
    surface at v1; we can loosen this once an LLM classifier replaces the
    regex.
    """
    text = f"{title}\n{summary}"
    ai_in_title = _AI_TERMS.search(title)
    pivot_in_title = _PIVOT_TERMS.search(title)
    if not (ai_in_title and pivot_in_title):
        return None

    # If "AI" appears in the company name (first 45 chars before the pivot verb),
    # the company is likely AI-native — require a strong pivot verb, not just
    # generic "announces/launches" which every AI company does constantly.
    company_part = title[:45]
    if _AI_TERMS.search(company_part) and not _STRONG_PIVOT_TERMS.search(title):
        return None
    severity = 0.70  # base: both cues present in title
    if _GUIDANCE_TERMS.search(text):
        severity += 0.15
    if _AI_TERMS.search(summary):
        severity += 0.10
    severity = min(severity, 0.95)
    return {
        "evidence": _evidence_snippet(text, ai_in_title, pivot_in_title),
        "severity": round(severity, 3),
    }


def _evidence_snippet(text: str, *matches: re.Match) -> str:
    spans = sorted(m.span() for m in matches if m)
    if not spans:
        return text[:160].strip()
    start = max(0, spans[0][0] - 40)
    end = min(len(text), spans[-1][1] + 60)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _company_from_headline(title: str) -> str:
    # Press releases usually lead with "CompanyName Announces..."; we don't
    # need a perfect parse — the ticker is the source of truth downstream.
    return re.split(r"\s+(?:Announces|Launches|Unveils|Introduces|Pivots|Reports)\b", title, maxsplit=1)[0].strip()
