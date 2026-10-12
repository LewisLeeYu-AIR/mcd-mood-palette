#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge_name.py — 营养库名称 ↔ 门店菜单名称桥接匹配。

背景：list-nutrition-foods（营养库）与 query-meals（门店菜单）分属两套命名体系且均无对方
的编码，只能按名称匹配。四层策略依次降级：
  1. 精确匹配
  2. 归一化精确（去空格/全半角/统一大小写/去常见装饰符号）
  3. 双向子串包含
  4. difflib 相似度 ≥ 0.75
  5. name_alias.json 人工别名兜底

用法：
  匹配报告（联调期评估匹配率）：
    python bridge_name.py report --menu <菜单名列表文件(每行一个)>
  单条查询：
    python bridge_name.py match --menu-item "巨无霸中套餐"
"""
import argparse
import difflib
import json
import sys
import unicodedata

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from palette_common import data_path, fmt_out, load_json, ref_path

SIM_THRESHOLD = 0.75


def normalize(name: str) -> str:
    """归一化：NFKC 全半角统一、去空格、转小写、去常见装饰符号。"""
    s = unicodedata.normalize("NFKC", name)
    drop = set(' \t·・-—_()（）[]【】,，、.。\'’"“”+')
    return "".join(ch for ch in s if ch not in drop).lower()


class NameBridge:
    def __init__(self, alias_map=None):
        self.alias = (alias_map or load_json(ref_path("name_alias.json"), {}).get("aliases", {}))
        # 营养名 -> 别名列表 的倒排
        self.norm_alias = {}
        for canon, alts in self.alias.items():
            keys = [normalize(canon)] + [normalize(a) for a in alts]
            self.norm_alias[tuple(keys)] = canon

    def match(self, menu_item: str, nutrition_names):
        """菜单名 -> 营养库名。返回 (nutrition_name, method, confidence)。"""
        m_exact = normalize(menu_item)
        # L1 精确
        for n in nutrition_names:
            if n == menu_item:
                return n, "exact", 1.0
        # L2 归一化精确
        norm_map = {normalize(n): n for n in nutrition_names}
        if m_exact in norm_map:
            return norm_map[m_exact], "normalized", 0.95
        # L5 别名（放在子串前，别名是人工标注，优先级高；支持别名是菜单名的子串，取最长命中避免歧义）
        alias_hits = [(len(k), canon) for keys, canon in self.norm_alias.items() for k in keys if k and k in m_exact]
        if alias_hits:
            alias_hits.sort(reverse=True)
            canon = alias_hits[0][1]
            if canon in nutrition_names:
                return canon, "alias", 0.95
        # L3 双向子串（营养名 ⊆ 菜单名 更常见：套餐名里嵌单品名）
        subs = [n for n in nutrition_names if normalize(n) in m_exact or m_exact in normalize(n)]
        if len(subs) == 1:
            return subs[0], "substring", 0.85
        if len(subs) > 1:
            best = max(subs, key=len)
            return best, "substring_multi", 0.7
        # L4 相似度
        ratios = [(difflib.SequenceMatcher(None, m_exact, normalize(n)).ratio(), n) for n in nutrition_names]
        ratios.sort(reverse=True)
        if ratios and ratios[0][0] >= SIM_THRESHOLD:
            return ratios[0][1], "similarity", round(ratios[0][0], 2)
        return None, "none", 0.0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_r = sub.add_parser("report", help="批量匹配报告")
    p_r.add_argument("--menu", required=True, help="菜单名列表文件（每行一个）")
    p_r.add_argument("--nutrition", default=data_path("nutrition_cache.json"))
    p_m = sub.add_parser("match", help="单条匹配")
    p_m.add_argument("--menu-item", required=True)
    p_m.add_argument("--nutrition", default=data_path("nutrition_cache.json"))
    args = ap.parse_args()

    cache = load_json(args.nutrition, None)
    if not cache or not cache.get("foods"):
        print(json.dumps({"ok": False, "error": "营养缓存不存在，请先运行 parse_nutrition.py"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    names = [f["name"] for f in cache["foods"]]
    bridge = NameBridge()

    if args.cmd == "match":
        n, method, conf = bridge.match(args.menu_item, names)
        fmt_out({"menu_item": args.menu_item, "nutrition_name": n, "method": method, "confidence": conf})
        return

    with open(args.menu, encoding="utf-8") as f:
        menu = [ln.strip() for ln in f if ln.strip()]
    results = []
    for item in menu:
        n, method, conf = bridge.match(item, names)
        results.append({"menu_item": item, "nutrition_name": n, "mode": method, "confidence": conf})
    hit = [r for r in results if r["nutrition_name"]]
    rate = round(len(hit) / len(menu), 3) if menu else 0
    fmt_out({
        "ok": True,
        "menu_count": len(menu),
        "matched": len(hit),
        "match_rate": rate,
        "by_method": {m: sum(1 for r in hit if r["mode"] == m) for m in {r["mode"] for r in hit}},
        "unmatched": [r["menu_item"] for r in results if not r["nutrition_name"]],
        "results": results,
    })


if __name__ == "__main__":
    main()
