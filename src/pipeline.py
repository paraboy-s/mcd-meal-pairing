"""编排层：把五个引擎串成一次完整决策。"""
from __future__ import annotations

from datetime import datetime

from . import coupon_engine as CE
from . import explain_engine as EE
from . import guard_engine as GE
from .data import Catalog
from .models import Constraints, Report, Tier
from .solver_engine import Solver
from .taste_engine import build_fingerprint


def run(constraints: Constraints, catalog: Catalog | None = None) -> Report:
    cat = catalog or Catalog()
    fp = build_fingerprint(cat.orders, _now(constraints.now))
    solver = Solver(cat)

    res = solver.solve(constraints, fp)
    tiers: list[Tier] = res["tiers"]

    warnings = GE.allergen_report(cat, constraints.avoid)
    for t in tiers:
        warnings += GE.validate(t.plan, cat, constraints, t.nutrient)

    elim = EE.eliminated(res["candidates"], tiers)
    cf = EE.counterfactuals(solver, constraints, fp, tiers)
    notes = CE.plan_notes(cat.coupons, constraints.now, constraints.channel)
    notes += CE.relevance(cat.coupons, cat, fp)

    return Report(
        constraints=constraints,
        tiers=tiers,
        fingerprint=fp,
        eliminated=elim,
        counterfactuals=cf,
        coupon_notes=notes,
        stats=res["stats"],
        warnings=warnings,
    )


def _now(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.now()
