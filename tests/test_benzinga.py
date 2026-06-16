"""Tests for the Benzinga news client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from switching.sources import benzinga

RAW = {
    "id": 1,
    "title": "Acme to acquire Beta for $2B",
    "teaser": "deal teaser",
    "body": "full body text",
    "created": "Mon, 16 Jun 2026 10:00:00 -0400",
    "url": "https://benzinga.com/news/1",
    "importance_rank": 3,
    "stocks": [{"name": "ACME", "exchange": "NASDAQ"}, {"name": "BETA"}],
    "channels": [{"name": "M&A"}, {"name": "News"}],
}


def _resp(payload):
    r = MagicMock()
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s
    r.__exit__ = MagicMock(return_value=False)
    return r


class TestNormalise:
    def test_maps_fields(self):
        n = benzinga._normalise(RAW)
        assert n["title"] == "Acme to acquire Beta for $2B"
        assert n["tickers"] == ["ACME", "BETA"]     # pre-tagged, no resolution
        assert n["channels"] == ["M&A", "News"]
        assert n["summary"] == "deal teaser"          # teaser preferred over body
        assert n["importance"] == 3

    def test_summary_falls_back_to_body(self):
        n = benzinga._normalise({"title": "t", "body": "b", "stocks": []})
        assert n["summary"] == "b"


class TestFetchNews:
    def test_noop_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            assert benzinga.is_configured() is False
            assert benzinga.fetch_news(tickers=["AAPL"]) == []

    def test_parses_response_and_builds_request(self):
        with patch.dict("os.environ", {"BENZINGA_API_KEY": "k"}), \
             patch("urllib.request.urlopen", return_value=_resp([RAW])) as mo:
            out = benzinga.fetch_news(tickers=["ACME"], display_output="abstract", page_size=5)
        assert len(out) == 1 and out[0]["tickers"] == ["ACME", "BETA"]
        url = mo.call_args[0][0].full_url
        assert "token=k" in url and "tickers=ACME" in url and "displayOutput=abstract" in url

    def test_channels_and_updated_since_params(self):
        with patch.dict("os.environ", {"BENZINGA_API_KEY": "k"}), \
             patch("urllib.request.urlopen", return_value=_resp([])) as mo:
            benzinga.fetch_news(channels=["WIIMs", "Press Releases"], updated_since=1781587294)
        url = mo.call_args[0][0].full_url
        assert "channels=WIIMs%2CPress+Releases" in url
        assert "updatedSince=1781587294" in url

    def test_empty_on_http_error(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with patch.dict("os.environ", {"BENZINGA_API_KEY": "k"}), \
             patch("urllib.request.urlopen", side_effect=err):
            assert benzinga.fetch_news(tickers=["X"]) == []
