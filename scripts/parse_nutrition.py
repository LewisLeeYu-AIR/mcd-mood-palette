#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parse_nutrition.py — 解析 list-nutrition-foods 的返回 → 结构化营养缓存 JSON。

输入格式（MCP 工具返回的 data 字符串）：
  [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
    猪柳麦满分,null,1288,308,16,16,24,781,213
    ...

用法：
  python parse_nutrition.py --input <原始返回文本文件> [--output data/nutrition_cache.json]
  （LLM 先把 MCP 工具返回保存为文本文件再调用本脚本）

输出缓存 schema：
  {"fetched_at": "ISO8601", "count": N, "foods": [{"name","energy_kcal","protein_g","fat_g","carb_g","sodium_mg","calcium_mg"}]}
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from palette_common import atomic_write_json, data_path, fmt_out


def parse(raw: str):
    """解析原始 data 字符串 → 食品列表。容错策略：跳过无法解析的行，不中断。"""
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return [], "empty input"
    header = lines[0]
    if not (header.startswith("[") and "{" in header):
        return [], f"unexpected header: {header[:60]}"
    fields = header[header.index("{") + 1:header.index("}")].split(",")
    idx = {f: i for i, f in enumerate(fields)}
    need = ["productName", "energyKcal", "protein", "fat", "carbohydrate", "sodium", "calcium"]
    for k in need:
        if k not in idx:
            return [], f"missing field {k} in header"

    foods, skipped = [], []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < len(fields):
            skipped.append(ln)
            continue
        name = parts[idx["productName"]].strip()
        if not name:
            skipped.append(ln)
            continue
        try:
            food = {
                "name": name,
                "energy_kcal": float(parts[idx["energyKcal"]]),
                "protein_g": float(parts[idx["protein"]]),
                "fat_g": float(parts[idx["fat"]]),
                "carb_g": float(parts[idx["carbohydrate"]]),
                "sodium_mg": float(parts[idx["sodium"]]),
                "calcium_mg": float(parts[idx["calcium"]]),
            }
        except ValueError:
            skipped.append(ln)
            continue
        if food["energy_kcal"] >= 0:  # 过滤脏数据（负值）
            foods.append(food)
    # 去重（同名保留首条）
    seen, uniq = set(), []
    for f in foods:
        if f["name"] not in seen:
            seen.add(f["name"])
            uniq.append(f)
    return uniq, f"skipped {len(skipped)} lines"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="MCP list-nutrition-foods 返回保存的文本文件")
    ap.add_argument("--output", default=None, help="输出缓存路径，默认 data/nutrition_cache.json")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = f.read()

    foods, note = parse(raw)
    if not foods:
        print(json.dumps({"ok": False, "error": note}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    cache = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(foods),
        "foods": foods,
    }
    out = args.output or data_path("nutrition_cache.json")
    atomic_write_json(out, cache)
    fmt_out({"ok": True, "count": len(foods), "note": note, "output": os.path.abspath(out)})


if __name__ == "__main__":
    main()
