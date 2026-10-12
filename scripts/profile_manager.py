#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile_manager.py — 用户静态档案 CRUD。

子命令：
  get                 读取档案（不存在时输出 {"exists": false}，不报错）
  init                从参数创建档案（已存在则拒绝，除非 --force）
  update              部分更新（合并传入的非空字段）

档案 schema：
  {"version":1, "created_at":..., "updated_at":..., "allergens":[], "dislikes":[], "fitness_goal":"减脂|增肌|保持|无"}
路径：data/profile.json（已 gitignore，绝不入库）
"""
import argparse
import datetime
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from palette_common import GOAL_KEYS, atomic_write_json, data_path, fmt_out, load_json

PROFILE = lambda: data_path("profile.json")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def cmd_get(_):
    p = PROFILE()
    if not __import__("os").path.exists(p):
        fmt_out({"exists": False})
        return
    prof = load_json(p)
    fmt_out({"exists": True, "profile": prof})


def _clean_goal(goal):
    if goal in GOAL_KEYS:
        return goal
    raise SystemExit(json.dumps({"ok": False, "error": f"fitness_goal 必须是 {sorted(GOAL_KEYS)} 之一"}, ensure_ascii=False))


def cmd_init(args):
    p = PROFILE()
    if __import__("os").path.exists(p) and not args.force:
        raise SystemExit(json.dumps({"ok": False, "error": "档案已存在；如需覆盖请加 --force"}, ensure_ascii=False))
    goal = _clean_goal(args.goal)
    prof = {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "allergens": args.allergens or [],
        "dislikes": args.dislikes or [],
        "fitness_goal": goal,
    }
    atomic_write_json(p, prof)
    fmt_out({"ok": True, "profile": prof})


def cmd_update(args):
    p = PROFILE()
    if not __import__("os").path.exists(p):
        raise SystemExit(json.dumps({"ok": False, "error": "档案不存在，请先 init"}, ensure_ascii=False))
    prof = load_json(p)
    if args.allergens is not None:
        prof["allergens"] = args.allergens
    if args.dislikes is not None:
        prof["dislikes"] = args.dislikes
    if args.goal:
        prof["fitness_goal"] = _clean_goal(args.goal)
    prof["updated_at"] = now_iso()
    atomic_write_json(p, prof)
    fmt_out({"ok": True, "profile": prof})


def main():
    ap = argparse.ArgumentParser(description="用户档案管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get").set_defaults(func=cmd_get)
    p_init = sub.add_parser("init")
    p_init.add_argument("--allergens", nargs="*", default=[], help="过敏原列表，如 麸质 乳制品")
    p_init.add_argument("--dislikes", nargs="*", default=[], help="忌口关键词列表")
    p_init.add_argument("--goal", required=True, help="减脂|增肌|保持|无")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)
    p_upd = sub.add_parser("update")
    p_upd.add_argument("--allergens", nargs="*", default=None)
    p_upd.add_argument("--dislikes", nargs="*", default=None)
    p_upd.add_argument("--goal", default=None)
    p_upd.set_defaults(func=cmd_update)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
