from datetime import datetime, timezone
from pathlib import Path

from switching.signal import Signal
from switching.sources import historical


def _sig(ticker: str, detector: str, event_dt: datetime) -> Signal:
    return Signal(
        detector=detector,
        ticker=ticker,
        company=ticker,
        event_dt=event_dt,
        headline=f"{ticker} event",
        url="",
        evidence="",
        severity=0.7,
    )


def test_load_without_live_reads_seed(tmp_path: Path):
    csv = tmp_path / "fake.csv"
    csv.write_text(
        "event_dt,ticker,company,headline,url,evidence,severity\n"
        "2024-01-15,AAPL,Apple,Apple authorizes $90B buyback,,,0.9\n",
        encoding="utf-8",
    )
    sigs = historical.load("fake", root=tmp_path)
    assert len(sigs) == 1
    assert sigs[0].ticker == "AAPL"


def test_load_with_live_merges_and_dedups(tmp_path: Path, monkeypatch):
    csv = tmp_path / "ai_pivot.csv"
    csv.write_text(
        "event_dt,ticker,company,headline,url,evidence,severity\n"
        "2024-01-15,AAPL,Apple,Apple Announces AI Pivot,,,0.8\n",
        encoding="utf-8",
    )
    # Duplicate of the seed + one fresh live event.
    live_event = _sig("MSFT", "ai_pivot", datetime(2024, 2, 1, tzinfo=timezone.utc))
    seed_dup = _sig("AAPL", "ai_pivot", datetime(2024, 1, 15, tzinfo=timezone.utc))
    # dedup_key uses normalized headline; align so the dup is detected.
    seed_dup = Signal(
        detector="ai_pivot",
        ticker="AAPL",
        company="Apple",
        event_dt=datetime(2024, 1, 15, tzinfo=timezone.utc),
        headline="Apple Announces AI Pivot",
        url="",
        evidence="",
        severity=0.8,
    )

    sentinel_client = object()

    def fake_pull_live(client, since=None, until=None):
        assert client is sentinel_client
        return [seed_dup, live_event]

    import switching.detectors.ai_pivot as det_module

    monkeypatch.setattr(det_module, "pull_live", fake_pull_live, raising=False)

    sigs = historical.load("ai_pivot", root=tmp_path, live=sentinel_client)
    tickers = sorted(s.ticker for s in sigs)
    assert tickers == ["AAPL", "MSFT"], tickers


def test_load_handles_detector_without_pull_live(tmp_path: Path):
    # No CSV and no pull_live hook → empty list, no crash.
    sentinel_client = object()
    sigs = historical.load("does_not_exist", root=tmp_path, live=sentinel_client)
    assert sigs == []
