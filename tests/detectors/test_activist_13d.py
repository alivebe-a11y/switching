from datetime import date

from switching.detectors._activist_filers import is_top_tier, match
from switching.detectors.activist_13d import filing_to_signal
from switching.sources.sec_edgar import Filing


def _filing(**overrides) -> Filing:
    base = dict(
        accession="0000000000-00-000000",
        cik="1318605",
        company="Target Co.",
        form="SC 13D",
        filed=date(2024, 3, 15),
        ticker="TGT",
        filer="Elliott Investment Management L.P.",
        reported_pct=7.5,
        url="https://example.com",
    )
    base.update(overrides)
    return Filing(**base)


def test_filer_allowlist_matches_case_insensitive():
    assert match("Elliott Investment Management L.P.") is not None
    assert match("ICAHN CAPITAL LP") is not None
    assert match("Vanguard Group") is None
    assert match(None) is None


def test_top_tier_bump():
    assert is_top_tier("Elliott Investment Management") is True
    assert is_top_tier("Ancora Holdings") is False


def test_filing_without_allowlist_match_returns_none():
    assert filing_to_signal(_filing(filer="Random Family Office")) is None


def test_filing_without_ticker_returns_none():
    assert filing_to_signal(_filing(ticker=None)) is None


def test_fresh_13d_scores_higher_than_amendment():
    fresh = filing_to_signal(_filing(form="SC 13D"))
    amend = filing_to_signal(_filing(form="SC 13D/A"))
    assert fresh is not None and amend is not None
    assert fresh.severity > amend.severity


def test_top_tier_scores_higher_than_second_tier():
    elliott = filing_to_signal(_filing(filer="Elliott Investment Management L.P."))
    ancora = filing_to_signal(_filing(filer="Ancora Advisors LLC"))
    assert elliott is not None and ancora is not None
    assert elliott.severity > ancora.severity


def test_signal_ticker_and_event_dt():
    sig = filing_to_signal(_filing(filed=date(2024, 3, 15), ticker="TGT"))
    assert sig is not None
    assert sig.ticker == "TGT"
    assert sig.event_dt.date() == date(2024, 3, 15)
    assert sig.extra["filer"].lower().startswith("elliott")
