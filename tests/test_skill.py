"""Skill 接入层的回归测试。

这些用例保护的是「换个门店、换份菜单还能不能算对」，
对应的都是真实接口踩过的坑，不是假想场景。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

from src.data import Catalog
from src.ingest import build_aliases, infer_composes, normalize_menu, normalize_nutrition

ROOT = pathlib.Path(__file__).resolve().parent.parent

QUERY_MEALS = {"data": {
    "categories": [
        {"name": "鸡肉汉堡\n/卷", "meals": [
            {"code": "9900005462", "tags": ["套餐", "新升级"]},
            {"code": "1406", "tags": ["单品"]}]},
        {"name": "饮品", "meals": [
            {"code": "9900008751", "tags": ["冷饮"]},
            {"code": "4810", "tags": ["小食"]}]},
        {"name": "小食甜品\n/其他", "meals": [{"code": "521955"}]},
        {"name": "巨无霸\n牛鱼肉堡", "meals": [{"code": "1100", "tags": ["单品"]},
                                              {"code": "9900005466", "tags": ["套餐"]}]},
    ],
    "meals": {
        "9900005462": {"name": "板烧鸡腿堡三件套", "currentPrice": "34.5", "originalPrice": "48"},
        "1406": {"name": "板烧鸡腿堡", "currentPrice": "23.5", "originalPrice": "23.5"},
        "9900008751": {"name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5"},
        "4810": {"name": "薯条", "currentPrice": "14.5", "originalPrice": "14.5"},
        "521955": {"name": "麦当劳联名徽章", "currentPrice": "34.9", "originalPrice": "39.9"},
        "1100": {"name": "巨无霸", "currentPrice": "26.5", "originalPrice": "26.5"},
        "9900005466": {"name": "巨无霸三件套", "currentPrice": "37.5", "originalPrice": "51"},
    },
    "frequent": {"code": "9900005432", "tags": ["我的常点"]},
}}


class TestQueryMealsShape(unittest.TestCase):
    """meals 是 map 不是数组，直接遍历会拿到键名。"""

    def setUp(self):
        self.cat = Catalog.from_payload({"store": {"storeCode": "1950064"},
                                         "menu": QUERY_MEALS})
        self.by_name = {i.name: i for i in self.cat.items}

    def test_parses_every_sku(self):
        self.assertEqual(len(self.cat.items), 6)  # 7 个商品，1 个周边被排除

    def test_prices_come_from_strings(self):
        self.assertAlmostEqual(self.by_name["板烧鸡腿堡"].price, 23.5)
        self.assertAlmostEqual(self.by_name["板烧鸡腿堡三件套"].orig_price, 48.0)

    def test_tags_mark_combos(self):
        self.assertEqual(self.by_name["板烧鸡腿堡三件套"].role, "combo")
        self.assertEqual(self.by_name["巨无霸三件套"].role, "combo")

    def test_merch_is_excluded(self):
        self.assertNotIn("麦当劳联名徽章", self.by_name)
        self.assertTrue(any("周边" in x for x in self.cat.issues))

    def test_name_beats_category(self):
        """薯条被挂在「饮品」分类下，按分类判会变成饮料。"""
        self.assertEqual(self.by_name["薯条"].role, "side")
        self.assertEqual(self.by_name["可乐"].role, "drink")

    def test_combo_nutrition_is_composed_not_single(self):
        """三件套必须是累加值，不能等于主餐单品的热量。"""
        combo = self.cat.nutrient(self.by_name["板烧鸡腿堡三件套"])
        main = self.cat.nutrient(self.by_name["板烧鸡腿堡"])
        self.assertEqual(combo.source, "composed")
        self.assertGreater(combo.kcal, main.kcal + 200)


class TestComboNutritionTrap(unittest.TestCase):
    """套餐名绝不能模糊匹配到单品营养。"""

    def test_combo_never_borrows_single_nutrition(self):
        raw = {"data": {"categories": [], "meals": {
            "9900005449": {"name": "吉士汉堡包套餐", "currentPrice": "26",
                           "originalPrice": "39.5"}}}}
        cat = Catalog.from_payload({"menu": raw, "store": {"storeCode": "1"}})
        item = cat.items[0]
        nut = cat.nutrient(item)
        # 营养表里有「吉士汉堡包」单品（294 kcal），套餐若借用它就是少算一整餐
        self.assertEqual(nut.source, "missing",
                         "套餐被错误地匹配到了单品营养，热量会被严重低估")


class TestRoleInference(unittest.TestCase):
    def test_spicy_wing_is_not_drink(self):
        """「脆汁鸡」含「汁」字，曾一度被当成饮品。"""
        items = normalize_menu([{"productName": "麦麦脆汁鸡1块", "currentPrice": "15"}], [])
        self.assertEqual(items[0].role, "main")

    def test_actual_juice_is_drink(self):
        items = normalize_menu([{"productName": "100% 苹果汁(盒装)", "currentPrice": "12"}], [])
        self.assertEqual(items[0].role, "drink")


class TestAliases(unittest.TestCase):
    def test_size_prefix_and_suffix_both_resolve(self):
        table = {"中薯条": {"energyKcal": 289, "protein": 4, "fat": 12,
                            "carbohydrate": 38, "sodium": 165, "calcium": 18},
                 "可乐中杯": {"energyKcal": 147, "protein": 0, "fat": 0,
                              "carbohydrate": 36, "sodium": 0, "calcium": 10}}
        items = normalize_menu([{"productName": "薯条", "currentPrice": "14.5"},
                                {"productName": "可乐", "currentPrice": "9.5"}], [])
        aliases = build_aliases(items, table)
        self.assertEqual(aliases.get("薯条"), "中薯条")
        self.assertEqual(aliases.get("可乐"), "可乐中杯")


class TestSkillEntry(unittest.TestCase):
    """scripts/pair.py 是宿主唯一的调用入口，必须稳定、可解析。"""

    def _run(self, payload=None, args=()):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pair.py"), *args],
            input=json.dumps(payload, ensure_ascii=False) if payload else None,
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))

    def test_demo_runs_without_token(self):
        r = self._run(args=("--demo",))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["tiers"]), 3)

    def test_demo_squad_runs_without_payload(self):
        r = self._run(args=("--demo", "--mode", "squad"))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["party"], 3)
        self.assertTrue(d["options"])

    def test_live_payload_path(self):
        r = self._run({"mode": "solo", "now": "2026-10-17 18:57",
                       "constraints": {"budget": 40},
                       "store": {"storeCode": "1950064"}, "menu": QUERY_MEALS})
        d = json.loads(r.stdout)
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["dataStatus"]["source"], "live")
        self.assertTrue(d["tiers"])

    def test_bad_json_returns_structured_error(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pair.py")],
            input="{not json", capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        d = json.loads(r.stdout)
        self.assertFalse(d["ok"])
        self.assertIn("JSON", d["error"])
        self.assertNotIn("Traceback", r.stdout)

    def test_squad_requires_two_members(self):
        r = self._run({"mode": "squad", "members": [{"name": "独食"}]})
        d = json.loads(r.stdout)
        self.assertFalse(d["ok"])
        self.assertIn("2", d["error"])

    def test_squad_runs(self):
        r = self._run({"mode": "squad", "budget": 100, "now": "2026-10-17 18:57",
                       "store": {"storeCode": "1950064"}, "menu": QUERY_MEALS,
                       "members": [
                           {"name": "小明", "kcalCap": 600, "avoid": ["spicy"]},
                           {"name": "小美", "taste": "beef"},
                           {"name": "老王", "taste": "chicken", "spiceTolerance": 0.9}]})
        d = json.loads(r.stdout)
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["party"], 3)
        self.assertTrue(d["options"])


class TestCouponMarkdown(unittest.TestCase):
    """query-my-coupons 返回的是给人读的 Markdown，不是 JSON。"""

    MD = """共 6 张可用优惠券 | 第 1/1 页

## 11.9元麦辣鸡腿堡
- **优惠**: ¥11.9 (用券价格)
- **有效期**: 2026-10-19 10:30-2026-10-23 23:59 周一、二、三、四、五 10:30-23:59
- **标签**: 到店专用、外送专用

## 巧克力味厚松饼猪柳蛋套餐
- **优惠**: ¥29.9 (用券价格)
- **有效期**: 2026-10-18 05:00-2026-10-18 10:29 周日 05:00-10:29
- **标签**: 到店专用
"""

    def test_parses_price_window_weekday_channel(self):
        from src.ingest import parse_coupon_markdown
        cs = parse_coupon_markdown(self.MD, [])
        self.assertEqual(len(cs), 2)
        c = cs[0]
        self.assertEqual(c["name"], "11.9元麦辣鸡腿堡")
        self.assertEqual(c["product"], "麦辣鸡腿堡")   # 去掉「11.9元」前缀
        self.assertAlmostEqual(c["priceAfter"], 11.9)
        self.assertEqual(c["dateFrom"], "2026-10-19")
        self.assertEqual(c["dateTo"], "2026-10-23")
        self.assertEqual(c["win"], ["10:30", "23:59"])
        self.assertEqual(c["weekdays"], [0, 1, 2, 3, 4])  # 周一至周五
        self.assertEqual(c["channels"], ["到店", "外送"])

    def test_sunday_only_coupon(self):
        from src.ingest import parse_coupon_markdown
        c = parse_coupon_markdown(self.MD, [])[1]
        self.assertEqual(c["weekdays"], [6])
        self.assertEqual(c["win"], ["05:00", "10:29"])
        self.assertEqual(c["channels"], ["到店"])

    def test_garbage_text_does_not_crash(self):
        from src.ingest import parse_coupon_markdown
        issues: list[str] = []
        self.assertEqual(parse_coupon_markdown("暂无可用优惠券", issues), [])
        self.assertTrue(issues)

    def test_coupon_name_matches_menu_with_different_wording(self):
        """券面「麦辣鸡腿堡」要能匹配菜单「麦辣鸡腿汉堡」。"""
        menu = json.loads((ROOT / "fixtures" / "menu.json").read_text(encoding="utf-8"))
        raw = [{"productCode": m["code"], "productName": m["name"],
                "currentPrice": m["price"]} for m in menu]
        cat = Catalog.from_payload({"store": {"storeCode": "1950064"}, "menu": raw,
                                    "coupons": self.MD})
        burger = cat.by_name["麦辣鸡腿汉堡"]
        from src import coupon_engine as CE
        plan = __import__("src.models", fromlist=["Plan"]).Plan(items=[burger])
        hit, saving = CE.best_for(plan, cat.coupons, "2026-10-19 12:30", "到店")
        self.assertIsNotNone(hit, "券没有匹配上同名不同写法的商品")
        self.assertAlmostEqual(saving, 22.5 - 11.9, places=2)


class TestOrderList(unittest.TestCase):
    """order-list 的字段叫 orderProductList / realTotalAmount。"""

    RAW = {"data": {"list": [{
        "orderId": "REDACTED-ORDER-01",
        "createTime": "2026-09-03 12:09:01",
        "storeName": "麦当劳日照芒果时代广场餐厅",
        "realTotalAmount": "25",
        "orderProductList": [
            {"productCode": "9900005432", "productName": "麦麦脆汁鸡1块", "quantity": 1},
            {"productCode": "515837", "productName": "圆筒冰淇淋", "quantity": 2}]}]}}

    def test_two_level_envelope_and_renamed_fields(self):
        from src.ingest import normalize_orders
        orders = normalize_orders(self.RAW, [])
        self.assertEqual(len(orders), 1)
        o = orders[0]
        self.assertAlmostEqual(o["total"], 25.0)
        self.assertEqual(len(o["items"]), 2)
        self.assertEqual(o["items"][1]["quantity"], 2)


class TestRoleAccuracy(unittest.TestCase):
    """用人工标注的真实菜单当基准，量化名称推断的可信度。

    这条测试的意义不是「必须 100%」——而是让误差范围**被写下来**，
    而不是靠「看起来还行」。
    """

    def test_agrees_with_manual_labels(self):
        from src import classify
        menu = json.loads((ROOT / "fixtures" / "menu.json").read_text(encoding="utf-8"))
        diff = [(m["role"], classify.infer_role(m["name"], m["compose"]), m["name"])
                for m in menu]
        wrong = [d for d in diff if d[0] != d[1]]
        acc = 1 - len(wrong) / len(menu)
        self.assertGreaterEqual(acc, 0.95, f"准确率跌到 {acc:.1%}，不一致：{wrong}")

    def test_known_gap_is_only_sauce_chicken(self):
        """已知差异只应出现在「蘸酱炸鸡」系列——它们既能当小食也能当主餐。"""
        from src import classify
        menu = json.loads((ROOT / "fixtures" / "menu.json").read_text(encoding="utf-8"))
        wrong = [m["name"] for m in menu
                 if classify.infer_role(m["name"], m["compose"]) != m["role"]]
        self.assertTrue(all("蘸酱" in n for n in wrong),
                        f"出现了预期之外的误判：{wrong}")


class TestRealFixtureRoundTrip(unittest.TestCase):
    """真实菜单走实时路径，结果应与 fixture 路径结构一致。"""

    def test_live_matches_fixture_shape(self):
        menu = json.loads((ROOT / "fixtures" / "menu.json").read_text(encoding="utf-8"))
        nut = json.loads((ROOT / "fixtures" / "nutrition.json").read_text(encoding="utf-8"))
        raw = [{"productCode": m["code"], "productName": m["name"],
                "currentPrice": m["price"]} for m in menu]
        cat_issues: list[str] = []
        items = normalize_menu(raw, cat_issues)
        items, inferred = infer_composes(items, cat_issues)
        table = normalize_nutrition([{"productName": k, **v} for k, v in nut.items()],
                                    cat_issues)
        aliases = build_aliases(items, table)

        combo = next(i for i in items if i.name == "板烧鸡腿堡三件套")
        self.assertEqual(combo.role, "combo")
        self.assertEqual(len(combo.compose), 3)
        self.assertIn("薯条", combo.compose)
        self.assertGreater(inferred, 5)
        self.assertTrue(aliases)


if __name__ == "__main__":
    unittest.main()
