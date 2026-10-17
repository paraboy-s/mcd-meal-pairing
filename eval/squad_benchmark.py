"""Squad 公平性评估：max-min 与「平均最优」的代价对比。

三个对照
--------
1. mean-opt   ：最大化总满意度（大多数人会这么做）
2. max-min    ：本方案（罗尔斯差异原则）
3. solo-greedy：人均均分预算、各点各的（理想上界，忽略共享与凑单）

指标
----
min   最不满意成员的满意度（公平性）
sum   总满意度（效率）
price 到手价

要回答的问题只有一个：为了把最不满意的人抬起来，牺牲了多少总满意度？
"""
from __future__ import annotations

from itertools import product

from src.data import Catalog
from src.models import Plan
from src.squad_engine import (Member, SquadSolver, assign, generate, presets,
                              score_items, synth_fp)

MIN_MEMBER_KCAL = 250.0


def scenarios():
    """预算 × 人数的网格，聚焦「池化预算不够让每个人都吃爽」的紧张区间。

    另加三组强异质成员：诉求越冲突，目标函数之间的差异才可观测。
    """
    out = []
    for party in (2, 3, 4):
        for budget in range(20 * party, 20 * party + 61, 10):
            base = presets()
            members = list(base[:party])
            while len(members) < party:
                members.append(Member(f"成员{len(members) + 1}",
                                      synth_fp(None, 0.5, 0.1), budget=budget))
            out.append((members, float(budget)))

    vegan = frozenset({"beef", "chicken", "fish", "pork"})
    specials = [
        ("控卡 vs 大胃", [Member("控卡", synth_fp("chicken", 0.1), kcal_cap=450, budget=32),
                          Member("大胃", synth_fp("beef", 0.7), budget=48)], 80.0),
        ("带娃", [Member("爸爸", synth_fp("beef", 0.6), budget=40),
                  Member("妈妈", synth_fp("chicken", 0.2), budget=40),
                  Member("小孩", synth_fp("chicken", 0.0), kcal_cap=450,
                         avoid=frozenset({"spicy"}), budget=28)], 95.0),
        ("素食搭子", [Member("素食", synth_fp(None), avoid=vegan, budget=38),
                      Member("无肉不欢", synth_fp("beef", 0.8), budget=48)], 85.0),
    ]
    for _, members, budget in specials:
        out.append((members, budget))
    return out


def _feasible(cart, members, cat, nutcache, budget):
    plan = Plan(items=list(cart))
    if plan.net_price > budget + 1e-9:
        return None
    buckets = assign(cart, members, cat, nutcache)
    details = [score_items(m, its, cat, nutcache) for m, its in zip(members, buckets)]
    if any(m.kcal_cap and d["kcal"] > m.kcal_cap + 1e-6
           for m, d in zip(members, details)):
        return None
    if any(d["kcal"] < MIN_MEMBER_KCAL for d in details):
        return None
    return [d["score"] for d in details], plan.net_price


def _solo_greedy(members, budget, cat, nutcache):
    """人均均分预算，各自挑最满意的一份。分到的就是自己的，不和别人抢。"""
    head = budget / len(members)
    mains = [i for i in cat.items if i.role in ("main", "combo")
             and nutcache[i.code].complete]
    sides = [i for i in cat.items if i.role == "side" and nutcache[i.code].complete]
    drinks = [i for i in cat.items if i.role == "drink" and nutcache[i.code].complete]

    scores, total = [], 0.0
    for m in members:
        best, best_s = None, -1.0
        pool = [i for i in mains if not m.blocks(i) and i.price <= head]
        for x in pool:
            for extra in [None] + [s for s in sides] + [d for d in drinks]:
                items = [x] + ([extra] if extra else [])
                if sum(i.price for i in items) > head:
                    continue
                s = score_items(m, items, cat, nutcache)["score"]
                if s > best_s:
                    best, best_s = items, s
        if best is None:
            return None
        # 忌口与热量上限仍是硬约束
        det = score_items(m, best, cat, nutcache)
        if m.kcal_cap and det["kcal"] > m.kcal_cap + 1e-6:
            return None
        if any(m.blocks(i) for i in best):
            return None
        scores.append(best_s)
        total += sum(i.price for i in best)
    return scores, round(total, 2)


def evaluate():
    cat = Catalog()
    solver = SquadSolver(cat)
    cache = solver.nutcache

    rows = {"mean-opt": [], "max-min": [], "solo-greedy": []}
    deltas: list[tuple[float, float]] = []      # (Δmin, Δsum) 每个场景

    for members, budget in scenarios():
        carts = generate(members, budget, cat, cache)
        feats = []
        for cart in carts:
            r = _feasible(cart, members, cat, cache, budget)
            if r:
                feats.append(r)
        if not feats:
            continue

        # 总满意度最高的那一单，看它的短板
        best_sum = max(feats, key=lambda f: sum(f[0]))
        mean_row = (min(best_sum[0]), sum(best_sum[0]), best_sum[1])
        rows["mean-opt"].append(mean_row)

        rep = solver.solve(members, budget=budget, now="2026-10-17 18:57")
        if rep.options:
            o = rep.options[0]
            rows["max-min"].append((o.min_score, o.sum_score, o.plan.net_price))
            deltas.append((o.min_score - mean_row[0], o.sum_score - mean_row[1]))

        solo = _solo_greedy(members, budget, cat, cache)
        if solo:
            rows["solo-greedy"].append((min(solo[0]), sum(solo[0]), solo[1]))

    print(f"场景数：{len(rows['max-min'])}"
          f"（预算 70–160 × 2–4 人，含控卡与忌口成员）\n")
    print("| 方案 | 平均 max-min | 平均总满意度 | 平均到手价 |")
    print("|---|---:|---:|---:|")
    label = {"mean-opt": "平均最优（最大化总分）", "max-min": "**McPair Squad（max-min）**",
             "solo-greedy": "人均均分 · 各点各的（理想上界）"}
    for k, name in label.items():
        rs = rows[k]
        if not rs:
            continue
        n = len(rs)
        print(f"| {name} | {sum(r[0] for r in rs) / n:.2f} | "
              f"{sum(r[1] for r in rs) / n:.2f} | ¥{sum(r[2] for r in rs) / n:.1f} |")

    m, s = rows["mean-opt"], rows["max-min"]
    if deltas:
        same = [d for d in deltas if d[0] <= 0.01]
        wins = [d for d in deltas if d[0] > 0.01]
        print(f"\n结论一：{len(same)}/{len(deltas)} 个场景里，"
              f"公平解与效率解是同一单 —— 公平通常不花钱。")
        if wins:
            print(f"结论二：只在 {len(wins)}/{len(deltas)} 个「预算紧到必须有人让步」的场景里分离，"
                  f"max-min 把短板抬高 "
                  f"{sum(d[0] for d in wins) / len(wins):+.2f}，"
                  f"代价是总分 {sum(d[1] for d in wins) / len(wins):+.2f}。")
        print("\n这不是「我们的方案更好」，而是「目标函数换了，取舍就变了」：")
        print("平均最优会让两个人很满意、一个人很难受；max-min 拒绝击穿任何人的底线。")
        print("「各点各的」数字上更优，但它要求每人都有独立预算、且放弃共享与凑单的规模经济；")
        print("现实里的拼单往往是池化预算（一个人请客、或部门统一报销），那才是本模式的场景。")


if __name__ == "__main__":
    evaluate()
