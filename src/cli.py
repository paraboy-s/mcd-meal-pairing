"""命令行入口。

用法示例：
  python -m src.cli --budget 40
  python -m src.cli --budget 40 --kcal-cap 700 --avoid spicy
  python -m src.cli --budget 60 --protein-min 30 --layer 3
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime

from .data import Catalog
from .models import Constraints
from .pipeline import run
from .render import render


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("mcpair", description="McPair · 麦麦配餐师")
    ap.add_argument("--budget", type=float, default=None, help="预算上限（元）")
    ap.add_argument("--kcal-cap", type=float, default=None, help="热量上限（kcal）")
    ap.add_argument("--kcal-target", type=float, default=None, help="目标热量（kcal）")
    ap.add_argument("--protein-min", type=float, default=0.0, help="蛋白下限（g）")
    ap.add_argument("--party", type=int, default=1, help="人数")
    ap.add_argument("--avoid", nargs="*", default=[], help="忌口：beef chicken fish pork spicy fried dessert")
    ap.add_argument("--need-drink", action="store_true", help="方案必须含饮品")
    ap.add_argument("--channel", default="到店", choices=["到店", "外送"])
    ap.add_argument("--now", default=datetime.now().strftime("%Y-%m-%d %H:%M"), help="模拟时间")
    ap.add_argument("--layer", type=int, default=3, choices=[1, 2, 3], help="披露层级")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--live", action="store_true",
                    help="调用 calculate-price 校验真实到手价（需 MCD_MCP_TOKEN）")
    ap.add_argument("--squad", action="store_true", help="多人拼单模式（max-min 偏好聚合）")
    ap.add_argument("--member", action="append", default=[], metavar="SPEC",
                    help="拼单成员，形如 \"小明:kcal=600,avoid=spicy,taste=chicken,budget=34\"，可重复")
    args = ap.parse_args(argv)

    if args.squad:
        return _squad(args)

    budget = 40.0 if args.budget is None else args.budget
    c = Constraints(
        budget=budget,
        kcal_cap=args.kcal_cap,
        kcal_target=args.kcal_target,
        protein_min=args.protein_min,
        party_size=args.party,
        avoid=frozenset(args.avoid),
        need_drink=args.need_drink,
        channel=args.channel,
        now=args.now,
    )

    cat = Catalog()
    report = run(c, cat)

    if args.json:
        payload = {
            "constraints": c.describe(),
            "fingerprint": report.fingerprint,
            "tiers": [{
                "key": t.key, "label": t.label,
                "items": [i.name for i in t.plan.items],
                "netPrice": t.plan.net_price,
                "origPrice": t.plan.orig_price,
                "kcal": t.nutrient.kcal, "protein": t.nutrient.protein,
                "coupon": t.plan.coupon["name"] if t.plan.coupon else None,
                "couponSaving": t.plan.coupon_saving,
                "rationale": t.rationale,
            } for t in report.tiers],
            "eliminated": report.eliminated,
            "counterfactuals": report.counterfactuals,
            "couponNotes": report.coupon_notes,
            "stats": report.stats,
            "warnings": report.warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report, args.layer))

    if args.live:
        _verify_live(report, cat)
    return 0


def _squad(args) -> int:
    """多人拼单：max-min 偏好聚合。"""
    from .squad_engine import (Member, SquadSolver, parse_member, presets,
                               render_squad, synth_fp, _aggregate)

    if args.member:
        members = [parse_member(s) for s in args.member]
    else:
        members = presets()
        while len(members) < max(2, args.party):
            members.append(Member(f"成员{len(members) + 1}",
                                  synth_fp(None, 0.5, 0.1), budget=40.0))

    budget = 100.0 if args.budget is None else args.budget
    cat = Catalog()
    rep = SquadSolver(cat).solve(members, budget=budget, now=args.now,
                                 channel=args.channel)

    if args.json:
        payload = {
            "budget": rep.budget,
            "members": [{"name": m.name, "kcalCap": m.kcal_cap,
                         "budget": m.budget, "avoid": sorted(m.avoid)}
                        for m in rep.members],
            "stats": rep.stats,
            "options": [{
                "key": o.key, "label": o.label,
                "cart": [{"name": n, "qty": q} for n, q in _aggregate(o.plan.items)],
                "netPrice": o.plan.net_price,
                "coupon": o.plan.coupon["name"] if o.plan.coupon else None,
                "minScore": o.min_score, "sumScore": o.sum_score,
                "members": [{"name": v.name, "score": v.score, "ideal": v.ideal,
                             "pay": v.pay, "kcal": v.kcal,
                             "items": v.items, "notes": v.notes}
                            for v in o.members],
                "rationale": o.rationale,
            } for o in rep.options],
            "counterfactuals": rep.counterfactuals,
            "note": rep.note,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_squad(rep, args.layer))
    return 0


def _verify_live(report, cat) -> None:
    """价格漂移校验：本地菜单价 vs calculate-price 实时返回值。"""
    from .mcp_client import McpClient, McpError

    client = McpClient()
    if not client.available:
        print("\n[live] 未检测到 MCD_MCP_TOKEN，已跳过实时校验。")
        return
    try:
        client.connect()
    except McpError as e:
        print(f"\n[live] 连接失败：{e}")
        return

    print("\n[live] calculate-price 实时校验")
    for t in report.tiers:
        items = [{"productCode": i.code, "quantity": 1} for i in t.plan.items]
        try:
            res = client.calculate_price(cat.store["storeCode"], items)
        except McpError as e:
            print(f"  {t.label}：调用失败 {e}")
            continue
        total = _extract_total(res)
        if total is None:
            print(f"  {t.label}：接口未返回总价，原始响应已忽略")
            continue
        diff = round(total - t.plan.price, 2)
        flag = "一致" if abs(diff) < 0.01 else f"偏差 {diff:+.2f} 元（以实时价为准）"
        print(f"  {t.label}：本地 ¥{t.plan.price:.2f} → 实时 ¥{total:.2f}，{flag}")


def _extract_total(res):
    """接口金额单位是分，字段名随场景略有差异，这里做保守解析。"""
    if not isinstance(res, dict):
        return None
    data = res.get("data", res)
    for key in ("totalAmount", "payAmount", "actualAmount", "totalPrice"):
        if isinstance(data, dict) and data.get(key) is not None:
            try:
                v = float(data[key])
            except (TypeError, ValueError):
                continue
            return v / 100 if v > 1000 else v
    return None


if __name__ == "__main__":
    raise SystemExit(main())
