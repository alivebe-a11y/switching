"""Tests for the dividend_surprise detector."""

from switching.detectors.dividend_surprise import classify


def test_special_dividend():
    m = classify("Costco Declares Special Cash Dividend of $15 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 15.0
    assert m["severity"] >= 0.85  # base 0.80 + large bonus


def test_small_special_dividend():
    m = classify("Ford Announces Special Dividend of $0.50 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 0.50
    assert m["severity"] >= 0.75


def test_dividend_initiation():
    m = classify("Meta Platforms Initiates Quarterly Dividend of $0.50 Per Share", "")
    assert m is not None
    assert m["direction"] == "initiation"
    assert m["per_share"] == 0.50
    assert m["severity"] >= 0.70


def test_first_dividend():
    m = classify("Alphabet Declares Its First-Ever Quarterly Dividend of $0.20 Per Share", "")
    assert m is not None
    assert m["direction"] == "initiation"


def test_dividend_increase():
    m = classify("Microsoft Increases Quarterly Dividend by 10%", "")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["severity"] >= 0.55


def test_large_dividend_increase():
    m = classify("Apple Raises Dividend", "The 25% increase brings the quarterly payout to $0.25 per share.")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["pct_increase"] == 25.0
    assert m["severity"] >= 0.65


def test_dividend_cut():
    m = classify("Intel Cuts Quarterly Dividend by 66%", "")
    assert m is not None
    assert m["direction"] == "cut"
    assert m["severity"] >= 0.60


def test_dividend_suspension():
    m = classify("Boeing Suspends Dividend Amid 737 MAX Crisis", "")
    assert m is not None
    assert m["direction"] == "cut"


def test_dividend_hike():
    m = classify("JPMorgan Hikes Dividend to $1.05 Per Share", "")
    assert m is not None
    assert m["direction"] == "increase"
    assert m["per_share"] == 1.05


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("NVDA beats estimates on surging AI demand", "") is None


def test_rejects_stock_split():
    assert classify("Amazon announces 20-for-1 stock split", "") is None


def test_severity_capped():
    m = classify("Company Declares Special Cash Dividend of $100 Per Share", "")
    assert m is not None
    assert m["severity"] <= 0.95


def test_extraordinary_dividend():
    m = classify("MSFT Announces Extraordinary Dividend of $3.00 Per Share", "")
    assert m is not None
    assert m["direction"] == "special"
    assert m["per_share"] == 3.0


# ---------------------------------------------------------------------------
# Vanilla-declaration exclusion — sourced from live post-mortem (2026-05-28).
# Counterfactual on 22 closed trades: tightening lifts WR 59%->69% and avg
# return +0.22% -> +0.92% by blocking routine "declares quarterly dividend"
# announcements that consistently lose.
# ---------------------------------------------------------------------------

class TestVanillaDeclareExclusion:
    """Routine "Declares <Nth> Quarter Dividend" headlines are no-op
    recurring announcements with no surprise content. Block them unless
    the TITLE also has change-direction language (increase/special/init)."""

    def test_excludes_first_quarter_dividend_declaration(self):
        # TRU -3.6% loser
        assert classify(
            "TransUnion Declares First Quarter 2026 Dividend of $0.125 per Share"
        ) is None

    def test_excludes_quarterly_dividend_declaration(self):
        # LFVN -3.6% loser
        assert classify("LifeVantage Declares Quarterly Dividend") is None

    def test_excludes_sprott_quarterly(self):
        # SII -2.6% loser
        assert classify("Sprott Inc. Declares First Quarter 2026 Dividend") is None

    def test_excludes_second_quarter_declaration(self):
        # CCAP -4.6% loser
        assert classify(
            "Crescent Capital BDC, Inc. Reports First Quarter 2026 Earnings Results; "
            "Declares a Second Quarter Dividend"
        ) is None

    def test_excludes_regular_quarterly_when_no_other_signal(self):
        assert classify("Company XYZ Declares Regular Quarterly Cash Dividend") is None

    # ── Critical: real surprises must still classify ──
    def test_increase_still_matches_via_title(self):
        # NACCO +5.0% biggest title-only-increase winner
        m = classify("NACCO INDUSTRIES INCREASES DIVIDEND BY 4%")
        assert m is not None
        assert m["direction"] == "increase"

    def test_raises_quarterly_still_matches(self):
        # TKR +1.4% winner
        m = classify(
            "Timken Raises Quarterly Dividend to 36 Cents Per Share; "
            "Marking 13 Years of Increases"
        )
        assert m is not None
        assert m["direction"] == "increase"

    def test_special_dividend_still_matches(self):
        # TK +2.7% winner — title says "Declares" BUT also "Special Dividend"
        m = classify(
            "Teekay Corporation Ltd. First Quarter 2026 Update; "
            "and Declares a Special Dividend"
        )
        assert m is not None
        assert m["direction"] == "special"

    def test_supplemental_dividend_still_matches(self):
        # NOV -4.6% loser BUT structurally a real surprise (supplemental).
        # We deliberately let it through — special dividends ARE catalysts;
        # NOV happening to lose is noise, not a regex failure.
        m = classify("NOV Declares Regular Quarterly Dividend and Supplemental Dividend")
        assert m is not None
        assert m["direction"] == "special"

    def test_consecutive_increase_still_matches(self):
        # CB +1.0% winner
        m = classify(
            "Chubb Limited Shareholders Approve 33rd Consecutive Annual Dividend "
            "Increase; Chubb Limited Announces Next Quarterly Dividend Payment"
        )
        assert m is not None
        assert m["direction"] == "increase"

    def test_n_percent_increase_still_matches(self):
        # CPK +1.0% winner
        m = classify("Chesapeake Utilities Corporation Raises Dividend by 7.3 Percent")
        assert m is not None
        assert m["direction"] == "increase"
