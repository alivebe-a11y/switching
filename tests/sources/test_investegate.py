"""Tests for the Investegate RNS scraper (primary UK source)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from switching.sources import investegate

_SAMPLE = """
<table><tbody>
<tr>
  <td>22 May 2026 06:31 PM</td>
  <td><a href="https://www.investegate.co.uk/source/RNS">RNS</a></td>
  <td><a href="https://www.investegate.co.uk/company/GLV"><img/></a></td>
  <td><a href="https://www.investegate.co.uk/company/GLV">Glenveagh Properties (CDI) (GLV)</a></td>
  <td><a href="https://www.investegate.co.uk/announcement/rns/glenveagh-properties-cdi---glv/transaction-in-own-shares/9583338">Transaction in Own Shares</a></td>
</tr>
<tr>
  <td>22 May 2026 06:15 PM</td>
  <td><a href="https://www.investegate.co.uk/source/RNS">RNS</a></td>
  <td><a href="https://www.investegate.co.uk/company/SGE">Sage Group (SGE)</a></td>
  <td><a href="https://www.investegate.co.uk/announcement/rns/sage-group---sge/trading-statement/9583300">Trading Statement ahead of expectations</a></td>
</tr>
</tbody></table>
"""


def setup_function(_):
    investegate._reset_cache()


def teardown_function(_):
    investegate._reset_cache()


class TestParse:
    def test_extracts_items_with_epic(self):
        items = investegate.parse(_SAMPLE)
        assert len(items) == 2
        assert items[0].title == "Transaction in Own Shares (GLV)"
        assert items[0].extract_ticker() == "GLV.L"
        assert items[0].market == "uk"
        assert items[1].extract_ticker() == "SGE.L"

    def test_timestamp_parsed(self):
        items = investegate.parse(_SAMPLE)
        assert items[0].published.year == 2026
        assert items[0].published.hour == 18  # 06:31 PM -> 18:31

    def test_url_captured(self):
        items = investegate.parse(_SAMPLE)
        assert items[0].url.endswith("/9583338")

    def test_garbage_html_yields_nothing(self):
        assert investegate.parse("<html><body>no announcements</body></html>") == []

    def test_rows_without_epic_skipped(self):
        # announcement URL without the ---<epic>/ slug -> can't ticker -> skip
        html = '<tr><td>22 May 2026 06:31 PM</td><td><a href="https://www.investegate.co.uk/announcement/rns/something/plain/123">Notice</a></td></tr>'
        assert investegate.parse(html) == []

    def test_duplicate_urls_collapsed(self):
        items = investegate.parse(_SAMPLE + _SAMPLE)  # same rows twice
        assert len(items) == 2


class TestScrape:
    def setup_method(self, _):
        investegate._reset_cache()

    def teardown_method(self, _):
        investegate._reset_cache()

    def _resp(self, text, status=200):
        r = MagicMock()
        r.status_code = status
        r.text = text
        r.raise_for_status = MagicMock()
        return r

    def test_scrape_fetches_and_parses(self):
        with patch("switching.sources.investegate.requests.get", return_value=self._resp(_SAMPLE)) as g:
            items = investegate.scrape()
        assert len(items) == 2
        g.assert_called_once()

    def test_scrape_is_cached(self):
        with patch("switching.sources.investegate.requests.get", return_value=self._resp(_SAMPLE)) as g:
            investegate.scrape()
            investegate.scrape()   # within TTL -> no second HTTP call
        assert g.call_count == 1

    def test_scrape_raises_on_http_error(self):
        import requests
        r = self._resp("", status=503)
        r.raise_for_status.side_effect = requests.HTTPError("503")
        with patch("switching.sources.investegate.requests.get", return_value=r):
            try:
                investegate.scrape(force=True)
                assert False, "expected HTTPError"
            except requests.HTTPError:
                pass

    def test_scrape_zero_items_on_empty_page(self):
        with patch("switching.sources.investegate.requests.get", return_value=self._resp("<html></html>")):
            assert investegate.scrape(force=True) == []
