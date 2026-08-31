"""Canonical registry of direct ATS source adapters.

This module is the single registration point for direct sources. To add one,
append a :class:`DirectSourceSpec` to :data:`DIRECT_SOURCE_SPECS` and nothing
else:

* ``watcher.config.validation`` derives the accepted direct watchlist ``ats``
  values from :data:`DIRECT_ATS`.
* ``watcher/run.py`` builds runtime adapters with :func:`build_direct_sources`.

GitHub backstop feeds are configured per watchlist entry rather than per ATS,
so they are deliberately not registered here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from watcher.sources.ashby import AshbySource
from watcher.sources.bain import BainSource
from watcher.sources.brassring import BrassRingSource
from watcher.sources.epic import EpicSource
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.ibm import IbmSource
from watcher.sources.icims import IcimsSource
from watcher.sources.lever import LeverSource
from watcher.sources.oracle_hcm import OracleHcmSource
from watcher.sources.paylocity import PaylocitySource
from watcher.sources.smartrecruiters import SmartRecruitersSource
from watcher.sources.successfactors import SuccessFactorsSource
from watcher.sources.talentbrew import TalentBrewSource
from watcher.sources.taleo_sourcing import TaleoSourcingSource
from watcher.sources.workable import WorkableSource
from watcher.sources.workday import WorkdayPacer, WorkdaySource


@dataclass(frozen=True)
class DirectSourceSpec:
    """One registered direct ATS adapter and how to construct it.

    ``needs_workday_pacer`` marks the adapters whose constructor takes the
    shared :class:`WorkdayPacer`, so tenant pacing is never weakened by
    per-thread adapter construction. Every other adapter is built with no
    arguments.
    """

    ats: str
    factory: Callable[..., object]
    needs_workday_pacer: bool = False

    def build(self, *, workday_pacer: WorkdayPacer | None = None) -> object:
        if self.needs_workday_pacer:
            return self.factory(pacer=workday_pacer)
        return self.factory()


DIRECT_SOURCE_SPECS: tuple[DirectSourceSpec, ...] = (
    DirectSourceSpec("ashby", AshbySource),
    DirectSourceSpec("bain", BainSource),
    DirectSourceSpec("brassring", BrassRingSource),
    DirectSourceSpec("epic", EpicSource),
    DirectSourceSpec("greenhouse", GreenhouseSource),
    DirectSourceSpec("ibm", IbmSource),
    DirectSourceSpec("icims", IcimsSource),
    DirectSourceSpec("lever", LeverSource),
    DirectSourceSpec("oracle_hcm", OracleHcmSource),
    DirectSourceSpec("paylocity", PaylocitySource),
    DirectSourceSpec("smartrecruiters", SmartRecruitersSource),
    DirectSourceSpec("successfactors", SuccessFactorsSource),
    DirectSourceSpec("talentbrew", TalentBrewSource),
    DirectSourceSpec("taleo_sourcing", TaleoSourcingSource),
    DirectSourceSpec("workable", WorkableSource),
    DirectSourceSpec("workday", WorkdaySource, needs_workday_pacer=True),
)

DIRECT_ATS: frozenset[str] = frozenset(spec.ats for spec in DIRECT_SOURCE_SPECS)


def build_direct_sources(
    *,
    workday_pacer: WorkdayPacer | None = None,
) -> dict[str, object]:
    """Construct one adapter instance per registered direct ATS."""

    return {
        spec.ats: spec.build(workday_pacer=workday_pacer)
        for spec in DIRECT_SOURCE_SPECS
    }
