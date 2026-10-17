"""引擎单测。stdlib unittest，不引入额外依赖。

运行： python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import coupon_engine as CE
from src import guard_engine as GE
from src.data import Catalog
from src.models import Constraints, MenuItem, Nutrient, Plan
from src.pipeline import run
from src.solver_engine import Candidate, Solver, generate
from src.taste_engine import build_fingerprint, taste_match


class TestCatalog(unittest.TestCase):
    def test_menu_loaded(self):
        cat = Catalog()
        self.assertGreater(len(cat.items), 50)
        self.assertIn("9900005432", cat.by_code)

    def test_combo_nutrition_equals_parts(self):
        """套餐热量必须等于标准构成之和，不能是拍脑袋的数。"""
        cat = Catalog()
        combo = cat.by_name["板烧鸡腿堡三件套"]
        parts = [cat.nutrition.resolve(n) for n in combo.compose]
        total = sum(p.kcal for p in parts)
        self.assertAlmostEqual(cat.nutrient(combo).kcal, total, places=1)
        self.assertEqual(cat.nutrient(combo).source, "composed")

    def test_missing_nutrition_is_flagged(self):
        cat = Catalog()
        mitem = cat.by_name["龙焰鸡腿堡"]
        self.assertFalse(cat.nutrient(mitem).complete)


class TestTasteEngine(unittest.TestCase):
    def test_zero_beef_detected(self):
        cat = Catalog()
        fp = build_fingerprint(cat.orders)
        self.assertEqual(fp["ratio"]["beef"], 0.0)
        self.assertIn("鸡肉党", fp["labels"])
        self.assertIsNotNone(fp["surprise"])

    def test_confidence_monotonic(self):
        cat = Catalog()
        few = build_fingerprint(cat.orders[:1])
        many = build_fingerprint(cat.orders)
        self.assertLess(few["confidence"], many["confidence"])

    def test_taste_penalises_spicy(self):
        cat = Catalog()
        fp = build_fingerprint(cat.orders)
        spicy = cat.by_name["麦辣鸡腿汉堡"]
        mild = cat.by_name["板烧鸡腿堡"]
        self.assertGreater(taste_match(mild, fp), taste_match(spicy, fp))


class TestCouponEngine(unittest.TestCase):
    def test_weekend_rejected(self):
        c = {"id": "X", "name": "测试券", "product": "麦辣鸡腿堡", "priceAfter": 11.9,
             "weekdays": [0, 1, 2, 3, 4], "win": ["10:30", "23:59"],
             "dateFrom": "2026-10-19", "dateTo": "2026-10-23", "channels": ["到店"]}
        ok, _ = CE.validity(c, "2026-10-17 18:57", "到店")   # 周六
        self.assertFalse(ok)
        ok2, _ = CE.validity(c, "2026-10-19 19:00", "到店")  # 周一晚
        self.assertTrue(ok2)

    def test_time_window_rejected(self):
        c = {"id": "Y", "name": "早餐券", "product": "猪柳蛋麦满分", "priceAfter": 9.9,
             "weekdays": [6], "win": ["05:00", "10:29"],
             "dateFrom": "2026-10-18", "dateTo": "2026-10-18", "channels": ["到店"]}
        ok, _ = CE.validity(c, "2026-10-18 18:00", "到店")
        self.assertFalse(ok)

    def test_channel_rejected(self):
        c = {"id": "Z", "name": "到店券", "product": "麦辣鸡腿堡", "priceAfter": 11.9,
             "weekdays": [0], "win": ["10:30", "23:59"],
             "dateFrom": "2026-10-19", "dateTo": "2026-10-19", "channels": ["到店"]}
        ok, _ = CE.validity(c, "2026-10-19 12:00", "外送")
        self.assertFalse(ok)


class TestSolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = Catalog()
        cls.fp = build_fingerprint(cls.cat.orders)

    def test_constraints_hold(self):
        c = Constraints(budget=35, kcal_cap=800, avoid=frozenset({"beef"}),
                        now="2026-10-17 18:57")
        report = run(c, self.cat)
        for t in report.tiers:
            self.assertLessEqual(t.plan.net_price, 35 + 1e-6)
            self.assertLessEqual(t.nutrient.kcal, 800 + 1e-6)
            for item in t.plan.items:
                self.assertNotEqual(item.protein, "beef")

    def test_tier_prices_monotonic(self):
        c = Constraints(budget=40, now="2026-10-17 18:57")
        report = run(c, self.cat)
        prices = [t.plan.net_price for t in report.tiers]
        self.assertEqual(prices, sorted(prices), f"档位价格倒挂：{prices}")

    def test_meal_structure(self):
        """每套方案都必须「像一顿饭」：套餐，或主餐+小食/饮品。"""
        c = Constraints(budget=40, now="2026-10-17 18:57")
        report = run(c, self.cat)
        for t in report.tiers:
            roles = [i.role for i in t.plan.items]
            is_combo = "combo" in roles
            has_pair = any(r in ("side", "drink") for r in roles)
            self.assertTrue(is_combo or has_pair, f"{t.label} 结构不完整：{roles}")

    def test_nondominated_matches_naive(self):
        """增量档案法必须与 O(n^2) 朴素法结果一致。"""
        cat = self.cat
        c = Constraints(budget=30, now="2026-10-17 18:57")
        plans = generate(cat, c)[:120]
        cands = []
        for p in plans:
            nut = Nutrient(300, 15, 10, 30, 400, 100)
            cands.append(Candidate(p, nut, 0.5, 10.0))
        solver = Solver(cat)
        for i, cand in enumerate(cands):
            cand.score.kcal_fit = (i % 7) / 6.0

        naive = 0
        for i, a in enumerate(cands):
            dominated = any(
                Solver._dominates(b, a) for b in cands if b is not a)
            naive += 0 if dominated else 1

        self.assertEqual(solver._nondominated_count(cands), naive)

    def test_unknown_nutrition_excluded(self):
        c = Constraints(budget=60, now="2026-10-17 18:57")
        report = run(c, self.cat)
        for t in report.tiers:
            self.assertTrue(t.nutrient.complete)


class TestGuard(unittest.TestCase):
    def test_unknown_sku_filtered(self):
        cat = Catalog()
        ghost = MenuItem("GHOST", "幽灵堡", 9.9, 9.9, "main", "beef", False, "测试")
        plan = Plan(items=[ghost])
        kept, dropped = GE.filter_unknown(cat, [plan])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_budget_overflow_flagged(self):
        cat = Catalog()
        c = Constraints(budget=10, now="2026-10-17 18:57")
        item = cat.by_name["板烧鸡腿堡"]
        warn = GE.validate(Plan(items=[item]), cat, c, cat.nutrient(item))
        self.assertTrue(any("超出预算" in w for w in warn))

    def test_rate_limiter(self):
        rl = GE.RateLimiter(capacity=3, window=60)
        for _ in range(3):
            self.assertTrue(rl.acquire())
        self.assertFalse(rl.acquire())


if __name__ == "__main__":
    unittest.main()
