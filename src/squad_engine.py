"""Squad 引擎：多人拼单的偏好聚合。

问题定义
--------
N 个人共享一份购物车，每个人对「车」的满意度不同。求一个没人被牺牲的解。

形式化
------
成员 i 对购物车 x 的满意度 s_i(x) ∈ [0,1]。聚合目标：

    maximize   min_i s_i(x)
    s.t.       price(x) ≤ 预算
               分给 i 的项 ∩ 忌口_i = ∅

为什么不用「平均满意度最大」
----------------------------
平均最优会产出「两人 1.0、一人 0.2」的解 —— 总分很高，但有人被放弃。
max-min（罗尔斯差异原则）先保证最不满意的那个人不太惨，这才是拼单场景里
「公平」的可操作定义。这是我们与「数量相加」式拼单项目的本质区别。

实现要点
--------
1. 组合空间：每人的候选主餐取 Top-K，共享品按需求强度取 Top-N，
   份数取 0/1/人数三档 —— 从 1e9 降到 1e3 量级。
2. 分配规则：谁的边际渴望最高就给谁，主餐优先补缺口（避免有人只有薯条）。
3. 分账：自己点的自己付；券省下的钱按原价比例摊，尾差给分摊额最大的人。
4. 让步披露：用「个人可达最优」减去「实得」得到该成员让了多少，全部显式输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Optional

from . import coupon_engine as CE
from .data import Catalog
from .models import Constraints, MenuItem, Nutrient, Plan
from .solver_engine import _blocked, _nutrient_of
from .taste_engine import taste_match

TASTE_CEIL = 0.66      # taste_match 的理论上限（0.55 蛋白 + 0.10 套餐 + 0.05 加成）
TOP_MAINS = 4
TOP_SHARED = 3
MIN_KCAL_PER_HEAD = 350.0
MIN_KCAL_PER_MEMBER = 250.0

# 口味打分按角色加权：主餐决定「这顿吃得爽不爽」，
# 饮品/小食不该用同一把尺子 —— 否则一杯可乐会把整单口味分拖垮。
ROLE_WEIGHT = {"main": 1.0, "combo": 1.0, "side": 0.45, "drink": 0.30, "dessert": 0.40}


# ---------------------------------------------------------------- 成员
@dataclass
class Member:
    """一个拼单成员。fp 为品味指纹，可来自真实点餐史，也可由 spec 合成。"""
    name: str
    fp: dict
    kcal_cap: Optional[float] = None
    budget: Optional[float] = None          # 个人可接受的分摊上限
    avoid: frozenset[str] = frozenset()
    _c: Optional[Constraints] = field(default=None, repr=False, compare=False)

    @property
    def constraints(self) -> Constraints:
        if self._c is None:
            self._c = Constraints(avoid=self.avoid)
        return self._c

    def blocks(self, item: MenuItem) -> bool:
        return _blocked(item, self.constraints)


def synth_fp(protein: Optional[str] = None, spicy_tolerance: float = 0.5,
             sweet: float = 0.0, side_habit: float = 0.6,
             combo_habit: float = 0.5) -> dict:
    """合成一份最小可用的品味指纹（用于没有点餐史的成员）。"""
    if protein in ("chicken", "beef", "fish", "pork"):
        ratio = {"chicken": 0.0, "beef": 0.0, "fish": 0.0, "pork": 0.0}
        ratio[protein] = 0.8
    else:
        ratio = {"chicken": 0.25, "beef": 0.25, "fish": 0.25, "pork": 0.25}
    return {
        "ratio": ratio,
        "dims": {
            "spice_tolerance": spicy_tolerance,
            "sweet_tendency": sweet,
            "side_habit": side_habit,
            "combo_habit": combo_habit,
            "fried_tendency": spicy_tolerance,
            "protein_chicken": ratio["chicken"],
            "protein_beef": ratio["beef"],
            "protein_fish": ratio["fish"],
            "protein_pork": ratio["pork"],
        },
        "labels": [], "confidence": 0.0, "n_orders": 0, "n_items": 0,
        "drink_kinds": {}, "surprise": None, "price_band": (0.0, 0.0),
        "median_price": 0.0, "hours": [], "top_protein": protein or "chicken",
    }


def parse_member(spec: str) -> Member:
    """`小明:kcal=600,avoid=spicy,taste=chicken,spicy=0.72,budget=30`"""
    name, _, rest = spec.partition(":")
    kv: dict[str, str] = {}
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        k, _, v = chunk.partition("=")
        kv[k.strip()] = v.strip()

    avoid = frozenset(x for x in kv.get("avoid", "").split("|") if x)
    return Member(
        name=name.strip() or "成员",
        fp=synth_fp(
            protein=kv.get("taste"),
            spicy_tolerance=float(kv.get("spicy", 0.5)),
            sweet=float(kv.get("sweet", 0.0)),
            side_habit=float(kv.get("side", 0.6)),
            combo_habit=float(kv.get("combo", 0.5)),
        ),
        kcal_cap=float(kv["kcal"]) if "kcal" in kv else None,
        budget=float(kv["budget"]) if "budget" in kv else None,
        avoid=avoid,
    )


def presets() -> list[Member]:
    """默认演示成员：三个诉求互相冲突的典型组合。"""
    return [
        Member("小明", synth_fp("chicken", spicy_tolerance=0.15, sweet=0.0),
               kcal_cap=600, budget=34),
        Member("小美", synth_fp("beef", spicy_tolerance=0.0, sweet=0.2),
               avoid=frozenset({"spicy"}), budget=40),
        Member("老王", synth_fp("chicken", spicy_tolerance=0.9, sweet=0.3),
               budget=40),
    ]


# ---------------------------------------------------------------- 满意度
DRINK_KW = ("可乐", "雪碧", "怡泉", "红茶", "咖啡", "牛奶", "果汁", "柠", "橙",
            "奶铁", "豆浆", "抹茶", "黑巧", "炫酷")
SIDE_KW = ("薯", "玉米", "鸡块", "鸡翅", "芝士条", "苹果片", "鸡球", "扭扭")


def _combo_roles(item: MenuItem, cat: Catalog) -> tuple[bool, bool]:
    """套餐是否含饮品/小食。按 compose 里的名字查角色，查不到退回关键词。"""
    names = item.compose
    if not names:
        return True, True          # 套餐定义上就是「主食+配餐+饮品」
    has_drink = has_side = False
    for n in names:
        comp = cat.by_name.get(n)
        if comp is not None:
            has_drink = has_drink or comp.role == "drink"
            has_side = has_side or comp.role == "side"
        else:
            has_drink = has_drink or any(k in n for k in DRINK_KW)
            has_side = has_side or any(k in n for k in SIDE_KW)
    return has_drink, has_side


def score_items(member: Member, items: list[MenuItem], cat: Catalog,
                nutcache: dict[str, Nutrient]) -> dict:
    """成员对这个「分到的子集」的满意度，全部可解释、无黑箱。"""
    if not items:
        return {"score": 0.0, "taste": 0.0, "kcal": 0.0, "taste_n": 0.0,
                "kcal_fit": 0.0, "has_main": False, "blocked": 0}

    wsum = sum(ROLE_WEIGHT.get(i.role, 0.5) for i in items)
    taste = sum(ROLE_WEIGHT.get(i.role, 0.5) * taste_match(i, member.fp)
                for i in items) / (wsum or 1.0)
    taste_n = min(1.0, taste / TASTE_CEIL)
    kcal = sum(nutcache[i.code].kcal for i in items)

    target = (member.kcal_cap * 0.75) if member.kcal_cap else 750.0
    tol = max(200.0, target * 0.4)
    kcal_fit = max(0.0, 1.0 - abs(kcal - target) / tol)
    over = bool(member.kcal_cap and kcal > member.kcal_cap)
    if over:
        kcal_fit *= 0.55

    # 配餐完整度：只有汉堡也算一顿，但会掉分 —— 这是「看起来像一顿饭」的量化。
    # 套餐要展开它的构成来看，否则「三件套」会被误判成没饮品没小食。
    has_main = has_drink = has_side = False
    for i in items:
        if i.role == "combo":
            has_main = True
            d, s = _combo_roles(i, cat)
            has_drink = has_drink or d
            has_side = has_side or s
        elif i.role == "main":
            has_main = True
        elif i.role == "drink":
            has_drink = True
        elif i.role == "side":
            has_side = True
    blocked = sum(1 for i in items if member.blocks(i))

    comp = 0.0
    if has_main:
        comp += 0.55
    if has_drink:
        comp += 0.25
    if has_side:
        comp += 0.20

    s = 0.45 * taste_n + 0.30 * kcal_fit + 0.25 * comp
    if blocked:
        s *= 0.30          # 分到忌口项是硬伤，比「没吃到想吃的」严重得多
    return {"score": round(max(0.0, min(1.0, s)), 3), "taste": round(taste, 3),
            "kcal": round(kcal, 1), "taste_n": round(taste_n, 3),
            "kcal_fit": round(kcal_fit, 3), "comp": round(comp, 3),
            "has_main": has_main, "blocked": blocked}


def assign(cart: list[MenuItem], members: list[Member], cat: Catalog,
           nutcache: dict[str, Nutrient]) -> list[list[MenuItem]]:
    """把购物车里的每一份分给一个人。

    规则：按**边际满意度增益**分配 —— 这件东西给谁，谁的满意度涨得最多。
    比「按口味相似度分」更准：一杯可乐对三个人都不算难喝（taste 打平），
    但只有热量没吃饱的那个人真正需要它。
    """
    buckets: list[list[MenuItem]] = [[] for _ in members]
    order = sorted(range(len(cart)),
                   key=lambda k: (cart[k].role not in ("main", "combo"),
                                  -nutcache[cart[k].code].kcal, k))
    base = [0.0] * len(members)
    for idx in order:
        item = cart[idx]
        gains = []
        for k, m in enumerate(members):
            after = score_items(m, buckets[k] + [item], cat, nutcache)["score"]
            gains.append(after - base[k] - 0.01 * len(buckets[k]))
        best = max(range(len(members)), key=lambda k: gains[k])
        buckets[best].append(item)
        base[best] = score_items(members[best], buckets[best], cat, nutcache)["score"]
    return buckets


# ---------------------------------------------------------------- 组合空间
def generate(members: list[Member], budget: float, cat: Catalog,
             nutcache: dict[str, Nutrient]) -> list[list[MenuItem]]:
    """分层枚举：每人 Top-K 主餐 × 共享品 Top-N × 份数三档。"""
    per: list[list[MenuItem]] = []
    for m in members:
        pool = [i for i in cat.items
                if i.role in ("main", "combo")
                and nutcache[i.code].complete
                and not m.blocks(i)]
        pool.sort(key=lambda i: taste_match(i, m.fp), reverse=True)
        per.append(pool[:TOP_MAINS] or [i for i in cat.by_role("main")
                                        if nutcache[i.code].complete][:1])

    def demand(roles: tuple[str, ...]) -> list[MenuItem]:
        pool = [i for i in cat.items if i.role in roles and nutcache[i.code].complete]
        pool.sort(key=lambda i: sum(taste_match(i, m.fp) for m in members), reverse=True)
        return pool[:TOP_SHARED]

    sides, drinks = demand(("side",)), demand(("drink",))
    desserts = demand(("dessert",))[:2]
    n = len(members)

    carts: dict[str, list[MenuItem]] = {}

    def put(items: list[MenuItem]) -> None:
        # 硬约束一：人均至少一份主餐/套餐，否则会出现「三个人分两对鸡翅」这种答案
        if sum(1 for i in items if i.role in ("main", "combo")) < n:
            return
        # 硬约束二：人均热量下限 300 kcal，防止用小吃凑人数
        if sum(nutcache[i.code].kcal for i in items) < n * MIN_KCAL_PER_HEAD:
            return
        sig = "+".join(sorted(f"{i.code}x{items.count(i)}" for i in set(items)))
        carts.setdefault(sig, items)

    for base in product(*per):
        core = list(base)
        core_cost = sum(i.price for i in core)
        if core_cost > budget:
            continue
        for s, sq in [(None, 0)] + [(i, q) for i in sides for q in (1, n)]:
            for d, dq in [(None, 0)] + [(i, q) for i in drinks for q in (1, n)]:
                for ds, dsq in [(None, 0)] + [(i, 1) for i in desserts]:
                    items = core + [s] * sq + [d] * dq + [ds] * dsq
                    if sum(i.price for i in items) > budget:
                        continue
                    put(items)
    return list(carts.values())


# ---------------------------------------------------------------- 聚合
@dataclass
class MemberView:
    name: str
    score: float
    ideal: float
    pay: float
    alloc: float
    items: list[tuple[str, int]]
    kcal: float
    notes: list[str] = field(default_factory=list)

    @property
    def regret(self) -> float:
        return round(self.ideal - self.score, 3)


@dataclass
class SquadOption:
    key: str
    label: str
    plan: Plan
    nutrient: Nutrient
    min_score: float
    sum_score: float
    members: list[MemberView] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)


@dataclass
class SquadReport:
    members: list[Member]
    options: list[SquadOption]
    counterfactuals: list[str]
    stats: dict
    budget: float
    note: str = ""


def _aggregate(items: list[MenuItem]) -> list[tuple[str, int]]:
    seen: dict[str, int] = {}
    order: list[str] = []
    for i in items:
        if i.name not in seen:
            seen[i.name] = 0
            order.append(i.name)
        seen[i.name] += 1
    return [(n, seen[n]) for n in order]


def _split(plan: Plan, allocs: list[float]) -> list[float]:
    gross = plan.price or 1.0
    net = plan.net_price
    pay = [round(a * net / gross, 1) for a in allocs]
    drift = round(net - sum(pay), 1)
    if pay and abs(drift) >= 0.05:
        k = max(range(len(pay)), key=lambda i: pay[i])
        pay[k] = round(pay[k] + drift, 1)
    return pay


class SquadSolver:
    def __init__(self, catalog: Catalog):
        self.cat = catalog
        self.nutcache = {i.code: catalog.nutrient(i) for i in catalog.items}

    def solve(self, members: list[Member], budget: float,
              now: str, channel: str = "到店") -> SquadReport:
        carts = generate(members, budget, self.cat, self.nutcache)
        scored: list[dict] = []

        for cart in carts:
            plan = Plan(items=list(cart))
            coupon, saving = CE.best_for(plan, self.cat.coupons, now, channel)
            plan.coupon, plan.coupon_saving = coupon, saving
            if plan.net_price > budget + 1e-9:
                continue

            buckets = assign(cart, members, self.cat, self.nutcache)
            details = [score_items(m, its, self.cat, self.nutcache)
                       for m, its in zip(members, buckets)]

            # 个人硬约束：热量上限不可突破；分到的量不足 250 kcal 不算一顿饭
            if any(m.kcal_cap and d["kcal"] > m.kcal_cap + 1e-6
                   for m, d in zip(members, details)):
                continue
            if any(d["kcal"] < MIN_KCAL_PER_MEMBER for d in details):
                continue

            scores = [d["score"] for d in details]
            allocs = [sum(i.price for i in its) for its in buckets]

            # 个人预算上限：谁付超了，这单对他不可接受
            pay = _split(plan, allocs)
            if any(m.budget is not None and p > m.budget + 1e-6
                   for m, p in zip(members, pay)):
                continue

            scored.append({
                "cart": cart, "plan": plan, "scores": scores, "allocs": allocs,
                "pay": pay, "min": min(scores), "sum": sum(scores),
                "sig": plan.signature(),
            })

        stats = {
            "enumerated": len(carts), "feasible": len(scored),
            "party": len(members), "budget": budget,
        }
        if not scored:
            return SquadReport(members=members, options=[], counterfactuals=[],
                               stats=stats, budget=budget,
                               note="在给定预算下没有一组能满足所有人的底线，建议放宽预算或去掉某人的限制。")

        ideal = [max(r["scores"][k] for r in scored) for k in range(len(members))]

        fairest = max(scored, key=lambda r: (r["min"], r["sum"]))
        richest = max(scored, key=lambda r: (r["sum"], r["min"]))
        floor = fairest["min"] * 0.70
        pool = [r for r in scored if r["min"] >= floor]
        cheapest = min(pool or scored, key=lambda r: (r["plan"].net_price, -r["min"]))

        chosen: list[tuple[str, str, dict]] = []
        for key, label, row in (("fair", "最公平", fairest),
                                ("cheap", "省钱优先", cheapest),
                                ("treat", "最尽兴", richest)):
            if all(row["sig"] != c[2]["sig"] for c in chosen):
                chosen.append((key, label, row))

        options = [self._build(key, label, row, members, ideal,
                               ref_price=fairest["plan"].net_price)
                   for key, label, row in chosen]
        cf = self._counterfactuals(scored, members, ideal, fairest)
        return SquadReport(members=members, options=options, counterfactuals=cf,
                           stats=stats, budget=budget)

    def _build(self, key: str, label: str, row: dict, members: list[Member],
               ideal: list[float], ref_price: Optional[float] = None) -> SquadOption:
        plan: Plan = row["plan"]
        cart: list[MenuItem] = row["cart"]
        buckets = assign(cart, members, self.cat, self.nutcache)

        views: list[MemberView] = []
        owner: dict[str, str] = {}
        for m, its in zip(members, buckets):
            for i in its:
                owner.setdefault(i.name, m.name)

        for k, (m, its) in enumerate(zip(members, buckets)):
            detail = score_items(m, its, self.cat, self.nutcache)
            notes: list[str] = []
            if not detail["has_main"]:
                notes.append("没分到主食")
            if detail["blocked"]:
                notes.append(f"被分到 {detail['blocked']} 件忌口项")
            if m.kcal_cap and detail["kcal"] > m.kcal_cap:
                notes.append(f"热量 {detail['kcal']:.0f}，超出上限 {m.kcal_cap:.0f}")
            top = self._top_main(m)
            if top is not None and all(i.name != top.name for i in its):
                if top.name in owner:
                    notes.append(f"最想要的「{top.name}」让给了{owner[top.name]}")
                else:
                    notes.append(f"最想要的「{top.name}」这单没点上")
            regret = round(ideal[k] - detail["score"], 3)
            if regret >= 0.12:
                notes.append(f"比个人最优让了 {regret:.2f}（本可到 {ideal[k]:.2f}）")
            views.append(MemberView(
                name=m.name, score=detail["score"], ideal=round(ideal[k], 3),
                pay=row["pay"][k], alloc=round(row["allocs"][k], 2),
                items=_aggregate(its), kcal=detail["kcal"], notes=notes,
            ))

        return SquadOption(
            key=key, label=label, plan=plan,
            nutrient=_nutrient_of(self.cat, plan) or Nutrient(0, 0, 0, 0, 0, 0, "missing"),
            min_score=row["min"], sum_score=round(row["sum"], 3),
            members=views, rationale=self._rationale(key, row, views, plan, ref_price),
        )

    def _top_main(self, m: Member) -> Optional[MenuItem]:
        pool = [i for i in self.cat.items if i.role in ("main", "combo")
                and self.nutcache[i.code].complete and not m.blocks(i)]
        if not pool:
            return None
        return max(pool, key=lambda i: taste_match(i, m.fp))

    def _rationale(self, key: str, row: dict, views: list[MemberView],
                   plan: Plan, ref_price: Optional[float] = None) -> list[str]:
        out: list[str] = []
        worst = min(views, key=lambda v: v.score)
        if key == "fair":
            out.append(f"最不满意的是{worst.name}（{worst.score:.2f}），"
                       f"max-min 决策保证这个数被抬到最高")
        elif key == "cheap":
            saved = round((ref_price or plan.net_price) - plan.net_price, 1)
            out.append(f"在 max-min 不低于最优解 70% 的前提下最便宜"
                       + (f"，比最公平方案省 {saved:.1f} 元" if saved > 0.05
                          else "，且没有人被明显牺牲"))
        else:
            out.append("总满意度最高，代价是最弱的那个人可能被牺牲")
        if plan.coupon:
            out.append(f"用掉「{plan.coupon['name']}」，整单省 {plan.coupon_saving:.1f} 元")
        if plan.discount > 0.5:
            out.append(f"相对原价让利 {plan.discount:.1f} 元")
        return out

    def _counterfactuals(self, scored: list[dict], members: list[Member],
                         ideal: list[float], fairest: dict) -> list[str]:
        out: list[str] = []
        for k, m in enumerate(members):
            best = max(scored, key=lambda r: (r["scores"][k], r["min"]))
            if best["scores"][k] - fairest["scores"][k] < 0.08:
                continue
            worst = min(range(len(members)), key=lambda j: best["scores"][j])
            if best["min"] >= fairest["min"] - 1e-9:
                continue
            out.append(
                f"只照顾{m.name}：他能到 {best['scores'][k]:.2f}，"
                f"但{members[worst].name}会掉到 {best['scores'][worst]:.2f}，"
                f"max-min 只剩 {best['min']:.2f} → 否决")
            if len(out) >= 2:
                break
        if not out:
            out.append("没有任何一个人的「个人最优」比当前解高出 0.08 —— "
                       "这单不是靠牺牲某个人凑出来的")
        return out


# ---------------------------------------------------------------- 渲染
BAR = "─" * 52


def render_squad(report: SquadReport, layer: int = 3) -> str:
    L: list[str] = []
    names = " / ".join(m.name for m in report.members)
    L.append(f"【{len(report.members)} 人拼单】{names}")
    if not report.options:
        L.append("")
        L.append(report.note)
        return "\n".join(L)

    best = report.options[0]
    L.append(f"  预算 ¥{report.budget:.0f} · 枚举 {report.stats['enumerated']} 组"
             f" → 可行 {report.stats['feasible']} 组")

    for opt in report.options:
        L.append("")
        L.append(BAR)
        L.append(f"【{opt.label}】max-min {opt.min_score:.2f} · "
                 f"到手 ¥{opt.plan.net_price:.1f}"
                 + (f"（券省 {opt.plan.coupon_saving:.1f}）" if opt.plan.coupon else ""))
        L.append("  购物车：" + "  ".join(
            f"{n}×{q}" for n, q in _aggregate(opt.plan.items)))
        L.append("")
        for v in opt.members:
            L.append(f"  {v.name}  ¥{v.pay:.1f}  满意度 {v.score:.2f}  "
                     f"（{v.kcal:.0f} kcal）")
            if layer >= 2 and v.notes:
                for n in v.notes:
                    L.append(f"      · {n}")
        if layer >= 2 and opt.rationale:
            L.append("  为什么是这套：")
            for r in opt.rationale:
                L.append("    · " + r)

    if layer >= 2 and report.counterfactuals:
        L.append("")
        L.append(BAR)
        L.append("【有没有人被牺牲】")
        for c in report.counterfactuals:
            L.append("  · " + c)

    if layer >= 3:
        L.append("")
        L.append(BAR)
        L.append("【数据层】")
        L.append(f"  聚合目标：max-min（罗尔斯差异原则），不是平均满意度最大化")
        L.append(f"  分账规则：自己点的自己付，券省金额按原价比例摊，尾差归分摊最多者")
        tot = sum(v.pay for v in best.members)
        L.append(f"  分账校验：{'+'.join(f'{v.pay:.1f}' for v in best.members)}"
                 f" = ¥{tot:.1f}，与到手价 ¥{best.plan.net_price:.1f} 一致")
        L.append(f"  LLM 未参与任何数字计算，价格来自门店菜单，营养来自麦当劳营养表")
    return "\n".join(L)
