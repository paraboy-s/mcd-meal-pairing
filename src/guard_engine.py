"""护栏引擎：幻觉过滤 / 价格漂移 / 限流 / 过敏否决。

设计原则：宁可少推荐，不可推错的。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .data import Catalog
from .models import Plan

# 忌口词 -> 商品名关键词（用于一票否决与提示）
ALLERGEN_HINTS = {
    "花生": ("花生",),
    "乳制品": ("芝士", "奶", "冰淇淋", "麦旋风", "新地", "阿芙佳朵", "卡布奇诺"),
    "麸质": ("汉堡", "堡", "薯条", "薯饼", "派", "卷", "麦满分", "扭扭薯"),
    "牛肉": ("牛", "巨无霸", "安格斯", "吉士"),
    "鸡肉": ("鸡",),
    "鱼肉": ("鱼", "鳕", "三文鱼", "虾"),
    "猪肉": ("猪", "火腿", "培根", "香肠"),
}


@dataclass
class RateLimiter:
    """MCP 硬限制 600 req/min，这里留 20% 余量。"""
    capacity: int = 480
    window: float = 60.0
    _hits: list[float] = field(default_factory=list)

    def acquire(self) -> bool:
        now = time.time()
        self._hits = [t for t in self._hits if now - t < self.window]
        if len(self._hits) >= self.capacity:
            return False
        self._hits.append(now)
        return True

    def used(self) -> int:
        now = time.time()
        return len([t for t in self._hits if now - t < self.window])


def filter_unknown(catalog: Catalog, plans: list[Plan]) -> tuple[list[Plan], int]:
    """任何不在门店在售目录里的 SKU 一律丢弃（幻觉过滤）。"""
    kept, dropped = [], 0
    for p in plans:
        if all(i.code in catalog.by_code for i in p.items):
            kept.append(p)
        else:
            dropped += 1
    return kept, dropped


def allergen_report(catalog: Catalog, avoid: frozenset[str]) -> list[str]:
    if not avoid:
        return []
    hits = []
    for token in avoid:
        keys = ALLERGEN_HINTS.get(token, (token,))
        matches = [i.name for i in catalog.items if any(k in i.name for k in keys)]
        if matches:
            hits.append(f"{token}：已排除 {len(matches)} 个相关 SKU（如 {matches[0]}）")
    return hits


def price_drift(local_plan: Plan, live_prices: dict[str, float]) -> tuple[bool, float, list[str]]:
    """本地估算 vs calculate-price 真实返回。以真实值为准。"""
    if not live_prices:
        return False, 0.0, []
    notes = []
    delta = 0.0
    for item in local_plan.items:
        live = live_prices.get(item.code)
        if live is None:
            notes.append(f"{item.name}：真实接口未返回价格，已标记为不可下单")
            continue
        d = live - item.price
        if abs(d) >= 0.01:
            delta += d
            notes.append(f"{item.name}：本地 ¥{item.price:.1f} → 实时 ¥{live:.1f}（{d:+.1f}）")
    return bool(notes), round(delta, 2), notes


def validate(plan: Plan, catalog: Catalog, constraints, nutrient=None) -> list[str]:
    """下单前终检。返回阻断级告警。"""
    warn: list[str] = []
    for item in plan.items:
        if item.code not in catalog.by_code:
            warn.append(f"SKU {item.code} 不在当前门店在售列表，禁止下单")
    if plan.net_price > constraints.budget + 1e-6:
        warn.append(f"到手价 ¥{plan.net_price:.1f} 超出预算 ¥{constraints.budget:.1f}")
    if nutrient is not None:
        if constraints.kcal_cap and nutrient.kcal > constraints.kcal_cap + 1e-6:
            warn.append(f"热量 {nutrient.kcal:.0f} kcal 超出上限 {constraints.kcal_cap:.0f}")
        if constraints.protein_min and nutrient.protein < constraints.protein_min - 1e-6:
            warn.append(f"蛋白 {nutrient.protein:.0f} g 未达下限 {constraints.protein_min:.0f} g")
    if not nutrient or not nutrient.complete:
        warn.append("营养数据不完整，已标记为参考值")
    return warn
