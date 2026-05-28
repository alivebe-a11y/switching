"""Tests for the mna_target detector."""

from switching.detectors.mna_target import classify


def test_acquisition_target():
    m = classify("Activision Blizzard to Be Acquired by Microsoft for $95 Per Share in All-Cash Deal", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["all_cash"] is True
    assert m["price_per_share"] == 95.0
    assert m["severity"] >= 0.85


def test_definitive_agreement():
    m = classify("VMware Signs Definitive Agreement to Be Acquired by Broadcom", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["definitive"] is True
    assert m["severity"] >= 0.85


def test_acquirer_side():
    m = classify("Pfizer Acquires Seagen for $43 Billion", "")
    assert m is not None
    assert m["direction"] == "acquirer"
    assert m["severity"] >= 0.50


def test_merger_agreement():
    m = classify("Kroger and Albertsons Enter into Merger Agreement", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["severity"] >= 0.80


def test_tender_offer():
    m = classify("LVMH Launches Tender Offer for Tiffany at $135 Per Share", "")
    assert m is not None
    assert m["direction"] == "target"
    assert m["price_per_share"] == 135.0


def test_all_cash_bonus():
    m_cash = classify("Company to Be Acquired by BigCorp in All-Cash Deal", "Definitive agreement signed.")
    m_mix = classify("Company to Be Acquired by BigCorp in Cash-and-Stock Deal", "")
    assert m_cash is not None and m_mix is not None
    assert m_cash["severity"] > m_mix["severity"]


def test_uncertain_penalty():
    m = classify("BigCorp Exploring Potential Acquisition of SmallCo", "")
    assert m is not None
    assert m["uncertain"] is True
    assert m["severity"] < 0.80


def test_premium_extraction():
    m = classify("Target Corp agrees to be acquired", "The deal represents a 40% premium to the closing price.")
    assert m is not None
    assert m["premium_pct"] == 40.0


def test_rejects_unrelated():
    assert classify("Apple launches new MacBook Pro lineup", "") is None


def test_rejects_earnings():
    assert classify("Microsoft reports Q3 revenue of $52.9 billion", "") is None


def test_severity_capped():
    m = classify(
        "Company to Be Acquired in All-Cash Definitive Agreement",
        "The deal represents a 50% premium.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


def test_to_acquire_is_acquirer():
    """'Company to Acquire X' = acquirer-side — should be direction=acquirer."""
    m = classify("Amazon to Acquire One Medical for $3.9 Billion", "")
    assert m is not None
    assert m["direction"] == "acquirer"
    assert m["severity"] >= 0.50


def test_acquirer_definitive_agreement():
    """'Signs Definitive Agreement to Acquire X' = acquirer, not target."""
    m = classify("Microsoft Signs Definitive Agreement to Acquire Activision Blizzard", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_completes_acquisition_is_acquirer():
    """'Completes acquisition of X' = acquirer."""
    m = classify("Suncrete Inc Completes Acquisition of Nelson Bros Ready Mix", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_following_acquisition_of_is_acquirer():
    """'Revenue growth following acquisition of X' = acquirer."""
    m = classify("SEGG Media Reports 1400% Revenue Growth Following Acquisition of Veloce Media Group", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_exercises_option_to_acquire_is_acquirer():
    """'Exercises option to acquire X' = acquirer."""
    m = classify("Artivion Reports Q1 Results and Announces Exercise of Option to Acquire Endospan", "")
    assert m is not None
    assert m["direction"] == "acquirer"


def test_divestiture_is_acquirer():
    """Divestitures are detected as acquirer-side (neither party is a pure target)."""
    m = classify("FMC Corporation Announces Agreement to Divest India Business to Crystal Crop", "")
    assert m is not None
    assert m["direction"] == "acquirer"


# ---------------------------------------------------------------------------
# Non-M&A exclusions — drawn from real funnel false-positives.
# When a future commit relaxes _EXCLUDE_RX these tests will catch the regression.
# ---------------------------------------------------------------------------

class TestNonMnaExclusions:
    """Patterns that look like M&A but aren't — debt tenders, real estate,
    buybacks, law-firm PRs, junior-exchange shell games. All sourced from
    the live funnel data on 2026-05-28 where they were dominating drop volume."""

    # ── Debt tender offers (not stock takeovers) ───────────────────────
    def test_excludes_debt_tender_senior_secured_notes(self):
        # DIRECTV: x69 funnel rows
        assert classify(
            "DIRECTV Financing, LLC and DIRECTV Financing Co-Obligor, Inc. Announce "
            "Consideration for Tender Offer for Up to $1,400,000,000 in Aggregate Principal "
            "Amount of Their 5.875% Senior Secured Notes Due 2027"
        ) is None

    def test_excludes_debt_tender_six_series(self):
        # BCE (Bell Canada): x13 funnel rows
        assert classify(
            "Bell Announces Cash Tender Offers for Six Series of Debt Securities"
        ) is None

    def test_excludes_debt_tender_subordinated_notes(self):
        # SCOR: x8 funnel rows
        assert classify(
            "SCOR announces the launch of a cash tender offer and its intention to issue "
            "new subordinated notes"
        ) is None

    def test_excludes_early_results_of_tender(self):
        # Follow-up debt-tender announcement — same DIRECTV refinancing
        assert classify(
            "DIRECTV Financing, LLC Announce Early Results of Cash Tender Offer for "
            "5.875% Senior Secured Notes Due 2027"
        ) is None

    def test_does_NOT_exclude_stock_tender_offer(self):
        # Legitimate tender offer for SHARES (not debt) — must still match
        m = classify("Microsoft Tender Offer for All Outstanding Shares of Activision at $95")
        assert m is not None
        assert m["direction"] == "target"

    # ── Real-estate / commercial property transactions ──────────────────
    def test_excludes_industrial_portfolio(self):
        # Provident Industrial: x44 funnel rows
        assert classify(
            "Provident Industrial Acquires Commerce 45, a 1.5 Million-Square-Foot "
            "Two-Building Industrial Portfolio in Hutchins, Texas"
        ) is None

    def test_excludes_office_condominium(self):
        # REALM/DelShah/CitySpire: x70 funnel rows
        assert classify(
            "REALM, DelShah Capital and A.M. Properties Acquire CitySpire, "
            "156 West 56th Street, a Premier New York Midtown Office Condominium"
        ) is None

    def test_excludes_sf_industrial_facility(self):
        # Brennan: x9 funnel rows
        assert classify(
            "Brennan Investment Group Acquires 55,000 SF Industrial Facility in "
            "Bolingbrook, Illinois via Sale-Leaseback"
        ) is None

    def test_excludes_multifamily_property(self):
        # Bascom: x4 funnel rows
        assert classify(
            "Bascom Arizona Ventures Continues Acquisition Spree, Acquires Off-Market "
            "Tucson Multifamily Property for $45.5 Million"
        ) is None

    # ── Share buybacks (belong to the buyback detector, not mna_target) ─
    def test_excludes_share_buyback_programme(self):
        # Schouw: x3 funnel rows
        assert classify("Schouw & Co. share buy-back programme, week 21 2026") is None

    def test_excludes_acquisition_of_own_shares(self):
        # IBA: x15 funnel rows
        assert classify("IBA - ACQUISITION OF OWN SHARES") is None

    # ── Law-firm announcements (class actions, not M&A) ────────────────
    def test_excludes_law_firm_announcement(self):
        # PARRIS: x7 funnel rows
        assert classify(
            "PARRIS Law Firm Offers Counsel to Garden Grove Chemical Leak Survivors"
        ) is None

    # ── Junior-exchange qualifying transactions (shell-game M&A) ───────
    def test_excludes_qualifying_transaction(self):
        # Chicane Capital: x1 row
        assert classify(
            "Chicane Capital I Corp. and Elton Resources Corp. Enter Into Definitive "
            "Merger Agreement with Respect to Qualifying Transaction and Brokered Private "
            "Placement of Subscription Receipts"
        ) is None

    # ── Critical: legitimate M&A must still classify after the exclusion regex ─
    def test_real_pharma_acquisition_still_matches(self):
        m = classify("Olympus to Acquire BioProtect to Expand Its Portfolio of "
                     "Urological Technologies and Address Prostate Cancer")
        assert m is not None
        assert m["direction"] == "acquirer"

    def test_lilly_target_acquisition_still_matches(self):
        m = classify("Curevo to be Acquired by Lilly to Advance Next-Generation "
                     "Shingles Prevention")
        assert m is not None
        assert m["direction"] == "target"

    def test_pfizer_seagen_still_matches(self):
        # The original headline this detector was built for
        m = classify("Pfizer Acquires Seagen for $43 Billion in All-Cash Deal")
        assert m is not None
        assert m["direction"] == "acquirer"

    def test_activision_microsoft_still_matches(self):
        # The other original headline — confirms target detection survives
        # the exclusion regex. (all_cash is asserted elsewhere with a
        # headline that actually contains "all-cash".)
        m = classify(
            "Activision Blizzard to Be Acquired by Microsoft for $95 Per Share"
        )
        assert m is not None
        assert m["direction"] == "target"
        assert m["price_per_share"] == 95.0
