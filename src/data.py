"""数据层：fixture 加载 + 营养解析（含别名、套餐拆解、缺失标记）。"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Optional

from .models import MenuItem, Nutrient

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


_BUILTIN_ALIASES: dict[str, str] | None = None


def _builtin_aliases() -> dict[str, str]:
    """菜单名 → 营养表名的内置对照表。

    这是本项目最贵的一块资产：接口菜单与营养表由两套命名维护，
    「麦麦脆汁鸡1块」对「麦麦脆汁鸡-琵琶腿」、「麦乐鸡」对「麦乐鸡5块」，
    纯靠字符串算法永远对不齐。表放在 fixtures 里，可被 payload 覆盖。
    """
    global _BUILTIN_ALIASES
    if _BUILTIN_ALIASES is None:
        try:
            _BUILTIN_ALIASES = _load("nutrition_aliases.json")
        except FileNotFoundError:
            _BUILTIN_ALIASES = {}
    return dict(_BUILTIN_ALIASES)


class NutritionResolver:
    """把菜单名解析成营养数据，并如实标注来源。

    source 取值：
      exact    营养表里直接有
      alias    经由别名表命中
      composed 套餐按标准构成累加
      estimated 结构化估算（仅用于缺少营养数据的单品）
      missing  无法解析，调用方必须自行决定是否剔除
    """

    def __init__(self, table: dict, aliases: dict):
        self.table = table
        self.aliases = aliases
        self._cache: dict[str, Nutrient] = {}

    def resolve(self, name: str, compose: tuple[str, ...] = (),
                depth: int = 0, role: str = "main") -> Nutrient:
        if name in self._cache:
            return self._cache[name]
        n = self._resolve(name, compose, depth, role)
        self._cache[name] = n
        return n

    def _resolve(self, name: str, compose: tuple[str, ...], depth: int,
                 role: str) -> Nutrient:
        if name in self.table:
            return self._mk(self.table[name], "exact")

        alias = self.aliases.get(name)
        if alias and alias in self.table and self._alias_ok(name, alias, role):
            return self._mk(self.table[alias], "alias")

        if compose and depth < 3:
            parts = [self._resolve(c, (), depth + 1, "main") for c in compose]
            if all(p.complete for p in parts):
                return Nutrient(
                    kcal=round(sum(p.kcal for p in parts), 1),
                    protein=round(sum(p.protein for p in parts), 1),
                    fat=round(sum(p.fat for p in parts), 1),
                    carbohydrate=round(sum(p.carbohydrate for p in parts), 1),
                    sodium=round(sum(p.sodium for p in parts), 1),
                    calcium=round(sum(p.calcium for p in parts), 1),
                    source="composed",
                )

        # 套餐绝不做模糊兜底：「吉士汉堡包套餐」会被「吉士汉堡包」命中，
        # 把一整顿饭算成一个汉堡，热量少算一半以上。宁可标 missing 让求解器剔除。
        if role != "combo":
            hit = self._fuzzy(name)
            if hit:
                return self._mk(self.table[hit], "alias")

        return Nutrient(0, 0, 0, 0, 0, 0, source="missing")

    @staticmethod
    def _alias_ok(name: str, alias: str, role: str) -> bool:
        if role != "combo":
            return True
        # 套餐只能映射到套餐（允许杯型/后缀差异），不能映射到单品
        meal = ("三件套", "套餐", "随心配", "单人餐", "双人餐")
        return any(k in name for k in meal) == any(k in alias for k in meal)

    def _fuzzy(self, name: str) -> Optional[str]:
        """去括号后的包含匹配，只接受唯一命中且长度接近，避免张冠李戴。"""
        core = re.sub(r"[（(].*?[）)]", "", name).strip()
        if not core:
            return None
        cands = [k for k in self.table
                 if (core in k or k in core) and abs(len(core) - len(k)) <= 3]
        if len(cands) == 1:
            return cands[0]
        return None

    @staticmethod
    def _mk(row: dict, source: str) -> Nutrient:
        return Nutrient(
            kcal=row["energyKcal"], protein=row["protein"], fat=row["fat"],
            carbohydrate=row["carbohydrate"], sodium=row["sodium"],
            calcium=row["calcium"], source=source,
        )


class Catalog:
    """门店在售目录。

    两条构造路径：
      Catalog()                 读 fixtures（离线 demo / 单测 / replay）
      Catalog.from_payload(d)   接 agent 传入的 MCP 实时数据（Skill 主路径）
    """

    def __init__(self):
        self.store = _load("store.json")
        self.items: list[MenuItem] = []
        for row in _load("menu.json"):
            self.items.append(MenuItem(
                code=row["code"], name=row["name"], price=row["price"],
                orig_price=row["origPrice"], role=row["role"],
                protein=row["protein"], spicy=row["spicy"],
                category=row["category"], compose=tuple(row["compose"]),
            ))
        self.by_name = {i.name: i for i in self.items}
        self.by_code = {i.code: i for i in self.items}
        self.nutrition = NutritionResolver(_load("nutrition.json"), _load("nutrition_aliases.json"))
        self.coupons = _load("coupons.json")
        self.orders = _load("orders.json")
        self.source = "fixtures"
        self.issues: list[str] = []

    @classmethod
    def from_payload(cls, payload: dict) -> "Catalog":
        """从 MCP 实时数据构造目录。

        只传菜单也能跑：营养表、券、订单缺失时回退到 fixtures 的截面数据，
        并在 `issues` 里逐条声明回退了什么——不让调用方以为用的是实时数据。
        """
        from . import ingest

        self = cls.__new__(cls)
        self.source = "live"
        self.issues = []
        issues = self.issues

        store = payload.get("store") or {}
        self.store = {
            "storeCode": str(store.get("storeCode") or store.get("storeId") or ""),
            "storeName": str(store.get("storeName") or ""),
            "capturedAt": str(payload.get("capturedAt") or ""),
        }
        if not self.store["storeCode"]:
            issues.append("未传 storeCode，calculate-price 校验会被跳过")

        menu_raw = payload.get("menu")
        if not menu_raw:
            issues.append("未传菜单，已回退到 fixtures 的截面菜单——价格可能已变")
            self.items = Catalog().items
        else:
            self.items = ingest.normalize_menu(menu_raw, issues)
            self.items, _ = ingest.infer_composes(self.items, issues)

        self.by_name = {i.name: i for i in self.items}
        self.by_code = {i.code: i for i in self.items}

        if payload.get("nutrition"):
            table = ingest.normalize_nutrition(payload["nutrition"], issues)
            # 三级：自动推断打底 → 内置对照表纠偏 → 调用方显式传入的最终覆盖
            aliases = ingest.build_aliases(self.items, table)
            aliases.update(_builtin_aliases())
            aliases.update(payload.get("nutritionAliases") or {})
            self.nutrition = NutritionResolver(table, aliases)
        else:
            issues.append("未传营养表，已回退到 fixtures 的营养数据")
            self.nutrition = NutritionResolver(_load("nutrition.json"),
                                               _load("nutrition_aliases.json"))

        self.coupons = (ingest.normalize_coupons(payload["coupons"], issues)
                        if payload.get("coupons") is not None else [])
        if payload.get("coupons") is None:
            issues.append("未传优惠券，本次计算不含券——不代表用户没有券")

        self.orders = (ingest.normalize_orders(payload["orders"], issues)
                       if payload.get("orders") is not None else [])
        return self

    def nutrient(self, item: MenuItem) -> Nutrient:
        return self.nutrition.resolve(item.name, item.compose, role=item.role)

    def by_role(self, *roles: str) -> list[MenuItem]:
        return [i for i in self.items if i.role in roles]

    def coverage(self) -> dict:
        out = {}
        for i in self.items:
            out[i.name] = self.nutrient(i).source
        return out
