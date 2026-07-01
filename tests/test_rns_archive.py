"""Tests for the RNS stream archive (full Investegate feed capture)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from switching import rns_archive, storage


@pytest.fixture(autouse=True)
def _reset():
    yield
    rns_archive._reset()
    storage._reset_connection_cache()


def _item(title, url, when="2026-07-01T09:00:00+00:00"):
    return SimpleNamespace(
        title=title, url=url,
        published=datetime.fromisoformat(when),
    )


class TestCategory:
    def test_known_categories(self):
        assert rns_archive.rns_category("Director/PDMR Shareholding") == "director_dealing"
        assert rns_archive.rns_category("Transaction in Own Shares") == "own_shares"
        assert rns_archive.rns_category("Final Results") == "results"
        assert rns_archive.rns_category("Trading Update") == "trading_update"
        assert rns_archive.rns_category("Holding(s) in Company") == "holdings"
        assert rns_archive.rns_category("Placing and Subscription") == "capital_raise"
        assert rns_archive.rns_category("Some Random Announcement") == "other"

    def test_clean_splits_epic(self):
        h, e = rns_archive._clean("Final Results (SGE)")
        assert h == "Final Results"
        assert e == "SGE"


class TestRecord:
    def test_noop_until_configured(self):
        # not configured -> silent no-op, no crash
        rns_archive.record([_item("Final Results (ABC)", "u1")])

    def test_writes_and_dedups(self, tmp_path):
        rns_archive.configure(tmp_path / "uk_portfolio.json")
        items = [
            _item("Director/PDMR Shareholding (UTL)", "https://x/announcement/rns/utl/dd/1"),
            _item("Final Results (SGE)", "https://x/announcement/rns/sge/fr/2"),
        ]
        rns_archive.record(items)
        rns_archive.record(items)          # re-scrape → must NOT duplicate

        conn = storage.connect(storage.db_path_for(tmp_path / "uk_portfolio.json"))
        rows = conn.execute(
            "SELECT epic, category, headline FROM rns_archive ORDER BY epic").fetchall()
        assert len(rows) == 2                       # deduped by url
        cats = {r[0]: r[1] for r in rows}
        assert cats["UTL"] == "director_dealing"
        assert cats["SGE"] == "results"

    def test_category_summary(self, tmp_path):
        rns_archive.configure(tmp_path / "uk_portfolio.json")
        rns_archive.record([
            _item("Director/PDMR Shareholding (A)", "u/a"),
            _item("Director/PDMR Shareholding (B)", "u/b"),
            _item("Final Results (C)", "u/c"),
        ])
        summ = {d["category"]: d["n"] for d in rns_archive.category_summary(tmp_path / "uk_portfolio.json")}
        assert summ["director_dealing"] == 2
        assert summ["results"] == 1
