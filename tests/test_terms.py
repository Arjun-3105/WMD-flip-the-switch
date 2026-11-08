import pytest

from mandate.terms import Mandate, Terms, find_overlap, overlap_exists, parse_constraints

ACME = "price<=48000 term=12..24 net>=30 seats>=180"
NIMBUS = "price>=50000@12 price>=48000@24 price>=45000@36 net<=45 seats>=150"


def m(company, spec):
    return Mandate(company, f"M-{company}", parse_constraints(spec), salt="s")


def test_parse_ranges_and_conditions():
    cs = parse_constraints("term=12..24 price>=45000@36 price<=48k")
    rendered = [c.render() for c in cs]
    assert rendered == ["term>=12", "term<=24", "price>=45000@36", "price<=48000"]


@pytest.mark.parametrize("bad", ["", "price<48000", "colour=red", "term=24..12", "price>=1..2"])
def test_parse_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        parse_constraints(bad)


def test_evaluate_reports_each_violation_with_a_code():
    v = m("acme", ACME).evaluate(Terms(49000, 36, 15, 100))
    assert {x.code for x in v} == {"PRICE_ABOVE_MAX", "TERM_ABOVE_MAX", "NET_BELOW_MIN", "SEATS_BELOW_MIN"}


def test_term_conditional_floors_only_apply_to_their_term():
    nimbus = m("nimbus", NIMBUS)
    assert nimbus.evaluate(Terms(45500, 36, 30, 200)) == []
    assert [x.code for x in nimbus.evaluate(Terms(45500, 24, 30, 200))] == ["PRICE_BELOW_MIN"]


def test_tcv_limits():
    assert m("a", "tcv<=100000").evaluate(Terms(46000, 24, 30, 200)) == []
    assert m("a", "tcv<=90000").evaluate(Terms(46000, 24, 30, 200))[0].code == "TCV_ABOVE_MAX"


def test_overlap_found_and_not_found():
    assert overlap_exists(m("acme", ACME), m("nimbus", NIMBUS))
    found = find_overlap(m("acme", ACME), m("nimbus", NIMBUS))
    assert m("acme", ACME).evaluate(found) == [] and m("nimbus", NIMBUS).evaluate(found) == []
    assert not overlap_exists(m("acme", "price<=40000"), m("nimbus", NIMBUS))


def test_commitment_hides_rules_but_binds_them():
    a, b = m("acme", ACME), m("acme", ACME)
    assert a.commitment() == b.commitment()
    b.salt = "other"
    assert a.commitment() != b.commitment()
    assert "48000" not in a.commitment()


def test_terms_validation():
    with pytest.raises(ValueError):
        Terms.from_dict({"price_per_year": 1, "term_months": 12, "payment_days": 30})
    with pytest.raises(ValueError):
        Terms.from_dict({"price_per_year": -5, "term_months": 12, "payment_days": 30, "seats": 1})
