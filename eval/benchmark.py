"""基准评估：McPair vs 三个典型基线。

基线代表了榜单上绝大多数项目的做法：
  cheapest_combo   —— 在预算内挑最便宜的套餐（省钱助手范式）
  best_discount    —— 挑让利最大的套餐（优惠券助手范式）
  first_match      —— 菜单顺序第一个满足预算的套餐（朴素推荐范式）

运行： python -m eval.benchmark
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data import Catalog
from src.models import Constraints
from src.pipeline import run
from src.taste_engine import build_fingerprint, taste_match
from src.solver_engine import FRIED_KEYWORDS

BUDGETS = [15, 20, 25, 30, 35, 40, 45, 50]
KCAPS = [None, 500, 600, 700, 800]
AVOIDS = [frozenset(), frozenset({"spicy"}), frozenset({"beef"}), frozenset({"fried"})]
NOW = "2026-10-17 18:57"


def scenarios() -> list[Constraints]:
    out = []
    i = 0
    for b in BUDGETS:
        for k in KCAPS:
            c = Constraints(budget=b, kcal_cap=k, avoid=AVOIDS[i % len(AVOIDS)],
                            need_drink=(i % 3 == 0), now=NOW)
            if c.avoid and any(x in c.avoid for x in ("fried", "beef", "spicy")):
                pass
            out.append(c)
            i += 1
    return out


class Baseline:
    name = "baseline"

    def __init__(self, cat: Catalog):
        self.cat = cat

    def pick(self, c: Constraints):
        combos = [i for i in self.cat.by_role("combo")]
        pool = []
        for k in combos:
            nut = self.cat.nutrient(k)
            if not nut.complete:
                continue
            if k.price > c.budget:
                continue
            if c.kcal_cap and nut.kcal > c.kcal_cap:
                continue
            if k.protein in c.avoid:
                continue
            if "spicy" in c.avoid and k.spicy:
                continue
            if "fried" in c.avoid and any(x in k.name for x in FRIED_KEYWORDS):
                continue
            if c.need_drink and not k.compose:
                continue
            pool.append((k, nut))
        if not pool:
            return None
        return self.rank(pool)

    def rank(self, pool):
        raise NotImplementedError


class CheapestCombo(Baseline):
    name = "cheapest_combo"

    def rank(self, pool):
        return sorted(pool, key=lambda x: x[0].price)[0]


class BestDiscount(Baseline):
    name = "best_discount"

    def rank(self, pool):
        return sorted(pool, key=lambda x: x[0].discount, reverse=True)[0]


class FirstMatch(Baseline):
    name = "first_match"

    def rank(self, pool):
        return pool[0]


def _main_taste(items, fp) -> float:
    """只比主餐口味契合——基线只有套餐，这样才是同口径比较。"""
    mains = [i for i in items if i.role in ("main", "combo")] or list(items)
    return sum(taste_match(i, fp) for i in mains) / len(mains)


def evaluate(now: str = NOW, title: str = "") -> dict:
    cat = Catalog()
    fp = build_fingerprint(cat.orders)
    cases = [replace(c, now=now) for c in scenarios()]
    baselines = [CheapestCombo(cat), BestDiscount(cat), FirstMatch(cat)]
    rows = {b.name: {"n": 0, "ok": 0, "price": 0.0, "taste": 0.0, "kcal_fit": 0.0, "coupon": 0}
            for b in baselines}
    rows["mcpair"] = {"n": 0, "ok": 0, "price": 0.0, "taste": 0.0, "kcal_fit": 0.0, "coupon": 0}

    for c in cases:
        base_target = c.kcal_target or (c.kcal_cap * 0.85 if c.kcal_cap else 800.0)
        tol = max(220.0, base_target * 0.35)

        for b in baselines:
            hit = b.pick(c)
            rows[b.name]["n"] += 1
            if not hit:
                continue
            item, nut = hit
            rows[b.name]["ok"] += 1
            rows[b.name]["price"] += item.price
            rows[b.name]["taste"] += taste_match(item, fp)
            rows[b.name]["kcal_fit"] += max(0.0, 1.0 - abs(nut.kcal - base_target) / tol)

        rep = run(c, cat)
        rows["mcpair"]["n"] += 1
        if rep.tiers:
            t = rep.tiers[1] if len(rep.tiers) > 1 else rep.tiers[0]
            rows["mcpair"]["ok"] += 1
            rows["mcpair"]["price"] += t.plan.net_price
            rows["mcpair"]["taste"] += _main_taste(t.plan.items, fp)
            rows["mcpair"]["kcal_fit"] += max(0.0, 1.0 - abs(t.nutrient.kcal - base_target) / tol)
            rows["mcpair"]["coupon"] += 1 if any(t.plan.coupon for t in rep.tiers) else 0

    if title:
        print(f"\n### {title}\n")
    print(f"场景数：{len(cases)}（预算 {BUDGETS} × 热量上限 {KCAPS}，混入忌口与饮品约束）\n")
    print("| 方案 | 可行率 | 平均客单价 | 主餐口味契合 | 热量贴合 | 用上券 |")
    print("|---|---:|---:|---:|---:|---:|")
    for name, r in rows.items():
        n = max(r["ok"], 1)
        label = {"mcpair": "**McPair（本方案）**", "cheapest_combo": "最便宜套餐",
                 "best_discount": "让利最大", "first_match": "菜单首个"}.get(name, name)
        print(f"| {label} | {r['ok'] / r['n']:.0%} | ¥{r['price'] / n:.1f} | "
              f"{r['taste'] / n:.2f} | {r['kcal_fit'] / n:.2f} | {r['coupon']} |")
    return rows


def note():
    print("\n注：基线只看单个套餐，无法组合小食与饮品，因此热量贴合天然吃亏；")
    print("    这不是刻意设弱，而是「只做套餐推荐」这个范式的天花板。")


def ablation(cases: int = 40):
    """四组消融：逐一关掉一个引擎，看指标怎么掉。

    这是「作品」与「研究」的分界线——它证明每个引擎不是装饰。
    """
    from src import coupon_engine as CE
    from src.solver_engine import Solver

    cat = Catalog()
    fp = build_fingerprint(cat.orders)
    # 用「周一午间」这个券可用的时间点，否则券引擎关不关都没差别
    cs = [replace(c, now="2026-10-19 12:30") for c in scenarios()]
    solvers = Solver(cat)

    neutral = {"ratio": {}, "dims": {"spice_tolerance": 0, "combo_habit": 0,
                                     "sweet_tendency": 0, "side_habit": 0,
                                     "fried_tendency": 0, "protein_chicken": 0,
                                     "protein_beef": 0, "protein_fish": 0,
                                     "protein_pork": 0}}

    def measure(tag: str, use_fp: dict, no_coupon: bool, no_nutrition: bool):
        agg = {"n": 0, "price": 0.0, "taste": 0.0, "kcal": 0.0, "coupon": 0}
        for c in cs:
            c2 = replace(c, kcal_cap=None, kcal_target=None) if no_nutrition else c
            base_target = 800.0 if no_nutrition else (
                c2.kcal_target or (c2.kcal_cap * 0.85 if c2.kcal_cap else 800.0))
            tol = max(220.0, base_target * 0.35)

            orig = CE.best_for
            if no_coupon:
                CE.best_for = lambda plan, *a, **k: (None, 0.0)
            try:
                res = solvers.solve(c2, use_fp)
            finally:
                CE.best_for = orig

            tiers = res["tiers"]
            if not tiers:
                continue
            t = tiers[1] if len(tiers) > 1 else tiers[0]
            agg["n"] += 1
            agg["price"] += t.plan.net_price
            agg["taste"] += _main_taste(t.plan.items, use_fp)
            agg["kcal"] += max(0.0, 1.0 - abs(t.nutrient.kcal - base_target) / tol)
            agg["coupon"] += 1 if any(x.plan.coupon for x in tiers) else 0
        n = max(agg["n"], 1)
        return (tag, agg["n"], agg["price"] / n, agg["taste"] / n,
                agg["kcal"] / n, agg["coupon"])

    rows = [
        measure("完整", fp, False, False),
        measure("关闭品味引擎（画像置中性）", neutral, False, False),
        measure("关闭券引擎（券一律不匹配）", fp, True, False),
        measure("关闭营养约束（不看热量上限）", fp, False, True),
    ]

    print("\n## 消融实验\n")
    print(f"在 {len(cs)} 个场景上取均衡档均值，每次只关掉一个引擎。\n")
    print("| 配置 | 可行场景 | 平均到手价 | 主餐口味契合 | 热量贴合 | 用上券 |")
    print("|---|---:|---:|---:|---:|---:|")
    for tag, n, price, taste, kcal, coupon in rows:
        star = "**" if tag == "完整" else ""
        print(f"| {star}{tag}{star} | {n} | ¥{price:.1f} | {taste:.2f} | "
              f"{kcal:.2f} | {coupon} |")

    base = rows[0]
    for tag, n, price, taste, kcal, coupon in rows[1:]:
        drops = []
        if base[3] - taste > 0.02:
            drops.append(f"口味契合 {base[3]:.2f} → {taste:.2f}")
        if base[4] - kcal > 0.02:
            drops.append(f"热量贴合 {base[4]:.2f} → {kcal:.2f}")
        if base[5] - coupon >= 3:
            drops.append(f"用上券 {base[5]} → {coupon} 个场景")
        if drops:
            print(f"\n- {tag}：{'；'.join(drops)}")


if __name__ == "__main__":
    evaluate("2026-10-17 18:57", "场景 A：周六晚，券全部不可用")
    evaluate("2026-10-19 12:30", "场景 B：周一午间，券可用")
    note()
    ablation()
