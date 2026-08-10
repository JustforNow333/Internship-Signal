import pytest

from watcher.season_terms import season_term_matches, terms_match


@pytest.mark.parametrize(
    "source_term",
    [
        "Summer 2027",
        "Summer-2027",
        "Summer 2027 Internship",
        "2027 Summer",
        "Summer '27",
        "Summer ’27",
    ],
)
def test_equivalent_summer_year_forms_match(source_term):
    assert season_term_matches(source_term, "Summer 2027")


@pytest.mark.parametrize(
    ("source_term", "configured_term"),
    [
        ("Autumn 2027", "Fall 2027"),
        ("Fall-2027 Internship", "Autumn 2027"),
        ("2027 Spring", "Spring 2027"),
        ("Winter '27", "Winter 2027"),
    ],
)
def test_supported_seasons_and_fall_autumn_equivalence(source_term, configured_term):
    assert season_term_matches(source_term, configured_term)


@pytest.mark.parametrize(
    "source_term",
    [
        "Summer 2028",
        "Fall 2027",
        "2027 Internship",
        "Summer",
        "Summer 2027 Analyst",
        "Summer 27",
        "Summer '2027",
        "Summer 20270",
    ],
)
def test_different_incomplete_or_ambiguous_terms_do_not_match(source_term):
    assert not season_term_matches(source_term, "Summer 2027")


def test_generic_terms_remain_exact_after_case_and_whitespace_normalization():
    assert season_term_matches("  Early   Careers  ", "early careers")
    assert not season_term_matches("Early Career", "early careers")
    assert not season_term_matches("Technology Internship", "Internship")
    assert not season_term_matches("Internship", "Summer 2027")


def test_term_collections_match_any_exact_or_equivalent_pair():
    assert terms_match(["Fall 2027", "Summer '27"], ["Summer 2027"])
    assert not terms_match(["Fall 2027"], ["Summer 2027"])
    assert terms_match(["anything"], [])
