from watcher.filters import filter_matches, is_internship, is_open


def job(**overrides):
    base = {
        "title": "Software Engineer Intern",
        "description": "Build backend services with Python.",
        "internship_type": "",
        "deadline_days_left": None,
        "role_classification": {"role": "swe"},
        "score": {"total": 60},
        "extra": {},
    }
    base.update(overrides)
    return base


def test_filters_keep_swe_internship_open_jobs():
    assert filter_matches([job()]) == [job()]


def test_filters_drop_non_swe_roles():
    assert filter_matches([job(role_classification={"role": "data_science"})]) == []


def test_filters_drop_new_grad_full_time_titles():
    assert not is_internship(job(title="Software Engineer New Grad"))
    assert filter_matches([job(title="Software Engineer New Grad")]) == []


def test_filters_drop_expired_or_inactive_jobs():
    assert not is_open(job(deadline_days_left=-1))
    assert not is_open(job(extra={"active": False}))
    assert filter_matches([job(deadline_days_left=-1), job(extra={"active": False})]) == []


def test_filters_optional_score_gate():
    assert filter_matches([job(score={"total": 69})], min_score=70) == []
    assert filter_matches([job(score={"total": 70})], min_score=70)


def test_full_time_title_with_intern_boilerplate_is_not_internship():
    # Full-time/senior title, but description mentions interns -> must NOT match.
    assert not is_internship(job(
        title="Staff Software Engineer",
        description="We also run a Summer 2026 internship program.",
    ))
    assert filter_matches([job(
        title="Staff Software Engineer",
        description="We also run a Summer 2026 internship program.",
    )]) == []


def test_title_based_internship_still_matches():
    assert is_internship(job(title="Software Engineer Intern - Summer 2026"))
    assert is_internship(job(title="Data Science Co-op"))


def test_truthy_non_intern_employment_type_is_not_internship():
    # Adapters store the ATS employment-type string in internship_type;
    # a plain truthiness check wrongly matched all of them.
    assert not is_internship(job(title="Security Reliability Engineer", internship_type="FullTime"))
    assert not is_internship(job(title="Web-App developer", internship_type="full"))
    assert not is_internship(job(title="Senior DevOps Engineer", internship_type="Contract"))
    assert filter_matches([job(title="Security Reliability Engineer", internship_type="FullTime")]) == []


def test_intern_employment_type_string_still_matches():
    assert is_internship(job(title="Software Engineer", internship_type="Intern"))
    assert is_internship(job(title="Backend Engineer", internship_type="internship"))
