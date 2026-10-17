"""品味引擎：把点餐史压成可计算的 8 维向量，并如实给出置信度。

设计要点
--------
1. 全部指标由统计量算出，LLM 只负责把结果翻译成人话。
2. 时间衰减：越近的订单权重越高（半衰期 180 天）。
3. 置信度随样本量单调上升，低置信度时只作软加权，不作硬约束。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

from .models import MenuItem

HALF_LIFE_DAYS = 180.0
CONF_SMOOTH = 12.0

# 商品名关键词 -> (蛋白源, 是否辣, 是否炸, 是否甜品, 是否小食, 是否饮品, 尺寸线索)
_RULES: list[tuple[tuple[str, ...], dict]] = [
    (("圆筒", "冰淇淋", "新地", "麦旋风", "阿芙佳朵", "派"), {"kind": "dessert"}),
    (("可乐", "雪碧", "芬达", "怡泉", "红茶", "柠", "橙", "苹果汁", "牛奶", "豆浆"), {"kind": "drink"}),
    (("咖啡", "美式", "奶铁", "拿铁", "卡布奇诺", "浓缩", "黑巧", "抹茶"), {"kind": "drink", "caffeine": True}),
    (("薯条", "薯饼", "玉米杯", "鸡块", "鸡翅", "鸡球", "鸡排", "V翅", "扭扭薯", "芝士条", "苹果片"),
     {"kind": "side"}),
    (("脆汁鸡", "脆皮鸡", "炸鸡"), {"protein": "chicken", "fried": True}),
    (("麦辣", "香辣", "辣么", "龙焰", "椒盐", "蒜了"), {"protein": "chicken", "spicy": True, "fried": True}),
    (("鸡腿堡", "板烧", "麦香鸡", "鸡肉堡", "鸡薯"), {"protein": "chicken"}),
    (("巨无霸", "牛堡", "吉士汉堡", "安格斯", "汉堡包", "不素之霸", "牛气"), {"protein": "beef"}),
    (("麦香鱼", "鳕鱼", "三文鱼", "鱼排", "虾"), {"protein": "fish"}),
    (("猪柳", "火腿", "培根", "香肠"), {"protein": "pork"}),
    (("三件套", "四件套", "套餐", "随心配", "双人餐", "多人餐", "分享餐"), {"kind": "combo"}),
]


def _classify(name: str) -> dict:
    feat: dict = {"protein": None, "spicy": False, "fried": False,
                  "kind": "main", "caffeine": False}
    for keys, patch in _RULES:
        if any(k in name for k in keys):
            for k, v in patch.items():
                if k == "protein":
                    if feat["protein"] is None:
                        feat["protein"] = v
                elif k in ("spicy", "fried", "caffeine"):
                    feat[k] = feat[k] or v
                else:
                    # kind：甜品/饮品/小食优先级高于 combo
                    if feat["kind"] == "main" or v in ("dessert", "drink", "side"):
                        feat["kind"] = v
    return feat


def _decay(dt: datetime, now: datetime) -> float:
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)


def build_fingerprint(orders: Iterable[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now()
    w_protein = {"chicken": 0.0, "beef": 0.0, "fish": 0.0, "pork": 0.0}
    w_total = 0.0
    spicy_w = fried_w = dessert_w = 0.0
    drink_kinds: dict[str, float] = {}
    side_hits = 0
    combo_hits = 0
    prices: list[float] = []
    hours: list[int] = []
    main_count = 0
    n_items = 0
    n_orders = 0

    for order in orders:
        try:
            dt = datetime.strptime(order["createTime"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        n_orders += 1
        w = _decay(dt, now)
        prices.append(float(order.get("total", 0)))
        hours.append(dt.hour)

        for it in order.get("items", []):
            qty = int(it.get("quantity", 1))
            f = _classify(it["productName"])
            wq = w * qty
            n_items += qty
            if f["kind"] in ("main", "combo"):
                main_count += qty
            if f["protein"] and f["kind"] in ("main", "combo"):
                w_protein[f["protein"]] += wq
                w_total += wq
            elif f["protein"]:
                w_protein[f["protein"]] += wq * 0.4
                w_total += wq * 0.4
            if f["spicy"]:
                spicy_w += wq
            if f["fried"]:
                fried_w += wq
            if f["kind"] == "dessert":
                dessert_w += wq
            if f["kind"] == "drink":
                if f["caffeine"]:
                    drink_kinds["咖啡"] = drink_kinds.get("咖啡", 0) + wq
                elif "无糖" in it["productName"]:
                    drink_kinds["无糖"] = drink_kinds.get("无糖", 0) + wq
                else:
                    drink_kinds["含糖"] = drink_kinds.get("含糖", 0) + wq
            if f["kind"] == "side":
                side_hits += qty
            if f["kind"] == "combo":
                combo_hits += qty

    denom = w_total or 1.0
    ratio = {k: round(v / denom, 3) for k, v in w_protein.items()}
    n_main = max(main_count, 1)

    confidence = round(min(0.92, n_items / (n_items + CONF_SMOOTH)), 3)

    dims = {
        "protein_chicken": ratio["chicken"],
        "protein_beef": ratio["beef"],
        "protein_fish": ratio["fish"],
        "protein_pork": ratio["pork"],
        "spice_tolerance": round(min(1.0, spicy_w / n_main), 3),
        "fried_tendency": round(min(1.0, fried_w / n_main), 3),
        "sweet_tendency": round(min(1.0, dessert_w / n_main), 3),
        "side_habit": round(min(1.0, side_hits / n_main), 3),
        "combo_habit": round(min(1.0, combo_hits / n_main), 3),
    }

    top_protein = max(ratio, key=ratio.get)
    labels: list[str] = []
    if ratio[top_protein] >= 0.5:
        labels.append({"chicken": "鸡肉党", "beef": "牛肉党",
                       "fish": "鱼肉党", "pork": "猪柳党"}[top_protein])
    if dims["spice_tolerance"] >= 0.34:
        labels.append("吃辣不眨眼")
    elif dims["spice_tolerance"] <= 0.08:
        labels.append("不碰辣")
    if dims["side_habit"] >= 0.5:
        labels.append("必配小食")
    if drink_kinds:
        labels.append("饮品偏好 " + max(drink_kinds, key=drink_kinds.get))
    if dims["combo_habit"] >= 0.4:
        labels.append("套餐型选手")
    if dims["sweet_tendency"] >= 0.25:
        labels.append("甜品收尾")

    surprise = None
    if ratio["beef"] == 0 and n_orders >= 3:
        surprise = f"{n_orders} 单里 0 单牛肉"

    price_band = (0.0, 0.0)
    if prices:
        s = sorted(prices)
        price_band = (round(s[len(s) // 4], 1), round(s[(3 * len(s)) // 4], 1))

    return {
        "dims": dims,
        "ratio": ratio,
        "labels": labels,
        "confidence": confidence,
        "n_orders": n_orders,
        "n_items": n_items,
        "drink_kinds": drink_kinds,
        "surprise": surprise,
        "price_band": price_band,
        "median_price": round(sorted(prices)[len(prices) // 2], 1) if prices else 0.0,
        "hours": hours,
        "top_protein": top_protein,
    }


def taste_match(item: MenuItem, fp: dict) -> float:
    """单品与画像的契合度，[0,1]。显式特征加权，不做黑箱 embedding。"""
    ratio = fp.get("ratio", {})
    s = 0.0
    if item.protein in ratio:
        s += 0.55 * ratio.get(item.protein, 0.0)
    if item.spicy:
        if fp["dims"]["spice_tolerance"] < 0.12:
            s -= 0.22          # 明确不碰辣的人，辣味项直接扣分
        else:
            s += 0.20 * fp["dims"]["spice_tolerance"]
    elif fp["dims"]["spice_tolerance"] <= 0.08:
        s += 0.05
    if item.role in ("main", "combo") and fp["dims"]["combo_habit"] >= 0.4:
        s += 0.10
    if item.role == "dessert":
        s += 0.15 * fp["dims"]["sweet_tendency"]
    if item.role == "side":
        s += 0.10 * fp["dims"]["side_habit"]
    return round(max(0.0, min(1.0, s)), 3)


def describe(fp: dict) -> str:
    if not fp["labels"]:
        return "样本太少，还看不出来"
    return " / ".join(fp["labels"])


def confidence_note(fp: dict) -> str:
    c = fp["confidence"]
    if c >= 0.7:
        return f"置信度 {c:.2f} —— 样本充分，判断比较可靠。"
    if c >= 0.4:
        miss = int(c * 100)
        return (f"置信度 {c:.2f} —— 样本偏少，我猜你是{describe(fp)}，"
                f"但有 {100 - miss}% 的可能我错了。再吃几顿会更准。")
    return f"置信度 {c:.2f} —— 样本太少，下面只把画像当参考权重，不当结论。"
