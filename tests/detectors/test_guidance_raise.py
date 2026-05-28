"""Tests for the guidance_raise detector."""

from switching.detectors.guidance_raise import classify


def test_raises_guidance():
    m = classify("NVDA Raises Full-Year Revenue Guidance", "")
    assert m is not None
    assert m["direction"] == "raise"
    assert m["full_year"] is True
    assert m["severity"] >= 0.70


def test_raises_outlook():
    m = classify("AMD Raises Outlook for Q3 Revenue", "")
    assert m is not None
    assert m["direction"] == "raise"


def test_increases_forecast():
    m = classify("Tesla Increases Full-Year Delivery Forecast", "")
    assert m is not None
    assert m["direction"] == "raise"
    assert m["full_year"] is True


def test_lowers_guidance():
    m = classify("Intel Lowers Full-Year Revenue Guidance", "")
    assert m is not None
    assert m["direction"] == "lower"
    assert m["severity"] >= 0.65


def test_cuts_outlook():
    m = classify("FedEx Cuts Outlook Amid Weakening Demand", "")
    assert m is not None
    assert m["direction"] == "lower"


def test_pre_announce_beat():
    m = classify("Shopify Pre-Announces Results Above Guidance", "")
    assert m is not None
    assert m["direction"] == "pre_announce_beat"
    assert m["severity"] >= 0.70


def test_pre_announce_miss():
    m = classify("Snap Pre-Announces Results Below Expectations", "")
    assert m is not None
    assert m["direction"] == "pre_announce_miss"
    assert m["severity"] >= 0.65


def test_revises_upward():
    m = classify("CrowdStrike Revises Guidance Upward for FY2025", "")
    assert m is not None
    assert m["direction"] == "raise"


def test_large_raise_bonus():
    m = classify(
        "Meta Raises FY Revenue Guidance",
        "Company now expects revenue of $150 billion, up from $120 billion to $150 billion.",
    )
    assert m is not None
    assert m["large_raise"] is True
    assert m["severity"] >= 0.80


def test_rejects_quarterly_earnings():
    assert classify("AAPL Reports Q3 Quarterly Results Above Expectations", "") is None


def test_rejects_regular_report():
    assert classify("Amazon Reports Q2 Revenue of $134 Billion", "") is None


def test_rejects_unrelated():
    assert classify("Google announces new Pixel phone lineup", "") is None


def test_severity_capped():
    m = classify(
        "Company Raises Full-Year Guidance",
        "Now expects revenue of $200 billion, up from $150 billion to $200 billion.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_full_year_bonus():
    m_fy = classify("MSFT Raises Full-Year Guidance", "")
    m_q = classify("MSFT Raises Guidance", "")
    assert m_fy is not None and m_q is not None
    assert m_fy["severity"] > m_q["severity"]


def test_narrows_range():
    m = classify("Apple Narrows Guidance Range to Upper End", "")
    assert m is not None
    assert m["direction"] == "raise"


# ---------------------------------------------------------------------------
# Law-firm class-action exclusions — sourced from live post-mortem (2026-05-28).
# 4 of 8 guidance_raise losers were Levi & Korsinsky / similar firm PRs whose
# boilerplate summary text matched the RAISE regex. None are real guidance changes.
# ---------------------------------------------------------------------------

class TestLawFirmExclusions:
    def test_excludes_levi_korsinsky_securities_fraud_pr(self):
        # TLSI -4.6% loser
        assert classify(
            "TLSI (TLSI) Securities Fraud Investigation - Levi & Korsinsky"
        ) is None

    def test_excludes_levi_korsinsky_investigates_officers(self):
        # UPWK -3.6% loser
        assert classify(
            "Upwork Inc. Investigation Initiated: Levi & Korsinsky Investigates the "
            "Officers and Directors of Upwork Inc. (UPWK)"
        ) is None

    def test_excludes_securities_claims_investigation(self):
        # WGS -2.6% loser
        assert classify(
            "Levi & Korsinsky Announces Investigation of Securities Claims Against "
            "GeneDx Holdings Corp. (WGS)"
        ) is None

    def test_excludes_other_law_firms(self):
        # Same pattern from other class-action firms — must also be blocked
        assert classify(
            "Bragar Eagel & Squire, P.C. Investigates Aterian, Inc. on Behalf of "
            "Long-Term Stockholders"
        ) is None
        assert classify(
            "The Schall Law Firm Announces Investigation of Securities Claims Against XYZ"
        ) is None
        assert classify(
            "Robbins Geller Rudman & Dowd LLP Files Securities Class Action Against ABC"
        ) is None

    def test_excludes_when_law_firm_phrasing_is_only_in_summary(self):
        # Title alone looks innocuous; summary contains the boilerplate.
        # This is the real-world case: the SUMMARY's "raises serious concerns
        # about prior guidance" triggered _RAISE_RX before the exclusion existed.
        assert classify(
            "Important Investor Notice Regarding XYZ Corp",
            "Levi & Korsinsky LLP encourages investors to contact the firm regarding "
            "XYZ's previously issued guidance. The investigation raises serious "
            "concerns about the company's outlook."
        ) is None

    # ── Critical: legitimate guidance raises must still classify ──
    def test_real_guidance_raise_still_matches(self):
        # SIBN +15.8% biggest winner — must still fire
        m = classify(
            "SI-BONE, Inc. Reports Financial Results for the First Quarter 2026 "
            "and Raises 2026 Guidance"
        )
        assert m is not None
        assert m["direction"] == "raise"

    def test_cvs_full_year_raise_still_matches(self):
        # CVS +7.9% winner
        m = classify(
            "CVS HEALTH CORPORATION REPORTS STRONG FIRST QUARTER 2026 RESULTS "
            "AND RAISES FULL-YEAR 2026 GUIDANCE"
        )
        assert m is not None
        assert m["direction"] == "raise"
        assert m["full_year"] is True

    def test_trivago_legit_guidance_raise_still_matches(self):
        m = classify(
            "trivago Delivers 15% Growth in Q1 and Raises Guidance After Fifth "
            "Consecutive Double-Digit Quarter"
        )
        assert m is not None
        assert m["direction"] == "raise"

    def test_taboola_legit_full_year_raise_still_matches(self):
        m = classify(
            "Taboola Reports Strong First Quarter 2026 Results Exceeding High-End "
            "of Guidance, Raises Full-Year Outlook"
        )
        assert m is not None
        assert m["direction"] == "raise"
