"""券引擎。

券不是「能用就用」，这里回答三个问题：
  1. 这张券此刻到底能不能用（日期 / 星期 / 时段 / 渠道四重校验）
  2. 用在哪个商品上收益最大
  3. 为了让券生效去凑单，净收益是正还是负
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import MenuItem, Plan

WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
FRIED_CHICKEN_KEYWORDS = ("脆汁鸡", "麦乐鸡", "鸡翅", "鸡排", "脆皮鸡", "V翅")

# 券面文案与菜单命名并不一致（真实数据里就有），必须归一后再匹配，
# 否则「券永远用不上」——这是最容易被忽略、也最致命的一类 bug。
PRODUCT_ALIASES = {
    "麦辣鸡腿堡": "麦辣鸡腿汉堡",
    "麦辣鸡腿汉堡": "麦辣鸡腿汉堡",
    "枫糖风味厚松饼猪柳蛋堡三件套": "枫糖风味厚松饼猪柳蛋堡三件套",
}


def _norm(name: str) -> str:
    return PRODUCT_ALIASES.get(name, name)


def same_product(item_name: str, coupon_product: str) -> bool:
    a, b = _norm(item_name), _norm(coupon_product)
    if a == b:
        return True
    return (a in b or b in a) and abs(len(a) - len(b)) <= 3



def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M")


def validity(coupon: dict, now: str, channel: str = "到店") -> tuple[bool, str]:
    """四重校验：渠道 / 日期区间 / 星期 / 时段。"""
    if channel not in coupon.get("channels", []):
        return False, f"仅限{'/'.join(coupon['channels'])}，当前是{channel}"
    dt = _parse(now)
    d = dt.strftime("%Y-%m-%d")
    if d < coupon["dateFrom"]:
        return False, f"{coupon['dateFrom']} 起生效"
    if d > coupon["dateTo"]:
        return False, f"{coupon['dateTo']} 已过期"
    if dt.weekday() not in coupon["weekdays"]:
        return False, "仅 " + "、".join(WD[w] for w in coupon["weekdays"]) + " 可用"
    hm = dt.strftime("%H:%M")
    if not (coupon["win"][0] <= hm <= coupon["win"][1]):
        return False, f"仅 {coupon['win'][0]}–{coupon['win'][1]} 可用"
    return True, "可用"


def _is_pick2(coupon: dict) -> bool:
    return coupon["product"].startswith("*")


def _eligible_for_pick2(items: list[MenuItem]) -> list[MenuItem]:
    return [i for i in items if any(k in i.name for k in FRIED_CHICKEN_KEYWORDS)]


def evaluate(plan_items: list[MenuItem], coupon: dict, channel: str) -> Optional[float]:
    """返回该券套在这个方案上能省多少；用不上返回 None。"""
    if _is_pick2(coupon):
        cand = _eligible_for_pick2(plan_items)
        if len(cand) < 2:
            return None
        top2 = sorted(cand, key=lambda i: i.price, reverse=True)[:2]
        saving = sum(i.price for i in top2) - coupon["priceAfter"]
        return round(saving, 2) if saving > 0 else None
    for it in plan_items:
        if it.role == "main" and same_product(it.name, coupon["product"]):
            saving = it.price - coupon["priceAfter"]
            return round(saving, 2) if saving > 0 else None
    return None


def best_for(plan: Plan, coupons: list[dict], now: str, channel: str) -> tuple[Optional[dict], float]:
    """在方案上找收益最大的一张券。同一方案最多应用一张（接口约束）。"""
    best, best_saving = None, 0.0
    for c in coupons:
        ok, _ = validity(c, now, channel)
        if not ok:
            continue
        s = evaluate(plan.items, c, channel)
        if s and s > best_saving:
            best, best_saving = c, s
    return best, best_saving


def plan_notes(coupons: list[dict], now: str, channel: str) -> list[str]:
    """无论用不用得上，都要如实告诉用户券的真实状态。"""
    notes: list[str] = []
    valid, invalid = [], []
    for c in coupons:
        ok, why = validity(c, now, channel)
        (valid if ok else invalid).append((c, why))

    if not valid:
        notes.append(f"当前时刻（{now}）你的 {len(coupons)} 张券一张都用不了。")
        for c, why in invalid[:4]:
            notes.append(f"  · {c['name']}：{why}")
        nxt = _next_window(invalid, now)
        if nxt:
            notes.append(f"  最近可用：{nxt}")
    else:
        notes.append(f"当前有 {len(valid)} 张券可用：" + "、".join(c["name"] for c, _ in valid))
        for c, why in invalid[:3]:
            notes.append(f"  · {c['name']} 不可用：{why}")
    return notes


def _next_window(invalid: list[tuple[dict, str]], now: str) -> Optional[str]:
    dt = _parse(now)
    best = None
    for c, _ in invalid:
        start = _parse(c["dateFrom"] + " " + c["win"][0])
        if start > dt and (best is None or start < best[0]):
            best = (start, c)
    if not best:
        return None
    start, c = best
    delta_h = (start - dt).total_seconds() / 3600
    when = f"{start.strftime('%m-%d %H:%M')}（约 {delta_h:.1f} 小时后）" if delta_h > 1 else \
           f"{start.strftime('%m-%d %H:%M')}"
    return f"{c['name']}，{when}"


def relevance(coupons: list[dict], catalog, fp: dict) -> list[str]:
    """券与口味的匹配度。券没用上，有时不是时机问题，是发错了人。"""
    from .taste_engine import taste_match

    off, on, unavailable = [], [], []
    for c in coupons:
        if _is_pick2(c):
            continue
        item = next((i for i in catalog.by_role("main")
                     if same_product(i.name, c["product"])), None)
        if item is None:
            unavailable.append(c["name"])
            continue
        (on if taste_match(item, fp) >= 0.35 else off).append(c["name"])

    out = []
    if off:
        out.append(f"{len(off)} 张券指向你几乎不点的品类：" + "、".join(off[:3])
                   + " —— 券不是没用，是没发对人。")
    if on:
        out.append(f"{len(on)} 张券和你的口味对得上：" + "、".join(on[:3]))
    if unavailable:
        out.append(f"{len(unavailable)} 张券对应商品本店当前未售：" + "、".join(unavailable[:3]))
    return out


def addon_analysis(items: list[MenuItem], coupons: list[dict], now: str, channel: str,
                   catalog_by_name: dict[str, MenuItem]) -> list[dict]:
    """凑单分析：为了用掉一张券，额外买的东西值不值。"""
    out = []
    present = {i.name for i in items}
    for c in coupons:
        ok, why = validity(c, now, channel)
        if not ok or _is_pick2(c):
            continue
        if c["product"] in present:
            continue
        item = catalog_by_name.get(c["product"])
        if not item:
            continue
        saving = item.price - c["priceAfter"]
        net = round(saving - item.price, 2)   # 买进来再打折，等价于净收益为负
        out.append({
            "coupon": c["name"], "need": c["product"],
            "cost": item.price, "saving": round(saving, 2), "net": net,
            "verdict": "不划算" if net <= 0 else "划算",
        })
    return out
