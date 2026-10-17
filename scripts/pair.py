#!/usr/bin/env python3
"""McPair Skill 的唯一计算入口。

    echo '{"constraints":{"budget":40},"menu":[...]}' | python scripts/pair.py

约定：
  - stdin 一个 JSON 对象，stdout 一个 JSON 对象，不打印任何多余文字
  - 任何失败都返回 {"ok": false, "error": "..."}，不带 traceback
  - 所有金额与热量都由本脚本算出，宿主智能体不得改写这些数字

--demo 用内置 fixtures 跑一份完整结果，供没有 Token 的人验证安装。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 版本检查放在 import 引擎之前，否则 3.6 会抛 SyntaxError，用户看到的是天书
if sys.version_info < (3, 9):
    json.dump({
        "ok": False,
        "error": f"需要 Python 3.9+，当前是 {sys.version.split()[0]}",
        "hint": "Anaconda base 常为 3.6；改用 D:\\anaconda\\envs\\py309\\python.exe "
                "或安装 3.9+ 后重试。本项目零第三方依赖，不需要 pip install。",
    }, sys.stdout, ensure_ascii=False, indent=2)
    raise SystemExit(1)

from src import view  # noqa: E402
from src.data import Catalog  # noqa: E402
from src.models import Constraints  # noqa: E402
from src.pipeline import run as run_solo  # noqa: E402

DEFAULT_SOLO_BUDGET = 40.0
DEFAULT_SQUAD_BUDGET = 100.0


def _fail(msg: str, **extra) -> dict:
    out = {"ok": False, "error": msg}
    out.update(extra)
    return out


def build_catalog(payload: dict) -> Catalog:
    if any(k in payload for k in ("menu", "nutrition", "coupons", "orders", "store")):
        return Catalog.from_payload(payload)
    cat = Catalog()
    cat.issues = ["本次使用 fixtures 截面数据（2026-10-17 18:57），价格可能已变"]
    return cat


def do_solo(payload: dict, cat: Catalog) -> dict:
    raw = payload.get("constraints") or {}
    avoid = raw.get("avoid") or []
    if isinstance(avoid, str):
        avoid = [avoid]
    c = Constraints(
        budget=float(raw.get("budget") or DEFAULT_SOLO_BUDGET),
        kcal_cap=_opt_float(raw.get("kcalCap")),
        kcal_target=_opt_float(raw.get("kcalTarget")),
        protein_min=float(raw.get("proteinMin") or 0.0),
        avoid=frozenset(a.strip() for a in avoid if a),
        need_drink=bool(raw.get("needDrink")),
        channel=raw.get("channel") or "到店",
        now=view.now_str(payload.get("now")),
    )
    report = run_solo(c, cat)
    out = view.solo_view(report, cat)
    out["reply"] = _reply(out)
    return out


def do_squad(payload: dict, cat: Catalog) -> dict:
    from src.squad_engine import Member, SquadSolver, synth_fp

    raw_members = payload.get("members") or []
    if len(raw_members) < 2:
        return _fail("squad 模式至少需要 2 位成员，每位成员给出 name 与可选 budget/kcalCap/avoid/taste")

    members = []
    for m in raw_members:
        avoid = m.get("avoid") or []
        if isinstance(avoid, str):
            avoid = [avoid]
        taste = m.get("taste")
        fp = synth_fp(taste, float(m.get("spiceTolerance") or 0.0),
                      float(m.get("sweetTendency") or 0.0)) if taste else \
            synth_fp(None, 0.5, 0.1)
        members.append(Member(
            name=str(m.get("name") or f"成员{len(members) + 1}"),
            fp=fp,
            kcal_cap=_opt_float(m.get("kcalCap")),
            avoid=frozenset(a.strip() for a in avoid if a),
            budget=_opt_float(m.get("budget")),
        ))

    budget = float(payload.get("budget") or DEFAULT_SQUAD_BUDGET)
    squad = SquadSolver(cat)
    rep = squad.solve(members, budget=budget,
                      now=view.now_str(payload.get("now")),
                      channel=payload.get("channel") or "到店")
    out = view.squad_view(rep, cat)
    out["reply"] = _squad_reply(out)
    return out


def _squad_reply(out: dict) -> str:
    if not out.get("options"):
        return (f"{out.get('party')} 人、预算池 ¥{out.get('budget')} 内没有可行组合。"
                f"拼单需要每人至少一份主餐，预算池往往要比单人相加更宽，建议提高预算或减人。")
    o = out["options"][0]
    worst = min(o["members"], key=lambda m: m["score"])
    return (f"{out['party']} 人拼单，最公平的一单 ¥{o['netPrice']}，"
            f"最低满意度 {o['minScore']}（{worst['name']}）。"
            f"注意这是 max-min 解：它不追求总满意度最高，只保证最不满意的人不被牺牲。")


def _reply(out: dict) -> str:
    if not out.get("tiers"):
        return (f"预算 ¥{out['constraints']['budget']} 内没有任何组合能凑成一顿完整的饭。"
                f"把预算抬高一点，或去掉热量/忌口限制后重算。")
    t = out["tiers"][1] if len(out["tiers"]) > 1 else out["tiers"][0]
    s = out["stats"]
    parts = [f"从 {s.get('enumerated', 0)} 种搭配里筛出 {s.get('passed', 0)} 组可行。"]
    parts.append(f"「{t['label']}」：{t['items'][0]['name']}，"
                 f"到手 ¥{t['netPrice']}、{t['kcal']} kcal。")
    if t.get("coupon"):
        parts.append(f"用掉「{t['coupon']}」，省 {t['couponSaving']} 元。")
    return "".join(parts)


def _opt_float(v):
    if v in (None, "", 0):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("mcpair-skill", add_help=True)
    ap.add_argument("--demo", action="store_true",
                    help="用内置 fixtures 跑一份示例，不需要 Token 与 stdin")
    ap.add_argument("--mode", default=None, choices=["solo", "squad"])
    args = ap.parse_args(argv)

    if args.demo:
        payload: dict = {"mode": args.mode or "solo", "now": "2026-10-17 18:57"}
        if payload["mode"] == "squad":
            payload["budget"] = 100
            payload["members"] = [
                {"name": "小明", "kcalCap": 600, "avoid": ["spicy"],
                 "budget": 34, "taste": "chicken", "spiceTolerance": 0.15},
                {"name": "小美", "taste": "beef", "budget": 40},
                {"name": "老王", "taste": "chicken", "spiceTolerance": 0.9, "budget": 40},
            ]
        else:
            payload["constraints"] = {"budget": 40}
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            json.dump({
                "ok": False,
                "error": "stdin 为空。需要传入 JSON，例如 {\"constraints\":{\"budget\":40}}；"
                         "想直接看示例请加 --demo",
            }, sys.stdout, ensure_ascii=False, indent=2)
            return 1
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            json.dump(_fail(f"stdin 不是合法 JSON：{e}"), sys.stdout,
                      ensure_ascii=False, indent=2)
            return 1
        if args.mode:
            payload["mode"] = args.mode

    mode = (payload.get("mode") or "solo").lower()
    if mode not in ("solo", "squad"):
        json.dump(_fail(f"未知 mode：{mode}，只支持 solo / squad"), sys.stdout,
                  ensure_ascii=False, indent=2)
        return 1

    try:
        cat = build_catalog(payload)
        out = do_solo(payload, cat) if mode == "solo" else do_squad(payload, cat)
    except Exception as e:  # 不让 traceback 污染 agent 的上下文
        json.dump(_fail(f"{type(e).__name__}: {e}",
                        hint="把这段 error 原样反馈给用户，不要自己编造结果"),
                  sys.stdout, ensure_ascii=False, indent=2)
        return 1

    out["skill"] = "mcpair"
    out["version"] = "0.3.0"
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
