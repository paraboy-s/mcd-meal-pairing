"""把引擎输出整理成对外 JSON。

Skill 的所有消费者（宿主智能体、CLI、评估脚本）都读这一份结构，
避免出现「命令行一套字段、对话里另一套字段」。
"""
from __future__ import annotations

from datetime import datetime

from .models import Constraints, Report


def numeric(value, digits: int = 1):
    """统一数字格式：整数就不带小数点，避免出现 ¥14.0 这种别扭写法。"""
    if value is None:
        return None
    v = round(float(value), digits)
    return int(v) if float(v).is_integer() else v


def constraints_view(c: Constraints) -> dict:
    return {
        "text": c.describe(),
        "budget": c.budget,
        "kcalCap": c.kcal_cap,
        "kcalTarget": c.kcal_target,
        "proteinMin": c.protein_min,
        "avoid": sorted(c.avoid),
        "needDrink": c.need_drink,
        "channel": c.channel,
        "party": c.party_size,
        "now": c.now,
    }


def profile_view(fp: dict) -> dict:
    return {
        "labels": fp.get("labels", []),
        "confidence": numeric(fp.get("confidence"), 2),
        "nOrders": fp.get("n_orders", 0),
        "nItems": fp.get("n_items", 0),
        "surprise": fp.get("surprise"),
        "medianPrice": fp.get("median_price"),
        "priceBand": fp.get("price_band"),
    }


def tier_view(t) -> dict:
    return {
        "key": t.key,
        "label": t.label,
        "items": [{"name": i.name, "code": i.code, "price": numeric(i.price),
                   "role": i.role} for i in t.plan.items],
        "itemCount": len(t.plan.items),
        "netPrice": numeric(t.plan.net_price),
        "origPrice": numeric(t.plan.orig_price),
        "discount": numeric(t.plan.discount),
        "kcal": numeric(t.nutrient.kcal, 0),
        "protein": numeric(t.nutrient.protein, 0),
        "sodium": numeric(t.nutrient.sodium, 0),
        "nutrientSource": t.nutrient.source,
        "coupon": t.plan.coupon["name"] if t.plan.coupon else None,
        "couponSaving": numeric(t.plan.coupon_saving),
        "taste": numeric(getattr(t.score, "taste", None), 2),
        "rationale": t.rationale,
    }


def solo_view(report: Report, cat) -> dict:
    tiers = [tier_view(t) for t in report.tiers]
    prices = [t["netPrice"] for t in tiers]
    # 约束越紧，三档越挤。与其硬凑差异，不如如实说出可行空间有多窄。
    spread = round(max(prices) - min(prices), 1) if prices else 0.0
    return {
        "mode": "solo",
        "ok": bool(tiers),
        "constraints": constraints_view(report.constraints),
        "profile": profile_view(report.fingerprint),
        "tiers": tiers,
        "tierSpread": spread,
        "spreadNote": _spread_note(spread, len(tiers)),
        "eliminated": report.eliminated,
        "counterfactuals": report.counterfactuals,
        "couponNotes": report.coupon_notes,
        "warnings": report.warnings,
        "stats": report.stats,
        "dataStatus": _data_status(cat),
    }


def _spread_note(spread: float, n: int) -> str | None:
    if n < 2:
        return None
    if spread < 5.0:
        return (f"三档只差 ¥{spread:.1f}——不是没算开，而是你的约束把可行空间压窄了，"
                f"再往上加钱也换不到明显更好的组合。")
    return None


def squad_view(rep, cat) -> dict:
    from .squad_engine import _aggregate

    options = [{
        "key": o.key,
        "label": o.label,
        "cart": [{"name": n, "qty": q} for n, q in _aggregate(o.plan.items)],
        "itemCount": len(o.plan.items),
        "netPrice": numeric(o.plan.net_price),
        "origPrice": numeric(o.plan.orig_price),
        "discount": numeric(o.plan.discount),
        "coupon": o.plan.coupon["name"] if o.plan.coupon else None,
        "minScore": numeric(o.min_score, 2),
        "sumScore": numeric(o.sum_score, 2),
        "members": [{"name": v.name, "score": numeric(v.score, 2),
                     "ideal": numeric(getattr(v, "ideal", None), 2),
                     "pay": numeric(v.pay), "kcal": numeric(v.kcal, 0),
                     "items": v.items, "notes": v.notes} for v in o.members],
        "rationale": o.rationale,
    } for o in rep.options]

    return {
        "mode": "squad",
        "ok": bool(options),
        "party": len(rep.members),
        "budget": numeric(rep.budget),
        "members": [{"name": m.name, "budget": numeric(m.budget),
                     "kcalCap": m.kcal_cap, "avoid": sorted(m.avoid)}
                    for m in rep.members],
        "options": options,
        "counterfactuals": rep.counterfactuals,
        "note": rep.note,
        "stats": rep.stats,
        "dataStatus": _data_status(cat),
    }


def _data_status(cat) -> dict:
    return {
        "source": getattr(cat, "source", "fixtures"),
        "storeCode": cat.store.get("storeCode", ""),
        "storeName": cat.store.get("storeName", ""),
        "skuCount": len(cat.items),
        "couponCount": len(cat.coupons),
        "orderCount": len(cat.orders),
        "issues": list(getattr(cat, "issues", [])),
    }


def now_str(value: str | None) -> str:
    if value and value.strip():
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip()[:16], fmt).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
    return datetime.now().strftime("%Y-%m-%d %H:%M")
