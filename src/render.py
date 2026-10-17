"""三层输出渲染：结论 → 理由 → 数据。

渐进披露：第一层 3 秒读完，第二层给愿意看的人，第三层给较真的人。
"""
from __future__ import annotations

from .models import Report, Tier
from .taste_engine import confidence_note, describe

BAR = "─" * 52


def _items(t: Tier) -> str:
    return " + ".join(i.name for i in t.plan.items)


def render(report: Report, layer: int = 3) -> str:
    L: list[str] = []
    fp = report.fingerprint

    # ── 第一层：结论 ──
    L.append(f"【品味指纹】{describe(fp)}")
    if fp.get("surprise"):
        L.append(f"  注意：{fp['surprise']}。")
    L.append("  " + confidence_note(fp))
    L.append("")

    if not report.tiers:
        L.append("在给定约束下没有可行方案。建议放宽预算或热量上限。")
        return "\n".join(L)

    L.append(f"【结论】{report.constraints.describe()}")
    for t in report.tiers:
        L.append("")
        L.append(f"  {t.label}  ¥{t.plan.net_price:.1f}" +
                 (f"（原价 ¥{t.plan.orig_price:.1f}）" if t.plan.discount > 0.5 else ""))
        L.append(f"    {_items(t)}")
        L.append(f"    {t.nutrient.kcal:.0f} kcal · 蛋白 {t.nutrient.protein:.0f} g · "
                 f"脂肪 {t.nutrient.fat:.0f} g · 钠 {t.nutrient.sodium:.0f} mg")
        if t.plan.coupon:
            L.append(f"    已用券：{t.plan.coupon['name']}，省 {t.plan.coupon_saving:.1f} 元")

    if layer < 2:
        return "\n".join(L)

    # ── 第二层：理由 ──
    L.append("")
    L.append(BAR)
    L.append("【为什么是这三套】")
    for t in report.tiers:
        L.append(f"  {t.label}：{'；'.join(t.rationale)}")

    if report.counterfactuals:
        L.append("")
        L.append("【再动一下会怎样】")
        for c in report.counterfactuals:
            L.append(f"  · {c}")

    if report.eliminated:
        L.append("")
        n = report.stats.get("passed", 0)
        L.append(f"【被淘汰的 {n} 套里，最可惜的是】")
        for e in report.eliminated:
            L.append(f"  · {e['name']}（¥{e['price']:.1f} / {e['kcal']:.0f} kcal）"
                     f" 比{e['vs']}：{'、'.join(e['reasons'])}")

    if report.coupon_notes:
        L.append("")
        L.append("【你的券现在是什么状态】")
        for note in report.coupon_notes:
            L.append("  " + note)

    if report.warnings:
        L.append("")
        L.append("【护栏提示】")
        for w in report.warnings:
            L.append("  ! " + w)

    if layer < 3:
        return "\n".join(L)

    # ── 第三层：数据 ──
    L.append("")
    L.append(BAR)
    s = report.stats
    L.append("【数据层】")
    L.append(f"  枚举组合 {s.get('enumerated', 0)} 套 → 硬约束通过 {s.get('passed', 0)} 套 "
             f"→ 非支配前沿 {s.get('nondominated', 0)} 套")
    d = s.get("dropped", {})
    if d:
        L.append(f"  剪枝明细：热量超标 {d.get('kcal_over', 0)} · 蛋白不足 {d.get('protein_short', 0)} "
                 f"· 营养缺失 {d.get('nutrition_missing', 0)} · 缺饮品 {d.get('no_drink', 0)}")
    L.append("")
    L.append("  价格全部来自门店实时菜单，营养来自麦当劳营养表；未使用 LLM 估算任何数字。")
    L.append(f"  画像样本 {fp['n_orders']} 单 / {fp['n_items']} 件，"
             f"客单价中位数 ¥{fp['median_price']}")

    return "\n".join(L)
