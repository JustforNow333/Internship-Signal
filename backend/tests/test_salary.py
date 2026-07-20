import pytest

from app.salary import hourly_mid, parse_compensation


@pytest.mark.parametrize("raw,kind,period,hr_min,hr_max", [
    ("$25/hr", "paid", "hourly", 25.0, 25.0),
    ("$15.50/hr", "paid", "hourly", 15.5, 15.5),
    ("$4,000 monthly", "paid", "monthly", 25.0, 25.0),
    ("$4k/month", "paid", "monthly", 25.0, 25.0),
    ("$12,000 monthly", "paid", "monthly", 75.0, 75.0),
    ("25-30/hour", "paid", "hourly", 25.0, 30.0),
    ("$45-55/hour", "paid", "hourly", 45.0, 55.0),
    ("$3,000 for the summer", "paid", "term", 6.25, 6.25),
])
def test_paid_formats(raw, kind, period, hr_min, hr_max):
    c = parse_compensation(raw)
    assert c["kind"] == kind
    assert c["period"] == period
    assert c["usd_hourly_min"] == hr_min
    assert c["usd_hourly_max"] == hr_max


def test_bare_annual_number_is_assumed_and_penalized():
    c = parse_compensation("80k")
    assert c["kind"] == "paid"
    assert c["period"] == "annual" and c["period_assumed"] is True
    assert c["usd_hourly_min"] == pytest.approx(38.46)
    # An assumed period plus an assumed currency must not look confident.
    assert c["confidence"] < 0.6
    assert any("assumed" in n.lower() for n in c["notes"])


def test_shared_unit_range():
    c = parse_compensation("$80-90k")
    assert c["amount_min"] == 80_000 and c["amount_max"] == 90_000
    assert c["usd_hourly_min"] == pytest.approx(38.46)
    assert c["usd_hourly_max"] == pytest.approx(43.27)


def test_inr_lakh_uses_lpa_convention():
    c = parse_compensation("₹1.5L - ₹2.4L")
    assert c["currency"] == "INR"
    assert c["period"] == "annual" and c["period_assumed"] is True
    assert c["usd_hourly_min"] == pytest.approx(0.87)
    assert c["usd_hourly_max"] == pytest.approx(1.38)
    assert any("LPA" in n for n in c["notes"])


@pytest.mark.parametrize("raw,kind", [
    ("Unpaid", "unpaid"),
    ("Unpaid - college credit available", "unpaid"),
    ("Equity only", "equity_only"),
    ("Commission only", "commission_only"),
    ("Competitive", "unknown_vague"),
    ("Negotiable / DOE", "unknown_vague"),
    ("Stipend provided", "stipend_unspecified"),
])
def test_non_cash_kinds(raw, kind):
    assert parse_compensation(raw)["kind"] == kind


def test_unpaid_is_zero_dollars_not_unknown():
    c = parse_compensation("unpaid")
    assert c["usd_hourly_min"] == 0.0 and c["usd_hourly_max"] == 0.0
    assert hourly_mid(c) == 0.0


def test_blank_is_unknown_with_zero_confidence():
    c = parse_compensation("")
    assert c["kind"] == "unknown"
    assert c["confidence"] == 0.0
    assert hourly_mid(c) is None


def test_401k_is_not_a_salary():
    c = parse_compensation("401(k) matching available")
    assert c["kind"] != "paid" or c["usd_hourly_min"] is None


def test_hours_per_week_is_not_pay():
    # "40 hrs/week" must not be read as $40/hr.
    c = parse_compensation("$20/hr, 40 hrs/week")
    assert c["usd_hourly_min"] == 20.0 and c["usd_hourly_max"] == 20.0


def test_percentage_only_compensation_is_not_parsed_as_cash():
    equity = parse_compensation("0.5% equity")
    bonus = parse_compensation("10% performance bonus")

    assert equity["kind"] == "equity_only"
    assert equity["usd_hourly_min"] is None
    assert bonus["kind"] != "paid"
    assert bonus["usd_hourly_min"] is None


def test_competitive_salary_plus_equity_is_not_labeled_equity_only():
    compensation = parse_compensation("Competitive salary plus equity")

    assert compensation["kind"] == "unknown_vague"


def test_negated_unpaid_phrase_does_not_override_explicit_cash_pay():
    compensation = parse_compensation("This is not an unpaid internship; $20/hour")

    assert compensation["kind"] == "paid"
    assert compensation["usd_hourly_min"] == 20.0


def test_negated_unpaid_phrase_does_not_mask_a_separate_unpaid_statement():
    compensation = parse_compensation(
        "This is not an unpaid trial, but the internship offers no compensation"
    )

    assert compensation["kind"] == "unpaid"


def test_unspecified_stipend_plus_equity_is_not_labeled_equity_only():
    compensation = parse_compensation("Stipend provided plus equity")

    assert compensation["kind"] == "stipend_unspecified"
