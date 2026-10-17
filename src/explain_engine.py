"""解释引擎：反事实 + 淘汰理由 + 决策溯源。

解释的目标不是「说得好听」，而是让用户知道下一步该动哪个旋钮。
"""
from __future__ import annotations

import copy

from .models import Constraints, Tier
from .solver_engine import Candidate, Solver


def counterfactuals(solver: Solver, c: Constraints, fp: dict, tiers: list[Tier],
                    deltas: tuple[int, ...] = (2, 4, 6, 8)) -> list[str]:
    """预算再加 X 元，均衡档会变成什么？只报「真的变了」的那几档。"""
    if not tiers:
        return []
    base = _by_key(tiers, "balanced") or tiers[0]
    out: list[str] = []
    seen = {base.plan.main.name}
    for d in deltas:
        c2 = copy.replace(c, budget=c.budget + d) if hasattr(copy, "replace") else _clone(c, c.budget + d)
        res = solver.solve(c2, fp)
        if not res["tiers"]:
            continue
        nxt = _by_key(res["tiers"], "balanced") or res["tiers"][0]
        if nxt.plan.main.name in seen:
            continue
        seen.add(nxt.plan.main.name)
        dk = nxt.nutrient.kcal - base.nutrient.kcal
        dp = nxt.nutrient.protein - base.nutrient.protein
        out.append(
            f"预算 +{d} 元 → 可以换成「{nxt.plan.main.name}」"
            f"（热量 {dk:+.0f} kcal，蛋白 {dp:+.0f} g，到手价 ¥{nxt.plan.net_price:.1f}）")
        if len(out) >= 2:
            break

    if not out:
        upper = max(deltas)
        out.append(
            f"预算从 ¥{c.budget:.0f} 加到 ¥{c.budget + upper:.0f}，均衡档的主餐不会变——"
            f"再往上加钱，只会让你多一份小食或甜品，换不到更好的主餐。")
    return out


def _clone(c: Constraints, budget: float) -> Constraints:
    return Constraints(
        budget=budget, kcal_cap=c.kcal_cap, kcal_target=c.kcal_target,
        protein_min=c.protein_min, party_size=c.party_size, avoid=c.avoid,
        need_drink=c.need_drink, want_main=c.want_main, channel=c.channel,
        now=c.now, taste_weight=c.taste_weight,
        allow_unknown_nutrition=c.allow_unknown_nutrition,
    )


def _by_key(tiers: list[Tier], key: str):
    for t in tiers:
        if t.key == key:
            return t
    return None


def eliminated(cands: list[Candidate], tiers: list[Tier], top_n: int = 3) -> list[dict]:
    """被淘汰方案里「最可惜」的几个 —— 按与已选方案的差距从小到大排。"""
    if not cands or not tiers:
        return []
    picked = {t.plan.signature() for t in tiers}
    ref = _by_key(tiers, "balanced") or tiers[0]

    scored: list[tuple[float, Candidate, Tier]] = []
    for cand in cands:
        if cand.plan.signature() in picked:
            continue
        best_t = min(tiers, key=lambda t: abs(t.plan.net_price - cand.plan.net_price))
        gap = (cand.plan.net_price - best_t.plan.net_price) / max(best_t.plan.net_price, 1.0)
        gap += max(0.0, (cand.nutrient.kcal - ref.nutrient.kcal)) / 1000.0
        gap -= 0.3 * max(0.0, cand.taste - ref.score.taste)
        scored.append((gap, cand, best_t))

    scored.sort(key=lambda x: x[0])
    out: list[dict] = []
    seen_main: set[str] = set()
    seen_price: list[float] = []
    for gap, cand, best_t in scored:
        key = cand.plan.main.name
        if key in seen_main:
            continue
        # 价格分档取样，避免三条全是同一价位段的近似方案
        if any(abs(cand.plan.net_price - p) < 0.18 * max(p, 1.0) for p in seen_price):
            continue
        seen_main.add(key)
        seen_price.append(cand.plan.net_price)

        dp = cand.plan.net_price - best_t.plan.net_price
        dk = cand.nutrient.kcal - best_t.nutrient.kcal
        dt = cand.taste - best_t.score.taste
        reasons = []
        if dp > 0.05:
            reasons.append(f"贵 {dp:.1f} 元")
        if dk > 30:
            reasons.append(f"热量高 {dk:.0f} kcal")
        if dt < -0.05:
            reasons.append(f"口味契合低 {abs(dt):.0%}")
        if len(cand.plan.items) < len(best_t.plan.items):
            reasons.append(f"少 {len(best_t.plan.items) - len(cand.plan.items)} 件配餐")
        if not reasons:
            reasons.append("各项指标被同价位方案全面压制")
        out.append({
            "name": cand.plan.main.name,
            "price": cand.plan.net_price,
            "kcal": cand.nutrient.kcal,
            "vs": best_t.label,
            "reasons": reasons,
        })
        if len(out) >= top_n:
            break
    return out


def trace(tier: Tier, fp: dict) -> list[str]:
    """决策溯源：这单为什么成立。给较真的用户看。"""
    lines = [f"主餐「{tier.plan.main.name}」命中画像蛋白偏好 {tier.plan.main.protein}"]
    if tier.plan.coupon:
        lines.append(f"券「{tier.plan.coupon['name']}」匹配到 {tier.plan.main.name or '方案内商品'}，"
                     f"立减 {tier.plan.coupon_saving:.1f} 元")
    lines.append(f"热量来自营养表累加（{tier.nutrient.source}），蛋白 {tier.nutrient.protein:.0f} g")
    lines.append(f"到手价 = 商品价合计 − 券抵扣，未经 LLM 估算")
    return lines
