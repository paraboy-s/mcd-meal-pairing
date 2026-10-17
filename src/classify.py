"""从商品名推断结构化属性。

真实接口返回的菜单只有 `productName` 与价格，没有 `role` / `protein` / `spicy`。
求解器需要这三样才能工作，因此这里按名称做推断——推断规则全部写在明面上，
并由 `tests/test_classify.py` 用真实菜单逐条回归，避免静默出错。
"""
from __future__ import annotations

import re

# 判定顺序有意义：先整体后局部。套餐名里常含「可乐」，必须先判套餐。
_COMBO = ("三件套", "套餐", "随心配", "单人餐", "双人餐", "全家桶", "组合")
_DESSERT = ("冰淇淋", "圆筒", "麦旋风", "新地", "派", "阿芙佳朵", "拉明顿",
            "雪冰", "奶冻", "爆爆珠")
_DRINK = ("可乐", "雪碧", "芬达", "果汁", "苹果汁", "橙汁", "茶", "咖啡", "奶",
          "豆浆", "纯悦", "美式", "奶铁", "玛奇朵", "卡布奇诺", "黑巧",
          "橙橙", "柠柠", "麦炫酷", "怡泉", "冰沙")
_SIDE = ("薯条", "薯饼", "玉米杯", "苹果片", "芝士条", "笋卷", "沙拉", "油条",
         "扭扭薯", "双全盒", "香肠", "泡泡", "蘸酱", "拼盘")
_MAIN = ("堡", "卷", "麦满分", "饭", "粥", "鸡", "鱼", "肉")

_BEEF = ("牛", "安格斯", "巨无霸", "吉士", "不素之霸")
_FISH = ("鱼", "鳕", "虾", "三文鱼")
_PORK = ("猪", "培根", "火腿", "香肠", "图林根")
_CHICKEN = ("鸡", "脆汁", "V翅", "麦乐")
_SPICY = ("辣", "川", "椒盐", "龙焰")


def infer_role_soft(name: str) -> str | None:
    """只靠名称关键词判角色，判不出来返回 None（不猜）。

    顺序有意义：「麦辣鸡腿汉堡三件套」同时含「堡」和「三件套」，
    必须整体先判；「圆筒冰淇淋」不能因为分类里有「饮」字就被当成饮品。
    """
    for kw in _COMBO:
        if kw in name:
            return "combo"
    for kw in _DESSERT:
        if kw in name:
            return "dessert"
    for kw in _SIDE:
        if kw in name:
            return "side"
    for kw in _DRINK:
        if kw in name:
            return "drink"
    for kw in _MAIN:
        if kw in name:
            return "main"
    return None


def infer_role(name: str, compose: list[str] | None = None) -> str:
    """combo | main | side | drink | dessert

    名称优先，compose 只作兜底。否则「鸡薯双全盒」这种小食拼盘
    因为含两个子项就会被判成套餐。
    """
    soft = infer_role_soft(name)
    if soft is not None:
        return soft
    if compose:
        return "combo"
    return "main"


def infer_protein(name: str) -> str:
    """chicken | beef | fish | pork | veg | none"""
    # 「鸡肉堡」同时含「鸡」，先判牛肉/鱼肉/猪肉，避免「培根鸡腿堡」被判成鸡肉
    for kw in _BEEF:
        if kw in name:
            return "beef"
    for kw in _FISH:
        if kw in name:
            return "fish"
    for kw in _PORK:
        if kw in name:
            return "pork"
    for kw in _CHICKEN:
        if kw in name:
            return "chicken"
    return "none"


def infer_spicy(name: str) -> bool:
    return any(kw in name for kw in _SPICY)


def infer_category(role: str, protein: str) -> str:
    if role == "combo":
        return {"chicken": "鸡肉汉堡", "beef": "牛鱼肉堡", "fish": "牛鱼肉堡",
                "pork": "单人餐"}.get(protein, "套餐")
    if role == "main":
        return {"chicken": "鸡肉汉堡", "beef": "牛鱼肉堡", "fish": "牛鱼肉堡",
                "pork": "牛鱼肉堡"}.get(protein, "小食甜品")
    if role in ("side", "dessert"):
        return "小食甜品"
    if role == "drink":
        return "饮品"
    return "其他"


def strip_size(name: str) -> str:
    """去掉杯型后缀，便于营养表匹配：「可乐中杯」→「可乐」。"""
    return re.sub(r"(大杯|中杯|小杯|（盒装）|【.*?】|“.*?”)$", "", name).strip()
