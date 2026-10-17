"""把外部（MCP 实时返回）数据归一化成引擎内部结构。

设计原则：
1. **字段名容错，但不猜内容。** 名字/价格对不上就记进 `issues`，不静默填默认值。
2. **每条推断都可追溯。** 由 `classify` 从名称推出的 role/protein/spicy 会标 `inferred`。
3. 缺营养的商品不删，交回求解器按 `allow_unknown_nutrition` 决定，保证处置权在调用方。
"""
from __future__ import annotations

import re
from typing import Any

from . import classify
from .models import MenuItem, Nutrient

_KCAL_KEYS = ("energyKcal", "kcal", "energy", "calories")
_PROTEIN_KEYS = ("protein",)

# 菜单接口会把周边商品混进来：棒球帽、徽章、毛绒玩具、按摩捶……
# 不过滤就会算进「预算内最优搭配」，推荐用户买一顶帽子当午饭。
_MERCH = ("周边", "帽", "徽章", "捶", "毛绒", "玩具", "联名", "盲盒",
          "袋", "杯套", "贴纸", "摆件", "挂件")

_MEAL_WORDS = ("三件套", "四件套", "套餐", "随心配", "随心选", "双人餐",
               "分享餐", "亲子餐", "乐园餐", "全家桶", "组合")


def is_merch(name: str) -> bool:
    return any(kw in name for kw in _MERCH)


def flatten_meals(raw: Any, issues: list[str]) -> list[dict]:
    """把 query-meals 的三层结构拍平成菜单行。

    真实返回形如::

        {"data": {"categories": [{"name": "鸡肉汉堡/卷",
                                  "meals": [{"code": "9900005462", "tags": ["套餐"]}]}],
                  "meals": {"9900005462": {"name": "板烧鸡腿堡三件套",
                                           "currentPrice": "34.5",
                                           "originalPrice": "48"}}}}

    注意 `meals` 是 **map 而不是数组**，直接 `for row in meals` 会遍历出键名字符串。
    分类名与 tags 是这个接口里唯一权威的结构化信息（饮品 / 汉堡 / 套餐），
    比从商品名猜要准得多，所以必须带出来。
    """
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    meals = data.get("meals")
    if not isinstance(meals, dict):
        return _as_list(raw, issues, "菜单")

    tag_of: dict[str, list] = {}
    cat_of: dict[str, str] = {}
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cname = str(cat.get("name") or "").replace("\n", "").strip()
        for entry in cat.get("meals") or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "")
            if not code:
                continue
            cat_of.setdefault(code, cname)
            if entry.get("tags"):
                tag_of.setdefault(code, []).extend(entry["tags"])

    rows: list[dict] = []
    for code, info in meals.items():
        if not isinstance(info, dict):
            continue
        rows.append({
            "productCode": code,
            "productName": info.get("name"),
            "currentPrice": info.get("currentPrice"),
            "originalPrice": info.get("originalPrice"),
            "categoryName": cat_of.get(code, ""),
            "tags": tag_of.get(code, []),
        })
    return rows


def _role_from_context(cat_name: str, tags: list, name: str) -> str | None:
    """判定顺序：接口标签 > 商品名 > 接口分类名。

    标签里的「套餐」是权威信息；商品名比分类名更可靠——
    实测分类会把薯条挂在「饮品」下，按分类判就会把薯条当饮料。
    """
    tags = tags or []
    if "套餐" in tags or "多人餐" in tags:
        return "combo"
    soft = classify.infer_role_soft(name)
    if soft is not None:
        return soft
    if cat_name:
        if "饮品" in cat_name or "咖啡" in cat_name:
            return "drink"
        if "小食" in cat_name or "甜品" in cat_name:
            return "side"
        if any(k in cat_name for k in ("汉堡", "炸鸡", "安格斯", "巨无霸", "牛鱼肉")):
            return "main"
        if any(k in cat_name for k in ("单人餐", "随心配", "500", "开心乐园",
                                       "大堡口福", "多人餐")):
            return "combo"
    return None


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


def _money(v: Any) -> float | None:
    """价格单位可能是元也可能是分。>500 视为分——真实在售套餐不存在 ¥500 以上。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f / 100.0, 2) if f > 500 else round(f, 2)


def _as_list(v: Any, issues: list[str], label: str) -> list:
    """从各种信封结构里挖出数组。

    真实返回常见两层信封：`{"data": {"list": [...]}}`，
    只找一层会把整个对象当成一个元素，得到一个字段全空的行。
    """
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for key in ("data", "list", "items", "result", "products", "records"):
            sub = v.get(key)
            if isinstance(sub, list):
                return sub
            if isinstance(sub, dict):
                for k2 in ("list", "items", "result", "products", "records", "data"):
                    if isinstance(sub.get(k2), list):
                        return sub[k2]
        return [v]
    issues.append(f"{label} 不是数组，已忽略：{type(v).__name__}")
    return []


def normalize_menu(raw: Any, issues: list[str]) -> list[MenuItem]:
    rows = flatten_meals(raw, issues)
    items: list[MenuItem] = []
    seen: set[str] = set()
    merch: list[str] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(f"菜单第 {idx} 项不是对象，已跳过")
            continue
        name = _first(row, ("name", "productName", "product_name", "showName"))
        if not name:
            issues.append(f"菜单第 {idx} 项没有商品名，已跳过")
            continue
        name = str(name)
        if is_merch(name):
            merch.append(name)
            continue
        code = str(_first(row, ("code", "productCode", "productId", "skuId", "id"), "") or "")
        if not code:
            code = f"auto-{idx}"
            issues.append(f"「{name}」没有 productCode，已生成占位 {code}（无法调用 calculate-price 校验）")
        if code in seen:
            continue
        seen.add(code)

        price = _money(_first(row, ("price", "currentPrice", "salePrice", "amount", "saleAmount")))
        if price is None:
            issues.append(f"「{name}」没有可用价格，已跳过")
            continue
        orig = _money(_first(row, ("origPrice", "originalPrice", "listPrice", "marketPrice"))) or price

        raw_compose = _first(row, ("compose", "products", "subItems", "comboItems"), [])
        compose: list[str] = []
        if isinstance(raw_compose, list):
            for c in raw_compose:
                if isinstance(c, str):
                    compose.append(c)
                elif isinstance(c, dict):
                    n = _first(c, ("productName", "name"))
                    if n:
                        compose.append(str(n))

        role = _role_from_context(str(row.get("categoryName") or ""),
                                  row.get("tags") or [], name)
        if role is None:
            role = classify.infer_role(name, compose if is_meal_label(row) else [])
        protein = classify.infer_protein(name)
        items.append(MenuItem(
            code=code, name=name, price=price, orig_price=max(orig, price),
            role=role, protein=protein, spicy=classify.infer_spicy(name),
            category=classify.infer_category(role, protein), compose=tuple(compose),
        ))
    if merch:
        issues.append(f"菜单里混入 {len(merch)} 个周边商品，已排除（"
                      + "、".join(merch[:3]) + ("等" if len(merch) > 3 else "") + "）")
    return items


def is_meal_label(row: dict) -> bool:
    """接口若显式标了套餐/单品，尊重它；否则交给名称推断。"""
    for key in ("isCombo", "combo", "isSetMeal"):
        if row.get(key) in (True, 1, "1", "true"):
            return True
    return False


def normalize_nutrition(raw: Any, issues: list[str]) -> dict[str, dict]:
    rows = _as_list(raw, issues, "营养数据")
    table: dict[str, dict] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = _first(row, ("productName", "name", "foodName"))
        if not name:
            continue
        def num(keys):
            v = _first(row, keys)
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0
        table[str(name)] = {
            "energyKcal": num(_KCAL_KEYS), "protein": num(_PROTEIN_KEYS),
            "fat": num(("fat",)), "carbohydrate": num(("carbohydrate", "carb", "sugars")),
            "sodium": num(("sodium", "na")), "calcium": num(("calcium", "ca")),
        }
    if not table:
        issues.append("营养数据为空，全部方案将因营养缺失被排除")
    return table


_SIZE_PREFIX = ("迷你", "中", "大", "小")
_SIZE_SUFFIX_FAVOR = ("中杯", "小杯", "大杯")
_SIZE_PREFIX_FAVOR = ("中", "小", "大", "迷你")


def _bare(name: str) -> str:
    """剥掉尺寸前后缀：「中薯条」→「薯条」，「可乐中杯」→「可乐」。"""
    s = classify.strip_size(name)
    for p in _SIZE_PREFIX:
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break
    return s


def _prefer_size(cands: list[str]) -> str | None:
    """同一商品有多个规格时优先取中杯/中份——餐饮默认规格，不是猜口味。

    尺寸可能在名字后面（可乐中杯），也可能在前面（中薯条），两种都要认。
    """
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for tag in _SIZE_SUFFIX_FAVOR:
        hit = [c for c in cands if c.endswith(tag)]
        if len(hit) == 1:
            return hit[0]
    for prefix in _SIZE_PREFIX_FAVOR:
        hit = [c for c in cands if c.startswith(prefix)]
        if len(hit) == 1:
            return hit[0]
    return None


def build_aliases(menu: list[MenuItem], table: dict[str, dict]) -> dict[str, str]:
    """菜单名匹配不上营养表时逐级降级，只在唯一命中时建立别名。

    真实数据的命名漂移很常见：菜单叫「薯条」，营养表叫「中薯条」；
    菜单叫「麦辣鸡腿堡」，营养表叫「麦辣鸡腿汉堡」。不做这层归一，
    套餐热量永远是空的。
    """
    aliases: dict[str, str] = {}
    for item in menu:
        name = item.name
        if name in table:
            continue
        core = _bare(name)
        if not core:
            continue

        exact = [k for k in table if _bare(k) == core]
        pick = _prefer_size(exact)
        if pick:
            aliases[name] = pick
            continue

        # 「麦辣鸡腿堡」vs「麦辣鸡腿汉堡」：仅差一个字的包含关系，且长度差 ≤3 才接受
        near = [k for k in table
                if (core in _bare(k) or _bare(k) in core)
                and abs(len(core) - len(_bare(k))) <= 3]
        pick = _prefer_size(near)
        if pick:
            aliases[name] = pick
    return aliases


# 套餐标准构成：餐饮行业默认，且本店快餐三件套一律为「主餐 + 中薯条 + 可乐中杯」。
# 仅在接口未返回 compose 时使用，且结果标为「推断」，不冒充接口数据。
_MEAL_SUFFIX = ("三件套", "套餐", "单人餐", "双人餐", "组合", "随心配")


def infer_composes(items: list[MenuItem], issues: list[str]) -> tuple[list[MenuItem], int]:
    """给缺少 compose 的套餐补上构成。

    只做**精确**推断：去掉套餐后缀后必须与菜单里某个主餐同名。
    「麦辣鸡翅4块套餐」去掉后缀是「麦辣鸡翅4块」，与菜单的「麦辣鸡翅」差一个量词，
    份量对不上会算错一半热量，因此宁可不推断，让它落到营养缺失。
    """
    from dataclasses import replace

    by_name = {i.name: i for i in items}
    side = next((s for s in items if s.role == "side" and "薯条" in s.name), None)
    drink = next((d for d in items if d.role == "drink" and "可乐" in d.name), None)

    out = list(items)
    inferred = 0
    for idx, item in enumerate(items):
        if item.role != "combo" or item.compose:
            continue
        core = item.name
        for suf in _MEAL_SUFFIX:
            if core.endswith(suf):
                core = core[: -len(suf)]
                break
        main = by_name.get(core)
        if main is None or main.role not in ("main", "combo"):
            continue
        parts = [core]
        if side is not None:
            parts.append(side.name)
        if drink is not None:
            parts.append(drink.name)
        if len(parts) < 2:
            continue
        out[idx] = replace(item, compose=tuple(parts))
        inferred += 1

    if inferred:
        issues.append(f"{inferred} 个套餐的构成由名称推断（主餐 + 标准配餐），"
                      f"热量为按成分累加的估算值，不是接口直出")
    return out, inferred


def normalize_orders(raw: Any, issues: list[str]) -> list[dict]:
    rows = _as_list(raw, issues, "历史订单")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        items = []
        # 真实字段是 orderProductList；早期接口文档里叫 items，两者都认
        for it in _as_list(_first(row, ("orderProductList", "items", "products",
                                        "orderItems"), []), issues, "订单明细"):
            if isinstance(it, dict):
                items.append({
                    "productCode": str(_first(it, ("productCode", "code"), "")),
                    "productName": str(_first(it, ("productName", "name"), "")),
                    "quantity": int(_first(it, ("quantity", "qty", "num"), 1) or 1),
                })
        out.append({
            "orderId": str(_first(row, ("orderId", "id"), "")),
            "createTime": str(_first(row, ("createTime", "orderTime", "time"), "")),
            "beType": str(_first(row, ("beType",), "1")),
            "storeName": str(_first(row, ("storeName",), "")),
            "total": _money(_first(row, ("total", "totalAmount", "realTotalAmount",
                                         "payAmount"))) or 0.0,
            "items": items,
        })
    return out


# ------------------------------------------------------------------ 优惠券
# query-my-coupons 返回的是给模型阅读的 Markdown，不是 JSON。
# 真实样本：
#   ## 11.9元麦辣鸡腿堡
#   - **优惠**: ¥11.9 (用券价格)
#   - **有效期**: 2026-10-19 10:30-2026-10-23 23:59 周一、二、三、四、五 10:30-23:59
#   - **标签**: 到店专用、外送专用
_CP_HEAD = re.compile(r"^##\s+(.+?)\s*$", re.M)
_CP_PRICE = re.compile(r"\*\*优惠\*\*\s*[:：]\s*¥\s*([\d.]+)")
_CP_VALID = re.compile(
    r"\*\*有效期\*\*\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})"
    r"\s*[-–~]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*(.*)")
_CP_TAG = re.compile(r"\*\*标签\*\*\s*[:：]\s*(.+)")
_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _weekdays(text: str) -> list[int]:
    m = re.search(r"周([一二三四五六日天、，,\s]+)", text or "")
    if not m:
        return list(range(7))
    out = sorted({_WD[ch] for ch in m.group(1) if ch in _WD})
    return out or list(range(7))


def _product_from_title(title: str) -> str:
    """券名通常把商品名写在价格后面：「11.9元麦辣鸡腿堡」→「麦辣鸡腿堡」。"""
    return re.sub(r"^\s*[\d.]+\s*元\s*", "", title).strip()


def parse_coupon_markdown(text: str, issues: list[str]) -> list[dict]:
    heads = list(_CP_HEAD.finditer(text))
    if not heads:
        issues.append("优惠券返回的文本里没有识别到券标题，本次不含券计算")
        return []

    out: list[dict] = []
    for i, m in enumerate(heads):
        title = m.group(1).strip()
        block = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        price = _CP_PRICE.search(block)
        if not price:
            issues.append(f"券「{title}」没有解析出用券价格，已跳过（不猜）")
            continue
        valid = _CP_VALID.search(block)
        tag = _CP_TAG.search(block)
        channels = ["到店", "外送"]
        if tag:
            raw_tag = tag.group(1)
            channels = [c for c in ("到店", "外送") if c in raw_tag] or channels
        out.append({
            "id": f"C{len(out) + 1}",
            "name": title,
            "product": _product_from_title(title),
            "priceAfter": float(price.group(1)),
            "dateFrom": valid.group(1) if valid else "1970-01-01",
            "dateTo": valid.group(3) if valid else "2999-12-31",
            "win": [valid.group(2), valid.group(4)] if valid else ["00:00", "23:59"],
            "weekdays": _weekdays(valid.group(5) if valid else ""),
            "channels": channels,
        })
    return out


def normalize_coupons(raw: Any, issues: list[str]) -> list[dict]:
    if isinstance(raw, str):
        return parse_coupon_markdown(raw, issues)
    if isinstance(raw, dict) and not any(
            isinstance(raw.get(k), list) for k in ("data", "list", "items", "result")):
        # 可能是 {"data": {"list": [...]}} 之外的未知结构，交给通用列表处理
        pass
    rows = _as_list(raw, issues, "优惠券")
    out: list[dict] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = _first(row, ("name", "couponName", "title", "couponTitle"))
        product = _first(row, ("product", "productName", "goodsName", "useRule"))
        price_after = _money(_first(row, ("priceAfter", "couponPrice", "salePrice", "price")))
        if not name or price_after is None:
            issues.append(f"券第 {idx} 项缺少名称/价格，已跳过（不猜）")
            continue
        if not product:
            product = _product_from_title(str(name))
        out.append({
            "id": str(_first(row, ("id", "couponId", "code"), f"C{idx}")),
            "name": str(name), "product": str(product), "priceAfter": price_after,
            "weekdays": _first(row, ("weekdays",), list(range(7))),
            "win": _first(row, ("win", "timeWindow"), ["00:00", "23:59"]),
            "dateFrom": str(_first(row, ("dateFrom", "validFrom", "startDate"), "1970-01-01")),
            "dateTo": str(_first(row, ("dateTo", "validTo", "endDate"), "2999-12-31")),
            "channels": _first(row, ("channels",), ["到店", "外送"]),
        })
    return out


def nutrition_of(row_name: str, table: dict, aliases: dict) -> Nutrient:
    hit = row_name if row_name in table else aliases.get(row_name)
    if not hit or hit not in table:
        return Nutrient(0, 0, 0, 0, 0, 0, source="missing")
    r = table[hit]
    return Nutrient(r["energyKcal"], r["protein"], r["fat"],
                    r["carbohydrate"], r["sodium"], r["calcium"],
                    source="exact" if hit == row_name else "alias")
