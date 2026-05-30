"""Tests for the weekly performance report generator."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from switching.weekly_report import (
    _analyse_trades,
    _detector_rankings,
    _exit_reason_breakdown,
    _generate_suggestions,
    _severity_analysis,
    _skipped_opportunity,
    _t212_vs_paper,
    generate_report,
    load_all_reports,
    save_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _trade(
    ticker="AAPL",
    detector="analyst_upgrade",
    pnl=50.0,
    pct_return=0.05,
    exit_reason="first_green",
    entry_dt="2026-05-19T10:00:00+00:00",
    exit_dt="2026-05-20T15:00:00+00:00",
    entry_price=100.0,
    severity=0.75,
):
    return dict(
        ticker=ticker,
        detector=detector,
        pnl=pnl,
        pct_return=pct_return,
        exit_reason=exit_reason,
        entry_dt=entry_dt,
        exit_dt=exit_dt,
        entry_price=entry_price,
        severity=severity,
    )


def _paper_portfolio(tmp_path: Path, trades: list[dict]) -> Path:
    data = {
        "cash": 19000.0,
        "positions": [],
        "trades": trades,
        "seen_signals": [],
        "last_signals": [],
        "last_scan_dt": "",
        "max_position_pct": 0.01,
        "max_positions": 0,
        "cached_prices": {},
        "last_review_sent_dt": "",
        "last_weekly_report_dt": "",
    }
    p = tmp_path / "paper_portfolio.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Unit tests — helper functions
# ---------------------------------------------------------------------------


class TestAnalyseTrades:
    def test_empty_trades(self):
        result = _analyse_trades([])
        assert result["count"] == 0
        assert result["win_rate"] == 0.0

    def test_all_winners(self):
        trades = [_trade(pnl=10.0, pct_return=0.05) for _ in range(4)]
        result = _analyse_trades(trades)
        assert result["count"] == 4
        assert result["wins"] == 4
        assert result["win_rate"] == 1.0

    def test_mixed_results(self):
        trades = [
            _trade(pnl=20.0, pct_return=0.02),
            _trade(pnl=-10.0, pct_return=-0.01),
            _trade(pnl=15.0, pct_return=0.015),
        ]
        result = _analyse_trades(trades)
        assert result["count"] == 3
        assert result["wins"] == 2
        assert abs(result["win_rate"] - 2/3) < 0.001

    def test_total_pnl_summed(self):
        trades = [_trade(pnl=30.0), _trade(pnl=-10.0), _trade(pnl=20.0)]
        result = _analyse_trades(trades)
        assert abs(result["total_pnl"] - 40.0) < 0.01


class TestDetectorRankings:
    def test_sorted_by_win_rate_desc(self):
        trades = [
            _trade(detector="earnings_surprise", pnl=100.0, pct_return=0.10),
            _trade(detector="earnings_surprise", pnl=50.0, pct_return=0.05),
            _trade(detector="buyback", pnl=-10.0, pct_return=-0.01),
            _trade(detector="buyback", pnl=-20.0, pct_return=-0.02),
        ]
        rows = _detector_rankings(trades)
        assert rows[0]["detector"] == "earnings_surprise"
        assert rows[-1]["detector"] == "buyback"

    def test_includes_all_detectors(self):
        trades = [
            _trade(detector="a", pnl=10.0),
            _trade(detector="b", pnl=5.0),
            _trade(detector="c", pnl=-5.0),
        ]
        rows = _detector_rankings(trades)
        detectors = [r["detector"] for r in rows]
        assert set(detectors) == {"a", "b", "c"}


class TestExitReasonBreakdown:
    def test_groups_by_reason(self):
        trades = [
            _trade(exit_reason="first_green", pnl=10.0),
            _trade(exit_reason="first_green", pnl=15.0),
            _trade(exit_reason="stop_loss", pnl=-8.0),
            _trade(exit_reason="hold_expiry", pnl=2.0),
        ]
        bd = _exit_reason_breakdown(trades)
        assert "first_green" in bd
        assert "stop_loss" in bd
        assert "hold_expiry" in bd
        assert bd["first_green"]["count"] == 2
        assert bd["stop_loss"]["count"] == 1


class TestSeverityAnalysis:
    def test_splits_on_0_75_threshold(self):
        trades = [
            _trade(severity=0.80, pnl=10.0),
            _trade(severity=0.90, pnl=20.0),
            _trade(severity=0.60, pnl=-5.0),
            _trade(severity=0.50, pnl=-3.0),
        ]
        result = _severity_analysis(trades)
        assert result["high_severity"]["count"] == 2
        assert result["low_severity"]["count"] == 2
        assert result["high_severity"]["wins"] == 2

    def test_empty_trades(self):
        result = _severity_analysis([])
        assert result["high_severity"]["count"] == 0
        assert result["low_severity"]["count"] == 0


class TestT212VsPaper:
    def test_matches_on_ticker_and_date(self):
        t212 = [_trade(ticker="NVDA", pct_return=0.04, entry_dt="2026-05-19T10:00:00+00:00")]
        paper = [_trade(ticker="NVDA", pct_return=0.05, entry_dt="2026-05-19T11:00:00+00:00")]
        matched = _t212_vs_paper(t212, paper)
        assert len(matched) == 1
        assert abs(matched[0]["slippage"] - (-0.01)) < 0.0001

    def test_no_match_different_ticker(self):
        t212 = [_trade(ticker="NVDA", entry_dt="2026-05-19T10:00:00+00:00")]
        paper = [_trade(ticker="AAPL", entry_dt="2026-05-19T10:00:00+00:00")]
        matched = _t212_vs_paper(t212, paper)
        assert len(matched) == 0

    def test_no_match_different_date(self):
        t212 = [_trade(ticker="NVDA", entry_dt="2026-05-19T10:00:00+00:00")]
        paper = [_trade(ticker="NVDA", entry_dt="2026-05-20T10:00:00+00:00")]
        matched = _t212_vs_paper(t212, paper)
        assert len(matched) == 0


class TestSkippedOpportunity:
    def test_empty(self):
        result = _skipped_opportunity([])
        assert result["count"] == 0
        assert result["would_have_pnl"] == 0.0

    def test_counts_only_completed(self):
        signals = [
            {"tracking_complete": True, "simulated_pct_return": 0.05},
            {"tracking_complete": True, "simulated_pct_return": -0.02},
            {"tracking_complete": False, "simulated_pct_return": None},
        ]
        result = _skipped_opportunity(signals)
        assert result["count"] == 2
        assert result["would_have_won"] == 1

    def test_would_have_pnl_positive(self):
        signals = [
            {"tracking_complete": True, "simulated_pct_return": 0.10},
            {"tracking_complete": True, "simulated_pct_return": 0.05},
        ]
        result = _skipped_opportunity(signals)
        assert result["would_have_pnl"] > 0


class TestGenerateSuggestions:
    def _make_inputs(self, **overrides):
        defaults = dict(
            overall={"count": 0, "win_rate": 0.6, "wins": 0, "total_pnl": 0},
            detector_rows=[],
            exit_breakdown={},
            severity_data={
                "high_severity": {"count": 0, "win_rate": 0, "avg_return": 0},
                "low_severity": {"count": 0, "win_rate": 0, "avg_return": 0},
            },
            t212_matched=[],
            t212_stats={"count": 0, "total_pnl": 0},
            paper_stats={"count": 0, "total_pnl": 0},
            skipped_opps={"count": 0, "would_have_pnl": 0},
        )
        defaults.update(overrides)
        return defaults

    def test_poor_detector_flagged(self):
        det_row = {
            "detector": "buyback",
            "count": 10,
            "win_rate": 0.30,
            "total_pnl": -50.0,
            "avg_return": -0.01,
        }
        inputs = self._make_inputs(
            detector_rows=[det_row],
            overall={"count": 10, "win_rate": 0.30, "wins": 3, "total_pnl": -50},
        )
        suggestions = _generate_suggestions(**inputs)
        text = " ".join(suggestions)
        assert "buyback" in text
        assert any("disable" in s or "threshold" in s for s in suggestions)

    def test_strong_detector_praised(self):
        det_row = {
            "detector": "mna_target",
            "count": 8,
            "win_rate": 0.75,
            "total_pnl": 400.0,
            "avg_return": 0.08,
        }
        inputs = self._make_inputs(detector_rows=[det_row])
        suggestions = _generate_suggestions(**inputs)
        text = " ".join(suggestions)
        assert "mna_target" in text
        assert any("allocation" in s or "strong" in s for s in suggestions)

    def test_ok_state_gives_no_changes_message(self):
        inputs = self._make_inputs()
        suggestions = _generate_suggestions(**inputs)
        assert len(suggestions) == 1
        assert "no changes" in suggestions[0].lower() or "normal" in suggestions[0].lower()

    def test_stop_loss_rate_concern(self):
        inputs = self._make_inputs(
            overall={"count": 20, "win_rate": 0.4, "wins": 8, "total_pnl": -30},
            exit_breakdown={
                "stop_loss": {"count": 10, "win_rate": 0, "total_pnl": -100, "avg_return": -0.026},
                "first_green": {"count": 10, "win_rate": 1, "total_pnl": 70, "avg_return": 0.02},
            },
        )
        suggestions = _generate_suggestions(**inputs)
        assert any("stop" in s.lower() for s in suggestions)


# ---------------------------------------------------------------------------
# Integration test — generate_report against real-ish tmp dir
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_returns_tuple_of_messages_and_data(self, tmp_path: Path):
        _paper_portfolio(tmp_path, [])
        messages, data = generate_report(tmp_path)
        assert isinstance(messages, list)
        assert all(isinstance(m, str) for m in messages)
        assert isinstance(data, dict)

    def test_data_has_required_keys(self, tmp_path: Path):
        _paper_portfolio(tmp_path, [])
        _, data = generate_report(tmp_path)
        for key in ("generated_at", "week_start", "week_label", "paper",
                    "detector_rankings", "suggestions", "messages"):
            assert key in data, f"missing key: {key}"

    def test_at_least_one_message(self, tmp_path: Path):
        _paper_portfolio(tmp_path, [])
        messages, _ = generate_report(tmp_path)
        assert len(messages) >= 1

    def test_message_contains_header(self, tmp_path: Path):
        _paper_portfolio(tmp_path, [])
        messages, _ = generate_report(tmp_path)
        combined = "\n".join(messages)
        assert "Weekly Report" in combined

    def test_no_message_exceeds_telegram_limit(self, tmp_path: Path):
        """Each message must be ≤4096 chars for Telegram."""
        trades = [
            _trade(ticker=f"T{i}", detector="analyst_upgrade", pnl=float(i * 10))
            for i in range(30)
        ]
        _paper_portfolio(tmp_path, trades)
        messages, _ = generate_report(tmp_path)
        for msg in messages:
            assert len(msg) <= 4096

    def test_with_trades_shows_detector(self, tmp_path: Path):
        trades = [
            _trade(detector="earnings_surprise", pnl=100.0, pct_return=0.08),
            _trade(detector="earnings_surprise", pnl=50.0, pct_return=0.04),
            _trade(detector="buyback", pnl=-10.0, pct_return=-0.01),
        ]
        _paper_portfolio(tmp_path, trades)
        messages, _ = generate_report(tmp_path)
        combined = "\n".join(messages)
        assert "earnings_surprise" in combined
        assert "buyback" in combined

    def test_handles_missing_t212_file(self, tmp_path: Path):
        """Should not crash if t212_portfolio.json doesn't exist."""
        _paper_portfolio(tmp_path, [_trade(pnl=10.0)])
        messages, _ = generate_report(tmp_path)
        assert len(messages) >= 1

    def test_handles_all_missing_files(self, tmp_path: Path):
        """Empty directory should produce a valid (empty-data) report."""
        messages, _ = generate_report(tmp_path)
        assert len(messages) >= 1
        assert "Weekly Report" in "\n".join(messages)

    def test_data_messages_match_return(self, tmp_path: Path):
        """The 'messages' key in data must equal the returned message list."""
        _paper_portfolio(tmp_path, [])
        messages, data = generate_report(tmp_path)
        assert data["messages"] == messages


class TestArchive:
    def test_save_and_load(self, tmp_path: Path):
        data = {
            "generated_at": "2026-05-24T09:00:00+00:00",
            "week_start": "2026-05-24",
            "week_label": "24 May 2026",
            "messages": ["hello"],
        }
        path = save_report(tmp_path, data)
        assert path.exists()
        assert path.name == "2026-05-24.json"

    def test_load_all_returns_newest_first(self, tmp_path: Path):
        for date in ("2026-05-10", "2026-05-17", "2026-05-24"):
            save_report(tmp_path, {"week_start": date, "week_label": date, "messages": []})
        reports = load_all_reports(tmp_path)
        assert len(reports) == 3
        assert reports[0]["week_start"] == "2026-05-24"
        assert reports[-1]["week_start"] == "2026-05-10"

    def test_load_empty_dir(self, tmp_path: Path):
        assert load_all_reports(tmp_path) == []

    def test_overwrite_same_week(self, tmp_path: Path):
        """Running report twice on same Saturday should overwrite, not duplicate."""
        save_report(tmp_path, {"week_start": "2026-05-24", "messages": ["v1"]})
        save_report(tmp_path, {"week_start": "2026-05-24", "messages": ["v2"]})
        reports = load_all_reports(tmp_path)
        assert len(reports) == 1
        assert reports[0]["messages"] == ["v2"]

    def test_generate_and_send_saves_archive(self, tmp_path: Path):
        """generate_and_send always saves to disk even when Telegram fails."""
        from unittest.mock import patch
        _paper_portfolio(tmp_path, [_trade(pnl=20.0)])
        with patch("switching.notifications._send", return_value=False):
            from switching.weekly_report import generate_and_send
            generate_and_send(tmp_path)
        saved = list((tmp_path / "weekly_reports").glob("*.json"))
        assert len(saved) == 1

    def test_messages_are_html_safe(self, tmp_path: Path):
        """Every '&' in a report message must be an escaped HTML entity. A bare
        '&' (e.g. the literal "P&L") makes Telegram's HTML parser reject the
        whole message with HTTP 400 — the bug that stopped the weekly report
        sending and made it re-fire every scan cycle."""
        import re
        from switching.weekly_report import generate_report
        _paper_portfolio(tmp_path, [_trade(pnl=20.0), _trade(pnl=-5.0)])
        messages, _ = generate_report(tmp_path)
        assert messages
        bad_amp = re.compile(r"&(?!amp;|lt;|gt;|#\d+;|#x[0-9a-fA-F]+;)")
        for m in messages:
            assert not bad_amp.search(m), f"unescaped '&' in message: {m!r}"
