"""领域模型。所有引擎之间只通过这些结构通信。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Nutrient:
    kcal: float
    protein: float
    fat: float
    carbohydrate: float
    sodium: float
    calcium: float
    source: str = "exact"  # exact | alias | composed | estimated | missing

    @property
    def complete(self) -> bool:
        return self.source != "missing"


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    price: float
    orig_price: float
    role: str          # main | combo | side | drink | dessert
    protein: str       # chicken | beef | fish | pork | veg | none
    spicy: bool
    category: str
    compose: tuple[str, ...] = ()

    @property
    def discount(self) -> float:
        """相对锚定价值让利多少。"""
        return max(0.0, self.orig_price - self.price)


@dataclass
class Plan:
    items: list[MenuItem]
    coupon: Optional[dict] = None
    coupon_saving: float = 0.0

    @property
    def price(self) -> float:
        return sum(i.price for i in self.items)

    @property
    def orig_price(self) -> float:
        return sum(i.orig_price for i in self.items)

    @property
    def net_price(self) -> float:
        return round(max(0.0, self.price - self.coupon_saving), 2)

    @property
    def discount(self) -> float:
        return round(self.orig_price - self.net_price, 2)

    @property
    def main(self) -> MenuItem:
        for i in self.items:
            if i.role in ("main", "combo"):
                return i
        return self.items[0]

    def signature(self) -> str:
        return "+".join(sorted(i.code for i in self.items))


@dataclass
class Constraints:
    budget: float = 40.0
    kcal_cap: Optional[float] = None
    kcal_target: Optional[float] = None
    protein_min: float = 0.0
    party_size: int = 1
    avoid: frozenset[str] = frozenset()   # 忌口：beef / spicy / fried / dessert
    need_drink: bool = False
    want_main: bool = True
    channel: str = "到店"                  # 到店 | 外送
    now: str = "2026-10-17 18:57"          # yyyy-MM-dd HH:mm
    taste_weight: float = 1.0
    allow_unknown_nutrition: bool = False

    def describe(self) -> str:
        bits = [f"预算 ≤ ¥{self.budget:g}"]
        if self.kcal_cap:
            bits.append(f"热量 ≤ {self.kcal_cap:g} kcal")
        if self.protein_min:
            bits.append(f"蛋白 ≥ {self.protein_min:g} g")
        if self.party_size > 1:
            bits.append(f"{self.party_size} 人")
        if self.avoid:
            bits.append("忌口 " + "/".join(sorted(self.avoid)))
        if self.need_drink:
            bits.append("必须带饮品")
        return " · ".join(bits)


@dataclass
class Score:
    """单个方案的画像化得分，全部归一化到 [0,1]。"""
    cheap: float = 0.0
    saving: float = 0.0
    taste: float = 0.0
    kcal_fit: float = 0.0
    protein: float = 0.0
    coupon: float = 0.0

    def weighted(self, w: dict[str, float]) -> float:
        return sum(getattr(self, k) * v for k, v in w.items())


@dataclass
class Tier:
    key: str            # saver | balanced | treat
    label: str
    plan: Plan
    score: Score
    nutrient: Nutrient
    rationale: list[str] = field(default_factory=list)


@dataclass
class Report:
    constraints: Constraints
    tiers: list[Tier]
    fingerprint: dict
    eliminated: list[dict]
    counterfactuals: list[str]
    coupon_notes: list[str]
    stats: dict
    warnings: list[str] = field(default_factory=list)
