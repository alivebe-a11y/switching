"""Tests for the fda_decision detector's classify() function."""

from switching.detectors.fda_decision import classify


# ---------------------------------------------------------------------------
# Approval detection
# ---------------------------------------------------------------------------

def test_fda_approves_basic():
    m = classify("Vertex Pharmaceuticals receives FDA approval for CASGEVY sickle cell therapy", "")
    assert m is not None
    assert m["direction"] == "approval"
    assert m["severity"] == 0.85


def test_fda_grants_approval():
    m = classify("Eli Lilly receives FDA approval for Mounjaro obesity indication", "")
    assert m is not None
    assert m["direction"] == "approval"


def test_fda_approved_phrasing():
    m = classify("FDA approves Biogen Leqembi for Alzheimer's disease treatment", "")
    assert m is not None
    assert m["direction"] == "approval"


# ---------------------------------------------------------------------------
# Rejection / CRL detection
# ---------------------------------------------------------------------------

def test_complete_response_letter():
    m = classify("Madrigal Pharmaceuticals receives complete response letter from FDA for resmetirom", "")
    assert m is not None
    assert m["direction"] == "rejection"
    assert m["severity"] == 0.80


def test_fda_rejects():
    m = classify("FDA rejects Syndax olutasidenib application citing manufacturing concerns", "")
    assert m is not None
    assert m["direction"] == "rejection"


def test_crl_abbreviation():
    m = classify("Acme Biotech receives CRL from FDA for lead oncology asset", "")
    assert m is not None
    assert m["direction"] == "rejection"


# ---------------------------------------------------------------------------
# Breakthrough therapy designation
# ---------------------------------------------------------------------------

def test_breakthrough_therapy_designation():
    m = classify("Arcus Biosciences receives FDA breakthrough therapy designation for etrumadenant", "")
    assert m is not None
    assert m["direction"] == "breakthrough"
    assert m["severity"] == 0.70


def test_fda_grants_breakthrough():
    m = classify("FDA grants breakthrough therapy designation to Moderna mRNA cancer vaccine", "")
    assert m is not None
    assert m["direction"] == "breakthrough"


# ---------------------------------------------------------------------------
# Fast track designation
# ---------------------------------------------------------------------------

def test_fast_track_designation():
    m = classify("Beam Therapeutics receives fast track designation for BEAM-101 sickle cell program", "")
    assert m is not None
    assert m["direction"] == "fast_track"
    assert m["severity"] == 0.60


def test_fda_grants_fast_track():
    m = classify("FDA grants fast track designation to novel gene therapy for rare disease", "")
    assert m is not None
    assert m["direction"] == "fast_track"


# ---------------------------------------------------------------------------
# Priority review
# ---------------------------------------------------------------------------

def test_priority_review_designation():
    m = classify("Recursion Pharmaceuticals receives priority review designation from FDA", "")
    assert m is not None
    assert m["direction"] == "priority_review"
    assert m["severity"] == 0.55


def test_pdufa_date():
    m = classify("BioNTech announces PDUFA date for personalized cancer vaccine NDA", "")
    assert m is not None
    assert m["direction"] == "priority_review"


# ---------------------------------------------------------------------------
# Advisory committee (AdCom)
# ---------------------------------------------------------------------------

def test_adcom_votes():
    m = classify("FDA advisory committee votes 11-2 in favor of Alzheimer's drug", "")
    assert m is not None
    assert m["direction"] == "adcom"
    assert m["severity"] == 0.65


def test_adcom_panel():
    m = classify("FDA advisory panel recommends approval of Atea antiviral therapy", "")
    assert m is not None
    assert m["direction"] == "adcom"


# ---------------------------------------------------------------------------
# First-in-class bonus (+0.10)
# ---------------------------------------------------------------------------

def test_first_in_class_bonus_on_approval():
    m = classify(
        "Amgen receives FDA approval for Lumakras first-in-class KRAS inhibitor lung cancer",
        "",
    )
    assert m is not None
    assert m["direction"] == "approval"
    assert m["severity"] == 0.95  # 0.85 + 0.10


def test_novel_drug_bonus():
    m = classify("FDA approves novel small molecule inhibitor for rare pediatric cancer", "")
    assert m is not None
    assert m["direction"] == "approval"
    assert m["severity"] == 0.95  # 0.85 + 0.10


# ---------------------------------------------------------------------------
# Severity cap at 0.95
# ---------------------------------------------------------------------------

def test_severity_cap():
    m = classify(
        "Company receives FDA approval for first-in-class blockbuster therapy",
        "Peak sales estimates exceed $5 billion annually for multi-billion dollar market.",
    )
    assert m is not None
    assert m["severity"] <= 0.95


# ---------------------------------------------------------------------------
# Rejects unrelated pharma / biotech news
# ---------------------------------------------------------------------------

def test_rejects_partnership_announcement():
    m = classify("Pfizer announces strategic partnership with BioNTech for oncology research", "")
    assert m is None


def test_rejects_earnings_release():
    m = classify("Eli Lilly reports record fourth quarter 2023 revenue of $9.4 billion", "")
    assert m is None


def test_rejects_clinical_trial_start():
    m = classify("Moderna initiates Phase 2 clinical trial for respiratory syncytial virus vaccine", "")
    assert m is None


def test_rejects_generic_drug_mention():
    m = classify("Generic pharmaceutical company announces new manufacturing facility", "")
    assert m is None
