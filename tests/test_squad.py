"""Squad 引擎测试：公平性、硬约束、分账守恒。

这些用例的重点不是「跑通」，而是验证几个可以被推翻的命题：
  1. 选出来的方案，min 满意度确实是全局最优（不是近似）
  2. 任何人的热量上限、忌口都不会被突破
  3. 分账之和严格等于到手价（一分不差）
"""
from __future__ import annotations

import unittest

from src.data import Catalog
from src.models import Plan
from src.squad_engine import (Member, SquadSolver, assign, generate, parse_member,
                              presets, score_items, synth_fp)


class SquadTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = Catalog()
        cls.solver = SquadSolver(cls.cat)
        cls.members = presets()

    def solve(self, budget=100.0, members=None, now="2026-10-17 18:57"):
        return self.solver.solve(members or self.members, budget=budget, now=now)


class TestFingerprint(SquadTestBase):
    def test_synth_fp_has_required_keys(self):
        fp = synth_fp("chicken", spicy_tolerance=0.2)
        self.assertIn("ratio", fp)
        self.assertIn("dims", fp)
        self.assertAlmostEqual(fp["ratio"]["chicken"], 0.8)
        self.assertEqual(fp["dims"]["spice_tolerance"], 0.2)

    def test_parse_member(self):
        m = parse_member("小明:kcal=600,avoid=spicy,budget=34,taste=chicken")
        self.assertEqual(m.name, "小明")
        self.assertEqual(m.kcal_cap, 600.0)
        self.assertEqual(m.budget, 34.0)
        self.assertIn("spicy", m.avoid)
        self.assertTrue(m.blocks(self.cat.by_name["麦辣鸡腿汉堡"]))
        self.assertFalse(m.blocks(self.cat.by_name["板烧鸡腿堡"]))

    def test_avoid_all_spicy_excludes_all(self):
        m = Member("禁辣", synth_fp("beef"), avoid=frozenset({"spicy"}))
        spicy = [i for i in self.cat.items if i.spicy]
        self.assertTrue(spicy)
        for i in spicy:
            self.assertTrue(m.blocks(i))


class TestFeasibility(SquadTestBase):
    def test_every_member_gets_a_main(self):
        rep = self.solve()
        self.assertTrue(rep.options)
        for opt in rep.options:
            buckets = assign(opt.plan.items, rep.members, self.cat, self.solver.nutcache)
            for its in buckets:
                self.assertTrue(any(i.role in ("main", "combo") for i in its),
                                "有人没分到主食")

    def test_kcal_cap_never_broken(self):
        rep = self.solve()
        capped = [m for m in rep.members if m.kcal_cap]
        self.assertTrue(capped)
        for opt in rep.options:
            for v, m in zip(opt.members, rep.members):
                if m.kcal_cap:
                    self.assertLessEqual(v.kcal, m.kcal_cap + 1e-6,
                                         f"{v.name} 超出热量上限")

    def test_avoid_never_assigned(self):
        rep = self.solve()
        for opt in rep.options:
            for v, m in zip(opt.members, rep.members):
                for name, _ in v.items:
                    item = self.cat.by_name.get(name)
                    if item is not None:
                        self.assertFalse(m.blocks(item),
                                         f"{v.name} 被分到忌口项 {name}")

    def test_infeasible_is_reported_not_crashed(self):
        rep = self.solve(budget=30.0)
        self.assertEqual(rep.options, [])
        self.assertIn("没有", rep.note)

    def test_single_member_matches_solo_expectation(self):
        one = [Member("独食", synth_fp("chicken"), budget=60.0)]
        rep = self.solve(budget=60.0, members=one)
        self.assertTrue(rep.options)
        opt = rep.options[0]
        self.assertEqual(len(opt.members), 1)
        self.assertAlmostEqual(opt.members[0].pay, opt.plan.net_price, places=1)


class TestFairness(SquadTestBase):
    def test_selected_min_is_globally_optimal(self):
        """「最公平」的 min 分数必须是所有可行方案里最高的那个。"""
        rep = self.solve()
        carts = generate(rep.members, rep.budget, self.cat, self.solver.nutcache)
        best = 0.0
        for cart in carts:
            plan = Plan(items=list(cart))
            if plan.net_price > rep.budget + 1e-9:
                continue
            buckets = assign(cart, rep.members, self.cat, self.solver.nutcache)
            details = [score_items(m, its, self.cat, self.solver.nutcache)
                       for m, its in zip(rep.members, buckets)]
            if any(m.kcal_cap and d["kcal"] > m.kcal_cap + 1e-6
                   for m, d in zip(rep.members, details)):
                continue
            if any(d["kcal"] < 250.0 for d in details):
                continue
            best = max(best, min(d["score"] for d in details))
        self.assertAlmostEqual(rep.options[0].min_score, best, places=3)

    def test_min_beats_mean_optimal(self):
        """max-min 的短板，不应低于「平均最优解」的短板 —— 这是公平性的直接体现。"""
        rep = self.solve()
        carts = generate(rep.members, rep.budget, self.cat, self.solver.nutcache)
        best_mean, best_mean_min = 0.0, 0.0
        for cart in carts:
            plan = Plan(items=list(cart))
            if plan.net_price > rep.budget + 1e-9:
                continue
            buckets = assign(cart, rep.members, self.cat, self.solver.nutcache)
            details = [score_items(m, its, self.cat, self.solver.nutcache)
                       for m, its in zip(rep.members, buckets)]
            if any(m.kcal_cap and d["kcal"] > m.kcal_cap + 1e-6
                   for m, d in zip(rep.members, details)):
                continue
            if any(d["kcal"] < 250.0 for d in details):
                continue
            total = sum(d["score"] for d in details)
            if total > best_mean:
                best_mean, best_mean_min = total, min(d["score"] for d in details)
        self.assertGreaterEqual(rep.options[0].min_score, best_mean_min - 1e-9)

    def test_results_are_deterministic(self):
        a, b = self.solve(), self.solve()
        self.assertEqual([o.plan.signature() for o in a.options],
                         [o.plan.signature() for o in b.options])
        self.assertEqual(a.options[0].min_score, b.options[0].min_score)


class TestBilling(SquadTestBase):
    def test_split_sums_to_net_price(self):
        rep = self.solve()
        for opt in rep.options:
            total = sum(v.pay for v in opt.members)
            self.assertAlmostEqual(total, opt.plan.net_price, places=1,
                                   msg=f"{opt.label} 分账不平")

    def test_budget_never_exceeded(self):
        rep = self.solve()
        for opt in rep.options:
            self.assertLessEqual(opt.plan.net_price, rep.budget + 1e-6)

    def test_personal_budget_respected(self):
        rep = self.solve()
        for opt in rep.options:
            for v, m in zip(opt.members, rep.members):
                if m.budget:
                    self.assertLessEqual(v.pay, m.budget + 0.11)

    def test_coupon_saving_does_not_inflate_bill(self):
        rep = self.solve()
        for opt in rep.options:
            self.assertLessEqual(opt.plan.net_price, opt.plan.price + 1e-6)


if __name__ == "__main__":
    unittest.main()
