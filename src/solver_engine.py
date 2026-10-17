"""求解引擎：分层枚举 + 标量化打分 + 非支配过滤。

为什么不用 LLM 直接挑：这一步要可复现、可单测、可被暴力枚举验证。
候选宇宙按角色分层（主餐 / 套餐 × 小食 × 饮品 × 甜品），避免组合爆炸。
"""
from __future__ import annotations

from itertools import product
from typing import Iterable, Optional

from . import coupon_engine as CE
from .data import Catalog
from .models import Constraints, MenuItem, Nutrient, Plan, Score, Tier
from .taste_engine import taste_match

TIER_WEIGHTS = {
    "saver":    {"cheap": 0.40, "saving": 0.20, "coupon": 0.25, "taste": 0.10, "kcal_fit": 0.05, "protein": 0.00},
    "balanced": {"cheap": 0.15, "saving": 0.15, "coupon": 0.10, "taste": 0.25, "kcal_fit": 0.20, "protein": 0.15},
    "treat":    {"cheap": 0.02, "saving": 0.03, "coupon": 0.05, "taste": 0.45, "kcal_fit": 0.20, "protein": 0.25},
}
TIER_META = {
    "saver":    ("省钱档", 0.85),
    "balanced": ("均衡档", 1.00),
    "treat":    ("放纵档", 1.35),
}
FRIED_KEYWORDS = ("脆汁鸡", "麦乐鸡", "鸡翅", "鸡排", "脆皮鸡", "V翅", "薯条", "薯饼")


class Candidate:
    __slots__ = ("plan", "nutrient", "taste", "score", "main_protein")

    def __init__(self, plan: Plan, nutrient: Nutrient, taste: float, main_protein: float = 0.0):
        self.plan = plan
        self.nutrient = nutrient
        self.taste = taste
        self.main_protein = main_protein
        self.score = Score()

    @property
    def kcal(self) -> float:
        return self.nutrient.kcal

    @property
    def protein(self) -> float:
        return self.nutrient.protein


def _is_fried(item: MenuItem) -> bool:
    return any(k in item.name for k in FRIED_KEYWORDS)


def _blocked(item: MenuItem, c: Constraints) -> bool:
    if item.protein in c.avoid:
        return True
    if "spicy" in c.avoid and item.spicy:
        return True
    if "fried" in c.avoid and _is_fried(item):
        return True
    if "dessert" in c.avoid and item.role == "dessert":
        return True
    return False


def generate(cat: Catalog, c: Constraints) -> list[Plan]:
    """枚举候选方案。先做硬约束剪枝，再交给打分。"""
    mains = [i for i in cat.by_role("main") if not _blocked(i, c)]
    combos = [i for i in cat.by_role("combo") if not _blocked(i, c)]
    sides = [i for i in cat.by_role("side") if not _blocked(i, c)]
    drinks = [i for i in cat.by_role("drink") if not _blocked(i, c)]
    desserts = [i for i in cat.by_role("dessert") if not _blocked(i, c)]

    plans: list[Plan] = []

    def add(items: list[MenuItem]):
        p = Plan(items=items)
        if p.price <= c.budget + 1e-9:
            plans.append(p)

    for combo in combos:
        add([combo])
        if "dessert" not in c.avoid:
            for d in desserts:
                add([combo, d])

    # 只保留「像一顿饭」的结构：套餐，或 主餐 + 至少一份小食/饮品。
    # 裸主餐不作为候选，否则省钱档会给出「一块炸鸡」这种答案。
    for m in mains:
        for s in sides:
            add([m, s])
        for d in drinks:
            add([m, d])
        for s, d in product(sides, drinks):
            add([m, s, d])
        if "dessert" not in c.avoid:
            for s in sides:
                for ds in desserts:
                    add([m, s, ds])
    return plans


def _nutrient_of(cat: Catalog, plan: Plan) -> Optional[Nutrient]:
    parts = [cat.nutrient(i) for i in plan.items]
    if any(not p.complete for p in parts):
        return None
    return Nutrient(
        kcal=round(sum(p.kcal for p in parts), 1),
        protein=round(sum(p.protein for p in parts), 1),
        fat=round(sum(p.fat for p in parts), 1),
        carbohydrate=round(sum(p.carbohydrate for p in parts), 1),
        sodium=round(sum(p.sodium for p in parts), 1),
        calcium=round(sum(p.calcium for p in parts), 1),
        source="composed",
    )


DRINK_HINTS = ("可乐", "雪碧", "怡泉", "红茶", "咖啡", "牛奶", "苹果汁", "柠", "橙",
               "奶铁", "黑巧", "抹茶", "炫酷", "豆浆")


def _looks_like_drink(name: str) -> bool:
    return any(k in name for k in DRINK_HINTS)


def _has_drink(cat: Catalog, plan: Plan) -> bool:
    """方案是否带饮品——套餐要按其标准构成展开判断，不能只看顶层。

    套餐构成里的名字可能是营养表口径（如「可乐小杯」），不一定能在菜单里直接查到，
    所以先用目录查角色，查不到再退回关键词判断。
    """
    if any(i.role == "drink" for i in plan.items):
        return True
    for i in plan.items:
        if i.role == "combo":
            for name in i.compose:
                comp = cat.by_name.get(name)
                if comp is not None and comp.role == "drink":
                    return True
                if comp is None and _looks_like_drink(name):
                    return True
    return False


def _apply_hard_filters(cat: Catalog, plans: Iterable[Plan], c: Constraints) -> tuple[list[Candidate], dict]:
    out: list[Candidate] = []
    dropped = {"nutrition_missing": 0, "kcal_over": 0, "protein_short": 0, "no_drink": 0}
    for p in plans:
        nut = _nutrient_of(cat, p)
        if nut is None:
            dropped["nutrition_missing"] += 1
            continue
        if c.kcal_cap and nut.kcal > c.kcal_cap:
            dropped["kcal_over"] += 1
            continue
        if c.protein_min and nut.protein < c.protein_min:
            dropped["protein_short"] += 1
            continue
        if c.need_drink and not _has_drink(cat, p):
            dropped["no_drink"] += 1
            continue
        # 蛋白指标只看主食，避免「加盒牛奶凑蛋白」把方案带偏
        main_protein = sum(cat.nutrient(i).protein for i in p.items if i.role in ("main", "combo"))
        out.append(Candidate(p, nut, 0.0, main_protein))  # taste 由 solve() 用真实画像覆写
    return out, dropped


def _norm(values: list[float]) -> tuple[float, float]:
    return (min(values), max(values)) if values else (0.0, 1.0)


def _scale(v: float, lo: float, hi: float) -> float:
    if hi - lo < 1e-9:
        return 1.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


class Solver:
    def __init__(self, catalog: Catalog):
        self.cat = catalog

    def solve(self, c: Constraints, fp: dict) -> dict:
        raw = generate(self.cat, c)
        cands, dropped = _apply_hard_filters(self.cat, raw, c)

        # 券与品味打分
        for cand in cands:
            coupon, saving = CE.best_for(cand.plan, self.cat.coupons, c.now, c.channel)
            cand.plan.coupon = coupon
            cand.plan.coupon_saving = saving
            cand.taste = round(
                sum(taste_match(i, fp) for i in cand.plan.items) / len(cand.plan.items), 3)

        if not cands:
            return {"candidates": [], "tiers": [], "stats": {
                "enumerated": len(raw), "passed": 0, "dropped": dropped}}

        prices = [x.plan.net_price for x in cands]
        savings = [x.plan.discount for x in cands]
        proteins = [x.main_protein for x in cands]
        coupons = [x.plan.coupon_saving for x in cands]
        p_lo, p_hi = _norm(prices)
        s_lo, s_hi = _norm(savings)
        pr_lo, pr_hi = _norm(proteins)
        cp_lo, cp_hi = _norm(coupons)

        for cand in cands:
            base_target = c.kcal_target or (c.kcal_cap * 0.85 if c.kcal_cap else 800.0)
            tol_base = max(220.0, base_target * 0.35)
            cand.score.cheap = 1.0 - _scale(cand.plan.net_price, p_lo, p_hi)
            cand.score.saving = _scale(cand.plan.discount, s_lo, s_hi)
            cand.score.protein = _scale(cand.main_protein, pr_lo, pr_hi)
            cand.score.coupon = _scale(cand.plan.coupon_saving, cp_lo, cp_hi)
            cand.score.taste = cand.taste
            cand.score.kcal_fit = max(0.0, 1.0 - abs(cand.kcal - base_target) / tol_base)

        # 先算前沿规模（此时 kcal_fit 统一按基准目标），再挑三档
        nondom = self._nondominated_count(cands)
        tiers = self._pick_tiers(cands, c)
        stats = {
            "enumerated": len(raw),
            "passed": len(cands),
            "dropped": dropped,
            "nondominated": nondom,
        }
        return {"candidates": cands, "tiers": tiers, "stats": stats}

    # ---------- 三档代表解 ----------
    def _pick_tiers(self, cands: list[Candidate], c: Constraints) -> list[Tier]:
        """三档之间施加结构性约束，避免出现「放纵档比均衡档还便宜」这种倒挂。

        省钱档：价格不高于均衡档
        均衡档：价格不低于省钱档
        放纵档：热量严格高于均衡档（放纵的定义是更满足，不是更贵）
        """
        base_target = c.kcal_target or (c.kcal_cap * 0.85 if c.kcal_cap else 800.0)
        tiers: list[Tier] = []
        floor_price = 0.0
        balanced: Candidate | None = None

        for key in ("saver", "balanced", "treat"):
            label, target_k = TIER_META[key]
            target = base_target * target_k
            # 有热量上限时，三档目标都必须落在上限以内。
            # 否则放纵档的目标（1.35 倍）会被硬约束整片剪掉，
            # 只能在边界上挤，三档就塌缩成几乎一样的方案。
            if c.kcal_cap:
                target = min(target, c.kcal_cap * 0.98)
            tol = max(220.0, target * 0.35)
            for cand in cands:
                cand.score.kcal_fit = max(0.0, 1.0 - abs(cand.kcal - target) / tol)

            ranked = sorted(cands, key=lambda x: x.score.weighted(TIER_WEIGHTS[key]), reverse=True)

            if key == "saver":
                pools = [ranked]
            elif key == "balanced":
                pools = [
                    [x for x in ranked if x.plan.net_price >= floor_price - 1e-9],
                    ranked,
                ]
            else:
                # 放纵档优先「同一主餐加码」（加甜品/加大），退而求其次才换主餐
                bk = balanced.kcal if balanced else 0.0
                bmain = balanced.plan.main.name if balanced else None
                pools = [
                    [x for x in ranked if x.kcal > bk + 1e-9
                     and x.plan.main.name == bmain
                     and x.plan.net_price >= floor_price - 1e-9],
                    [x for x in ranked if x.kcal > bk + 1e-9
                     and x.plan.net_price >= floor_price - 1e-9],
                    [x for x in ranked if x.kcal > bk + 1e-9],
                    ranked,
                ]

            pick = None
            for pool in pools:
                for cand in pool:
                    if key == "treat" or all(
                            cand.plan.main.name != t.plan.main.name for t in tiers):
                        if all(cand.plan.signature() != t.plan.signature() for t in tiers):
                            pick = cand
                            break
                if pick is not None:
                    break
            if pick is None:
                pick = ranked[0]

            floor_price = pick.plan.net_price
            if key == "balanced":
                balanced = pick

            tiers.append(Tier(
                key=key, label=label, plan=pick.plan, score=pick.score,
                nutrient=pick.nutrient,
                rationale=self._rationale(pick, c, key),
            ))
        return tiers

    def _rationale(self, cand: Candidate, c: Constraints, key: str) -> list[str]:
        out: list[str] = []
        p = cand.plan
        if p.coupon:
            out.append(f"用掉「{p.coupon['name']}」，省 {p.coupon_saving:.1f} 元")
        if p.discount > 0.5:
            out.append(f"相对原价让利 {p.discount:.1f} 元")
        if key == "saver":
            out.append("价格优先，同时保住主餐不降级")
        elif key == "treat":
            out.append("口味与蛋白优先，接受更高热量")
        else:
            out.append("价格、热量、蛋白三者平衡点")
        if cand.taste >= 0.5:
            out.append(f"口味契合约 {cand.taste:.0%}")
        return out

    # ---------- 帕累托 ----------
    @staticmethod
    def _dominates(a: Candidate, b: Candidate) -> bool:
        """四目标：净价低、蛋白高、口味高、热量贴合好。"""
        better = False
        av = (a.plan.net_price, -a.main_protein, -a.taste, -a.score.kcal_fit)
        bv = (b.plan.net_price, -b.main_protein, -b.taste, -b.score.kcal_fit)
        for x, y in zip(av, bv):
            if x > y + 1e-9:
                return False
            if x < y - 1e-9:
                better = True
        return better

    def _nondominated_count(self, cands: list[Candidate]) -> int:
        """按净价升序增量维护非支配档案，避免 O(n^2) 全量比较。"""
        archive: list[Candidate] = []
        for cand in sorted(cands, key=lambda x: x.plan.net_price):
            if any(Solver._dominates(a, cand) for a in archive):
                continue
            archive = [a for a in archive if not Solver._dominates(cand, a)]
            archive.append(cand)
        return len(archive)
