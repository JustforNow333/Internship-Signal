"""Guards for the canonical direct-source registry."""

from watcher.config import NON_DIRECT_ATS, supported_ats
from watcher.run import _DirectSourceProvider, _default_direct_sources
from watcher.sources.base import DirectSourceDiagnostics
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_SOURCE_SPECS,
    build_direct_sources,
)
from watcher.sources.workday import WorkdayPacer, WorkdaySource


def test_direct_ats_surface_is_unchanged():
    assert DIRECT_ATS == frozenset(
        {
            "ashby",
            "bain",
            "brassring",
            "epic",
            "greenhouse",
            "ibm",
            "icims",
            "lever",
            "oracle_hcm",
            "paylocity",
            "smartrecruiters",
            "successfactors",
            "talentbrew",
            "workable",
            "workday",
        }
    )


def test_every_registered_direct_ats_can_be_constructed():
    sources = build_direct_sources()

    assert set(sources) == set(DIRECT_ATS)
    for ats, source in sources.items():
        assert source is not None, ats
        assert callable(getattr(source, "fetch", None)), ats


def test_every_registered_direct_ats_publishes_shared_health_diagnostics():
    """Collection reads one contract, so every adapter must expose it.

    `watcher/collection.py` no longer translates adapter-specific diagnostics,
    so an adapter that never publishes `last_health_diagnostics` would silently
    report no health evidence at all.
    """

    for ats, source in build_direct_sources().items():
        published = getattr(source, "last_health_diagnostics", None)
        assert isinstance(published, DirectSourceDiagnostics), ats
        # Nothing has been fetched yet, so no adapter claims a result.
        assert published.succeeded is None, ats


def test_registry_entries_are_unique_and_well_formed():
    names = [spec.ats for spec in DIRECT_SOURCE_SPECS]

    assert len(names) == len(set(names))
    assert all(name and name == name.strip().casefold() for name in names)
    assert not set(names) & NON_DIRECT_ATS


def test_supported_ats_is_the_registry_plus_non_direct_modes():
    assert supported_ats() == DIRECT_ATS | NON_DIRECT_ATS
    assert NON_DIRECT_ATS == frozenset({"bespoke", "github_only"})
    assert not DIRECT_ATS & NON_DIRECT_ATS


def test_runtime_adapter_construction_stays_aligned_with_the_registry():
    assert set(_default_direct_sources()) == set(DIRECT_ATS)
    assert _DirectSourceProvider(None, concurrent=True)._supported == DIRECT_ATS
    assert _DirectSourceProvider(None, concurrent=False)._supported == DIRECT_ATS


def test_workday_is_built_with_the_supplied_shared_pacer():
    pacer = WorkdayPacer(0.5)

    sources = build_direct_sources(workday_pacer=pacer)

    workday = sources["workday"]
    assert isinstance(workday, WorkdaySource)
    assert workday._pacer is pacer
    assert build_direct_sources()["workday"]._pacer is not pacer


def test_only_workday_requests_the_shared_pacer():
    pacer_aware = {spec.ats for spec in DIRECT_SOURCE_SPECS if spec.needs_workday_pacer}

    assert pacer_aware == {"workday"}
