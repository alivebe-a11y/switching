from datetime import date
from pathlib import Path

import pytest

from switching.sources import sec_edgar
from switching.sources.sec_edgar import EdgarAuthError, EdgarClient


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "edgar"


def _opener(responses: dict[str, bytes]):
    def fake(url, headers):
        assert "User-Agent" in headers and headers["User-Agent"]
        for key, payload in responses.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected URL {url}")

    return fake


def test_requires_user_agent(monkeypatch):
    monkeypatch.delenv("SWITCHING_EDGAR_UA", raising=False)
    with pytest.raises(EdgarAuthError):
        EdgarClient()


def test_uses_env_user_agent(monkeypatch):
    monkeypatch.setenv("SWITCHING_EDGAR_UA", "Test Agent test@example.com")
    client = EdgarClient(opener=_opener({}))
    assert client._ua == "Test Agent test@example.com"


def test_ticker_for_cik(monkeypatch):
    monkeypatch.setenv("SWITCHING_EDGAR_UA", "Test Agent test@example.com")
    payload = (FIXTURES / "ticker_map.json").read_bytes()
    client = EdgarClient(opener=_opener({"company_tickers.json": payload}))
    assert client.ticker_for_cik("320193") == "AAPL"
    assert client.ticker_for_cik("0000320193") == "AAPL"
    assert client.ticker_for_cik("999999999") is None


def test_search_filings_parses_hits(monkeypatch):
    monkeypatch.setenv("SWITCHING_EDGAR_UA", "Test Agent test@example.com")
    responses = {
        "company_tickers.json": (FIXTURES / "ticker_map.json").read_bytes(),
        "efts.sec.gov": (FIXTURES / "search_buyback.json").read_bytes(),
    }
    client = EdgarClient(opener=_opener(responses))
    filings = client.search_filings(
        forms=["8-K"], since=date(2023, 1, 1), query="repurchase program"
    )
    # Search fixture returns 2 hits; the loop will ask for a second page and
    # the opener returns the same payload, so pagination stops when hits<10.
    assert len(filings) >= 2
    assert filings[0].form == "8-K"
    assert filings[0].ticker == "AAPL"
    assert filings[0].filed == date(2023, 5, 4)
    assert filings[1].ticker == "MSFT"


def test_format_accession_roundtrip():
    assert sec_edgar._format_accession("000032019323000064") == "0000320193-23-000064"
    # Already dashed — returned as-is.
    assert sec_edgar._format_accession("0000320193-23-000064") == "0000320193-23-000064"
