"""Refresh the vendored Trading 212 API skill manifest.

Pulls the latest SKILL.md from the official trading212-labs/agent-skills
repo and writes it to docs/vendor/t212-api.md with a header noting the
source URL and the date pulled. Diff against git to spot upstream changes.

Why vendor instead of fetching live: CLAUDE.md keeps a summary of the
critical facts inline (rate limits, error codes, ticker format) so they
load into context every session. The full manifest in docs/vendor/ is the
backstop — available offline, in CI, in the container build — and lets
us diff for upstream changes deliberately rather than silently following
moving target.

Usage:
    python scripts/refresh_t212_docs.py            # pull + write
    python scripts/refresh_t212_docs.py --check    # exit 1 if file would change

Run when:
- You hit an undocumented T212 error and want to check if the doc covers it.
- Quarterly, as a sanity pass.
- After T212 mentions an API change.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Use OS trust store so corporate/home SSL inspection doesn't block the fetch.
# (Same trick edgar_8k_sample.py uses.)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # production environments with proper CA chain don't need it

import requests

_SOURCE_URL = (
    "https://raw.githubusercontent.com/trading212-labs/agent-skills/"
    "master/plugins/trading212-api/skills/trading212-api/SKILL.md"
)
_REPO_URL = "https://github.com/trading212-labs/agent-skills"
_VENDOR_PATH = Path(__file__).resolve().parent.parent / "docs" / "vendor" / "t212-api.md"

_HEADER_TEMPLATE = """<!--
VENDORED COPY — do NOT edit by hand.
Source:   {repo}
File:     plugins/trading212-api/skills/trading212-api/SKILL.md
Pulled:   {pulled}
Refresh:  python scripts/refresh_t212_docs.py

This is Trading 212's official skill manifest (Apache-2.0 licensed by the
trading212-labs org). We vendor it so the full API reference is always
available offline / in CI / in the container build, and so upstream changes
show up as a deliberate diff rather than silently moving under us.

The high-value bits (rate limits, error codes, ticker format) are also
summarised in CLAUDE.md under "Trading 212 API reference" — those load
into context automatically every session.
-->

"""


def _fetch() -> str:
    resp = requests.get(_SOURCE_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def _wrap(body: str) -> str:
    header = _HEADER_TEMPLATE.format(
        repo=_REPO_URL,
        pulled=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    return header + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Exit 1 if the vendored file would change (for CI).",
    )
    args = parser.parse_args()

    try:
        body = _fetch()
    except Exception as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 2

    new_content = _wrap(body)

    if args.check:
        if not _VENDOR_PATH.exists():
            print(f"Missing: {_VENDOR_PATH}", file=sys.stderr)
            return 1
        # Strip the "Pulled:" line from both sides — only material changes count
        def _normalise(s: str) -> str:
            return "\n".join(
                line for line in s.splitlines()
                if not line.startswith("Pulled:")
            )
        current = _VENDOR_PATH.read_text(encoding="utf-8")
        if _normalise(current) != _normalise(new_content):
            print(
                f"Upstream T212 manifest has changed. Re-run "
                f"`python scripts/refresh_t212_docs.py` and review the diff.",
                file=sys.stderr,
            )
            return 1
        print("Vendored T212 manifest is up to date.")
        return 0

    _VENDOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _VENDOR_PATH.write_text(new_content, encoding="utf-8", newline="\n")
    body_lines = body.count("\n") + 1
    print(f"Wrote {_VENDOR_PATH} ({body_lines} lines from upstream).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
