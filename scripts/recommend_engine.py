#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recommend_engine.py — 麦麦心情调色盘推荐引擎（仅标准库）。

设计原则：脚本管数字，LLM 管语义。
  脚本（本文件）：过敏原/忌口过滤、热量区间校验、候选生成（套餐池+单品组合池）、四维打分。
  LLM：情绪理解、候选重排微调、推荐理由文案（话术见 emotion_palette.json）。

v2 套餐优先改造（解决推送价格过高问题）：
  - 候选来源 = 套餐池（references/set_meals.json）+ 单品组合池，统一打分排序
  - score = 0.40×nutrition + 0.25×emotion + 0.20×value + 0.15×is_set（套餐软优先）
  - value（性价比）= 0.5×省钱率 + 0.3×营养单价归一化 + 0.2×口碑
  - 套餐超营养带上限不弃（1.3 倍内），以「换子项降档」（swappable）参与营养评分

个人模式用法：
  python recommend_engine.py --mood celebrate --training 力量 --goal 减脂 --period lunch \
      [--profile data/profile.json] [--nutrition data/nutrition_cache.json]

团餐模式用法（官方 5 档搭配规则）：
  python recommend_engine.py --mode party --purpose 项目庆功 --headcount 8 --budget-per-head 35

输出（stdout JSON）：emotion_palette / nutrition_band / set_meal_status / weights / candidates / filter_report / warnings
退出码：0 成功；1 配置或数据错误；2 无可推候选（需 LLM 降级话术）。
"""
import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from palette_common import (
    MOOD_KEYS, PARTY_PURPOSES, PERIOD_KEYS, TRAINING_KEYS,
    atomic_write_json, data_path, fmt_out, infer_category, load_json, ref_path,
)

MAX_CANDIDATES = 12          # 单品组合池截断
OUT_CANDIDATES = 9           # 最终输出候选数
WARN_DISCLAIMER = "营养与过敏原信息仅供参考，不构成营养或医疗建议，最终以门店公示信息为准。"

# ---------- 评分权重（可调） ----------
W_NUTRITION = 0.40           # 营养带贴近度
W_EMOTION = 0.25             # 情绪体验匹配
W_VALUE = 0.20               # 性价比（三因子）
W_SET_BONUS = 0.15           # 套餐软优先加成
V_SAVINGS = 0.5              # 性价比·省钱率
V_PROTEIN_PRICE = 0.3        # 性价比·营养单价
V_HALO = 0.2                 # 性价比·口碑
SET_OVER_CAP_RATIO = 1.3     # 套餐热量超上限倍数（超过才弃）
SET_DEGRADE_MIN_COUNT = 2    # 套餐合格候选低于此数时标注降级原因

# 团餐官方 5 档规则（来自 query-meals 工具描述）
PARTY_TIERS = [
    {"max": 20, "rule": "小食", "slots": ["side"]},
    {"max": 30, "rule": "汉堡+小食 或 汉堡+饮料", "slots": ["main", "side_or_drink"]},
    {"max": 40, "rule": "汉堡+薯条/小食+饮料", "slots": ["main", "side", "drink"]},
    {"max": 50, "rule": "汉堡+薯条+小食+饮料", "slots": ["main", "fries_side", "side", "drink"]},
    {"max": float("inf"), "rule": "丰富组合，优先不重复小食", "slots": ["main", "fries_side", "side2", "side", "drink"]},
]

# 聚餐目的加权
PURPOSE_WEIGHTS = {
    "项目庆功": {"celebrate": 1.5, "share_fun": 1.3},
    "部门团建": {"share_fun": 1.6, "explore_new": 1.2},
    "朋友聚会": {"share_fun": 1.5, "explore_new": 1.3},
    "家庭聚餐": {"cozy_classic": 1.4, "light_easy": 1.2},
}


# ---------- 数据加载 ----------

def load_foods(nutrition_path):
    cache = load_json(nutrition_path, None)
    if not cache or not cache.get("foods"):
        print(json.dumps({"ok": False, "error": "营养缓存不存在，请先调用 list-nutrition-foods 并运行 parse_nutrition.py"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    return cache["foods"]


def load_set_meals():
    return load_json(ref_path("set_meals.json"), {"meals": [], "halo_scores": {}})


def load_price_ref():
    return load_json(ref_path("price_ref.json"), {"prices": {}, "category_default": {}})


def enrich(foods):
    """合并 food_experience_map 的品类与标签，未收录的兜底推断品类。
    blocked（price_ref.blocked，无 SKU/不可售/无实价）的品直接挡在组合池外。"""
    fmap = load_json(ref_path("food_experience_map.json"), {"foods": {}})
    blocked = set(load_price_ref().get("blocked", {}))
    out = []
    for f in foods:
        if f["name"] in blocked:
            continue
        meta = fmap["foods"].get(f["name"])
        if meta:
            cat, is_new, tags = meta[0], meta[1], meta[2:]
        else:
            cat, is_new, tags = infer_category(f["name"]), 0, []
        out.append({**f, "category": cat, "is_new": bool(is_new), "tags": tags})
    return out


# ---------- 过滤层 ----------

def filter_allergen(foods, allergens, report):
    amap = load_json(ref_path("allergen_map.json"), {"allergens": {}})["allergens"]
    for allergen in allergens or []:
        kws = amap.get(allergen, {}).get("keywords", [])
        removed = [f["name"] for f in foods if any(k in f["name"] for k in kws)]
        if removed:
            report["allergen_removed"].extend(f"{n}（{allergen}）" for n in removed)
        foods = [f for f in foods if f["name"] not in removed]
    return foods


def filter_dislike(foods, dislikes, report):
    for d in dislikes or []:
        removed = [f["name"] for f in foods if d and d in f["name"]]
        if removed:
            report["dislike_removed"].extend(f"{n}（忌口:{d}）" for n in removed)
        foods = [f for f in foods if f["name"] not in removed]
    return foods


def filter_set_meals(meals, profile, report):
    """套餐池过滤：构成子项命中过敏原/忌口关键词即弃。"""
    amap = load_json(ref_path("allergen_map.json"), {"allergens": {}})["allergens"]
    out = []
    for m in meals:
        comp = " ".join(m.get("composition", []))
        blocked = None
        for allergen in profile.get("allergens") or []:
            kws = amap.get(allergen, {}).get("keywords", [])
            if any(k in comp for k in kws):
                blocked = f"{m['name']}（含{allergen}风险）"
                break
        if blocked:
            report["allergen_removed"].append(blocked)
            continue
        for d in profile.get("dislikes") or []:
            if d and any(d in c for c in m.get("composition", [])):
                blocked = f"{m['name']}（忌口:{d}）"
                break
        if blocked:
            report["dislike_removed"].append(blocked)
            continue
        out.append(m)
    return out


def get_band(goal, training, period):
    """计算最终热量/蛋白区间（含训练日修正与时段缩放）。goal=无 时返回 None。"""
    if goal == "无":
        return None
    bands = load_json(ref_path("nutrition_bands.json"))
    b = bands["bands"].get(goal)
    if not b:
        return None
    lo, hi = b["base"]["kcal_range"]
    pmin = b["base"]["protein_min"]
    mod = b["training_modifiers"].get(training, {})
    hi += mod.get("kcal_upper_delta", 0)
    pmin += mod.get("protein_delta", 0)
    scale = bands["period_adjust"].get(period, {}).get("kcal_scale", 1.0)
    lo, hi = round(lo * scale), round(hi * scale)
    return {"kcal_range": [lo, hi], "protein_min": pmin,
            "note": f"{goal}·{training}·{period}（区间已按时段 {scale}x 缩放）"}


# ---------- 套餐池 ----------

def set_meal_emotion_fit(meal, mood_cfg):
    """套餐情绪匹配：命中体验标签 0.85，未命中给底线 0.4（不惩罚非完美匹配，套餐优先的软性表达）；
    explore 情绪且带新品标签 +0.15。"""
    tag = mood_cfg["experience_tag"]
    hit = tag in meal.get("tags", [])
    ef = 0.85 if hit else 0.4
    if mood_cfg.get("mood_key") == "explore" and "explore_new" in meal.get("tags", []):
        ef = min(1.0, ef + 0.15)
    return ef


def gen_set_meal_candidates(meals, band, mood_cfg, halo_scores, period):
    """套餐候选生成：时段匹配 → 热量预筛（超上限 1.3 倍弃）→ 换子项降档 → 基础分。

    返回 (candidates, degrade_note)。candidates 内含 _nf/_ef/savings_rate/halo_score，供统一打分。
    """
    cands = []
    for m in meals:
        if period not in m.get("periods", []):
            continue
        kcal = m["nutrition"]["kcal"]
        protein = m["nutrition"]["protein_g"]
        eff_kcal = kcal
        swaps_used = []
        if band:
            hi = band["kcal_range"][1]
            cap = hi * SET_OVER_CAP_RATIO
            swappable = m.get("swappable", [])
            # 以「全部换子项后的最低热量」判定是否超上限 1.3 倍——换子项是套餐的天然降档机制，
            # 用默认热量预筛会误杀「换子项后可达标」的套餐（2026-10-11 实测发现）
            min_possible = kcal + sum(s["kcal_delta"] for s in swappable)
            if min_possible > cap:
                continue  # 即使全换也超上限 1.3 倍，弃
            if kcal > hi:
                # 贪心换子项：按热量降幅最大优先，换到进带或用完替换项
                for s in sorted(swappable, key=lambda x: x["kcal_delta"]):
                    if eff_kcal <= hi:
                        break
                    eff_kcal += s["kcal_delta"]
                    swaps_used.append(s)
                if eff_kcal > cap:
                    continue
        price_eff = round(m["price_ref"] + sum(s["price_delta"] for s in swaps_used), 1)
        orig = m["original_price_ref"]
        savings_rate = round(1 - price_eff / orig, 3) if orig else 0.0
        halo = halo_scores.get(m.get("halo", "普通"), 0.5)
        # P2-3：换子项后蛋白同步修正（swappable 带 protein_delta；缺失按 0）
        protein_eff = round(protein + sum(s.get("protein_delta", 0) for s in swaps_used), 1)
        nf = nutrition_fit({"kcal": eff_kcal, "protein_g": protein_eff}, band)
        ef = set_meal_emotion_fit(m, mood_cfg)
        cands.append({
            "kind": "set_meal",
            "name": m["name"],
            "menu_code_hint": m.get("menu_code_hint", ""),
            "composition": m["composition"],
            "price_ref": price_eff,
            "original_price_ref": orig,
            "savings": round(orig - price_eff, 1),
            "savings_rate": savings_rate,
            "halo_score": halo,
            "totals": {"kcal": eff_kcal, "protein_g": protein_eff},
            "kcal_default": kcal,
            "swap_options": swaps_used,
            "nutrition_estimated": bool(m["nutrition"].get("estimated")),
            "_nf": nf,
            "_ef": ef,
        })
    note = None
    if len(cands) < SET_DEGRADE_MIN_COUNT:
        note = "当前场景合格套餐不足（时段不供应或热量超限），已补充单品组合推荐"
    return cands, note


def combo_price(items, price_ref):
    """单品组合价格估算：成员参考价合计，缺失用品类默认价。返回 (总价, 是否含估价)。"""
    total = 0.0
    estimated = False
    for i in items:
        p = price_ref.get("prices", {}).get(i["name"])
        if p is None:
            p = price_ref.get("category_default", {}).get(i["category"], 12.0)
            estimated = True
        total += p
    return round(total, 1), estimated


# ---------- 单品组合池（原 v1 逻辑） ----------

def gen_combos_personal(foods, band, mood_cfg):
    """骨架模板 + 贪心填充生成单品组合候选（含 _nf/_ef 基础分）。"""
    by_cat = {}
    for f in foods:
        by_cat.setdefault(f["category"], []).append(f)

    period = mood_cfg.get("_period", "lunch")
    weights = mood_cfg["combo_hints"]["category_weights"]

    # 骨架：早餐 B+D；下午茶轻量；正餐按热量预算选档
    if period == "breakfast":
        skeletons = [["breakfast", "drink"]]
    elif period == "afternoon":
        skeletons = [["drink", "dessert"], ["side", "drink"], ["main"]]
    else:
        skeletons = [["main", "drink"], ["main", "side", "drink"], ["main", "side", "dessert", "drink"]]

    # 单品软上限：单件热量超过区间上限 1.25 倍的，不进入组合池（保留在报告中）
    if band:
        cap = band["kcal_range"][1] * 1.25
        by_cat = {c: [f for f in pool if f["energy_kcal"] <= cap] or pool for c, pool in by_cat.items()}

    candidates = []
    for skel in skeletons:
        pools = []
        for cat in skel:
            pool = by_cat.get(cat, [])
            if not pool:
                pools = []
                break
            pools.append(pool)
        if not pools:
            continue
        # 每骨架：主槽位取 top（按标签命中+蛋白），辅槽位取 3 个候选
        top_mains = rank_pool(pools[0], mood_cfg, weights.get("main", 0.5), band)
        mains = top_mains[:3]
        rest_pools = [rank_pool(p, mood_cfg, 0.3, band)[:3] for p in pools[1:]]
        for main in mains:
            for sides in itertools.product(*rest_pools):
                items = [main] + list(sides)
                totals = sum_items(items)
                if band and (totals["kcal"] < band["kcal_range"][0] or totals["kcal"] > band["kcal_range"][1]):
                    # 区间外：仅保留略超上限的组合以生成降档建议，超 25% 以上丢弃
                    if totals["kcal"] > band["kcal_range"][1] * 1.25:
                        continue
                    if totals["kcal"] < band["kcal_range"][0] and totals["kcal"] < band["kcal_range"][0] * 0.7:
                        continue
                candidates.append({
                    "items": items, "totals": totals,
                    "_nf": nutrition_fit(totals, band),
                    "_ef": emotion_fit(items, mood_cfg),
                })
    candidates.sort(key=lambda c: -(0.6 * c["_nf"] + 0.4 * c["_ef"]))
    # 去重：按餐品名集合
    seen, uniq = set(), []
    for c in candidates:
        key = tuple(sorted(i["name"] for i in c["items"]))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
        if len(uniq) >= MAX_CANDIDATES:
            break
    return uniq


def sum_items(items):
    t = {"kcal": 0, "protein_g": 0, "fat_g": 0, "carb_g": 0, "sodium_mg": 0}
    for i in items:
        t["kcal"] += i.get("energy_kcal", 0)
        t["protein_g"] += i.get("protein_g", 0)
        t["fat_g"] += i.get("fat_g", 0)
        t["carb_g"] += i.get("carb_g", 0)
        t["sodium_mg"] += i.get("sodium_mg", 0)
    return {k: round(v, 1) for k, v in t.items()}


def nutrition_fit(totals, band):
    """热量贴近度 + 蛋白达标度（连续斜坡公式，无 band 时给中性 0.5）。

    区间内：中心 1.0 → 边缘 0.5（线性）；区间外：边缘 0.5 → 超上限 25% 时 0。
    连续性保证「区间内偏离中心」与「刚好超线」的惩罚可比，修复旧公式
    「超线几乎不罚、区间内重罚」的不对称（该缺陷会误伤热量略高的套餐候选）。
    """
    if not band:
        return 0.5
    lo, hi = band["kcal_range"]
    mid = (lo + hi) / 2
    half = max((hi - lo) / 2, 1)
    kcal = totals["kcal"]
    if lo <= kcal <= hi:
        fit = 1.0 - 0.5 * (abs(kcal - mid) / half)
    else:
        over = (kcal - hi) if kcal > hi else (lo - kcal)
        fit = max(0.0, 0.5 - 0.5 * (over / max(hi * 0.25, 1)))
    prot_fit = 1.0 if totals["protein_g"] >= band["protein_min"] else max(0.0, totals["protein_g"] / max(band["protein_min"], 1))
    return round(0.7 * fit + 0.3 * prot_fit, 3)


def emotion_fit(items, mood_cfg):
    """单品组合的体验标签与今日情绪标签的重合度。"""
    tag = mood_cfg["experience_tag"]
    hit = sum(1 for i in items if tag in i.get("tags", []))
    new_bonus = 0.2 if mood_cfg.get("mood_key") == "explore" and any(i.get("is_new") for i in items) else 0.0
    return round(min(1.0, hit / max(len(items), 1) + new_bonus), 3)


def rank_pool(pool, mood_cfg, weight, band):
    """槽位内排序：情绪标签命中 + 蛋白缺口优先 + 热量贴合（轻权重，避免高热量新品霸榜）。
    penalty_keywords（来自 emotion_palette）命中的餐品降权，实现「避开大份/新品」等软偏好。"""
    tag = mood_cfg["experience_tag"]
    mid = (band["kcal_range"][0] + band["kcal_range"][1]) / 2 if band else None
    penalties = mood_cfg.get("combo_hints", {}).get("penalty_keywords", [])

    def keyf(f):
        s = 0.0
        if tag in f.get("tags", []):
            s += 1.0
        if f.get("is_new") and mood_cfg.get("mood_key") == "explore":
            s += 0.5
        if any(k in f.get("name", "") for k in penalties):
            s -= 0.6
        if band:
            s += min(f.get("protein_g", 0), band["protein_min"]) / max(band["protein_min"], 1) * 0.5
            s += max(0.0, 0.4 - abs(f.get("energy_kcal", 0) - mid) / max(mid, 1))
        return s

    return sorted(pool, key=keyf, reverse=True)


def build_downsize(items, band):
    """组合略超热量上限时生成降档建议。目标品必须真实存在（营养库或价格表），否则跳过。"""
    pr = load_price_ref()
    valid_names = set()
    try:
        cache = load_json(data_path("nutrition_cache.json"), None)
        if cache and cache.get("foods"):
            valid_names |= {f["name"] for f in cache["foods"]}
    except Exception:
        pass
    valid_names |= set(pr.get("prices", {}).keys())

    opts = []
    for i in items:
        if i["category"] == "drink" and ("可乐" in i["name"] or "雪碧" in i["name"]) and "无糖" not in i["name"]:
            target = "无糖可口可乐中杯" if "中杯" in i["name"] else "无糖可口可乐大杯"
            if target in valid_names:
                opts.append({"from": i["name"], "to": target, "kcal_saved": int(i["energy_kcal"])})
        elif i["category"] == "side" and i["name"].startswith("大"):
            target = i["name"].replace("大", "中", 1)
            if target not in valid_names:  # P2-2：如「中杯玉米杯」不存在则降档到小
                target = i["name"].replace("大", "小", 1)
            if target in valid_names:
                opts.append({"from": i["name"], "to": target, "kcal_saved": None})
        elif i["category"] == "side" and i["name"].startswith("中薯"):
            if "小薯条" in valid_names:
                opts.append({"from": i["name"], "to": "小薯条", "kcal_saved": None})
    return opts


# ---------- 团餐模式 ----------

def gen_party(foods, purpose, headcount, budget_per_head):
    tier = next(t for t in PARTY_TIERS if budget_per_head < t["max"])
    weights = PURPOSE_WEIGHTS.get(purpose, {})
    price_ref = load_price_ref()
    by_cat = {}
    for f in foods:
        by_cat.setdefault(f["category"], []).append(f)

    def scored(pool):
        return sorted(pool, key=lambda f: -sum(w for tag, w in weights.items() if tag in f.get("tags", [])))

    def unit_price(f):
        return price_ref.get("prices", {}).get(f["name"]) or price_ref.get("category_default", {}).get(f["category"], 12.0)

    mains, sides, drinks = scored(by_cat.get("main", [])), scored(by_cat.get("side", [])), scored(by_cat.get("drink", []))
    if not mains or not sides or not drinks:
        print(json.dumps({"ok": False, "error": "品类池为空，无法生成团餐组合"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    def build_combo(slots):
        """按槽位取排名靠前的候选构建单人组合，返回 per_head_items 或 None。"""
        used_sides, per_head = set(), []
        for si, slot in enumerate(slots):
            if slot == "main":
                per_head.append(mains[si % len(mains)])
            elif slot in ("side", "side2", "fries_side"):
                pool = [s for s in sides if s["name"] not in used_sides or slot == "side"]
                if not pool:
                    pool = sides
                pick = pool[si % len(pool)]
                used_sides.add(pick["name"])
                per_head.append(pick)
            elif slot == "side_or_drink":
                per_head.append((sides + drinks)[si % len(sides + drinks)])
            elif slot == "drink":
                per_head.append(drinks[si % len(drinks)])
        return per_head

    # P1-2：预算硬校验。顺序：本档位头部候选 → 本档位低价组合（换最便宜主餐/小食）→ 逐级降档；
    # 全部超预算（+15% 容差）→ 保留本档位最低价组合并标注需用户确认
    attempts = []
    tiers_chain = [tier] + [t for t in reversed(PARTY_TIERS) if t["max"] < tier["max"]]
    for t in tiers_chain:
        attempts.append((list(t["slots"]), None))                    # 档位头部候选
        attempts.append((list(t["slots"]), "cheap"))                 # 档位内低价组合
    chosen_slots, per_head_items, price_est, est_note = None, None, None, None
    for slots, mode in attempts:
        ph = build_combo(slots)
        if mode == "cheap":
            mi = next((k for k, f in enumerate(ph) if f["category"] == "main"), None)
            if mi is not None:
                ph[mi] = min(mains, key=unit_price)
            si = next((k for k, f in enumerate(ph) if f["category"] == "side" and k != mi), None)
            if si is not None:
                ph[si] = min(sides, key=unit_price)
            di = next((k for k, f in enumerate(ph) if f["category"] == "drink" and k != mi), None)
            if di is not None:
                ph[di] = min(drinks, key=unit_price)
        est = round(sum(unit_price(f) for f in ph), 1)
        if est <= budget_per_head * 1.15:
            chosen_slots, per_head_items, price_est, est_note = slots, ph, est, (
                "档位内选品，预算内" if mode is None else "档位内低价组合，预算内")
            break
    if per_head_items is None:
        slots, mode = attempts[-1]
        per_head_items = build_combo(slots)
        mi = next((k for k, f in enumerate(per_head_items) if f["category"] == "main"), None)
        if mi is not None:
            per_head_items[mi] = min(mains, key=unit_price)
        price_est = round(sum(unit_price(f) for f in per_head_items), 1)
        chosen_slots, est_note = slots, "超出预算，需 LLM 与用户确认调预算或降档"

    per_head_kcal = sum_items(per_head_items)
    candidates = [{
        "combo_id": "P1",
        "headcount": headcount,
        "budget_per_head": budget_per_head,
        "tier_rule": tier["rule"],
        "price_est_per_head": price_est,
        "budget_status": est_note,
        "items": [{"name": f"{i['name']} ×{headcount}", "kcal": i["energy_kcal"], "protein": i["protein_g"],
                   "category": i["category"], "per_head": True,
                   "unit_price": unit_price(i)} for i in per_head_items],
        "totals": {k: round(v * headcount, 1) for k, v in per_head_kcal.items()},
        "per_head_totals": per_head_kcal,
        "score": 1.0,
    }]
    return candidates, tier


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="麦麦心情调色盘推荐引擎")
    ap.add_argument("--mode", choices=["personal", "party"], default="personal")
    # 个人模式参数
    ap.add_argument("--mood", choices=sorted(MOOD_KEYS))
    ap.add_argument("--training", choices=sorted(TRAINING_KEYS))
    ap.add_argument("--goal", help="减脂|增肌|保持|无（默认读档案；都没有则视为 无）")
    ap.add_argument("--period", choices=sorted(PERIOD_KEYS), default="lunch")
    # 团餐模式参数
    ap.add_argument("--purpose", choices=sorted(PARTY_PURPOSES))
    ap.add_argument("--headcount", type=int)
    ap.add_argument("--budget-per-head", type=float, dest="budget_per_head")
    # 数据路径
    ap.add_argument("--profile", default=data_path("profile.json"))
    ap.add_argument("--nutrition", default=data_path("nutrition_cache.json"))
    ap.add_argument("--exclude", default="", help="换一批：排除已展示的候选（套餐名或逗号分隔的餐品名）")
    args = ap.parse_args()

    foods_raw = load_foods(args.nutrition)

    if args.mode == "party":
        if not (args.purpose and args.headcount and args.budget_per_head):
            print(json.dumps({"ok": False, "error": "团餐模式需要 --purpose --headcount --budget-per-head"}, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        foods = enrich(foods_raw)
        cands, tier = gen_party(foods, args.purpose, args.headcount, args.budget_per_head)
        fmt_out({
            "ok": True, "mode": "party",
            "purpose": args.purpose,
            "emotion_palette": None,
            "nutrition_band": None,
            "party_tier": {"rule": tier["rule"], "per_head_budget": args.budget_per_head},
            "candidates": cands,
            "filter_report": {"allergen_removed": [], "dislike_removed": [], "note": "团餐模式未应用个人档案过滤，请 LLM 口头确认团队忌口"},
            "warnings": [WARN_DISCLAIMER, "团餐实际可点性与价格以门店菜单为准，需经 query-meals 校验商品 code 后算价"],
        })
        return

    # ---------- 个人模式 ----------
    if not args.mood:
        print(json.dumps({"ok": False, "error": "个人模式需要 --mood"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    profile = load_json(args.profile, {"allergens": [], "dislikes": [], "fitness_goal": "无"})
    goal = args.goal or profile.get("fitness_goal", "无")
    training = args.training or "休息日"

    report = {"allergen_removed": [], "dislike_removed": [], "calorie_out": []}
    foods = enrich(foods_raw)
    foods = filter_allergen(foods, profile.get("allergens"), report)
    foods = filter_dislike(foods, profile.get("dislikes"), report)

    palette = load_json(ref_path("emotion_palette.json"))
    mood_cfg = next((m for m in palette["moods"] if m["mood_key"] == args.mood), None)
    if not mood_cfg:
        print(json.dumps({"ok": False, "error": f"未知情绪 {args.mood}"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    mood_cfg = {**mood_cfg, "_period": args.period}

    band = get_band(goal, training, args.period)

    # --- 套餐池 ---
    sm = load_set_meals()
    meals_pool = filter_set_meals(sm.get("meals", []), profile, report)
    set_cands, degrade_note = gen_set_meal_candidates(
        meals_pool, band, mood_cfg, sm.get("halo_scores", {}), args.period)

    # --- 单品组合池 ---
    combo_cands = gen_combos_personal(foods, band, mood_cfg)
    price_ref = load_price_ref()
    any_estimated = False
    for c in combo_cands:
        c["kind"] = "combo"
        c["price_ref"], est = combo_price(c["items"], price_ref)
        any_estimated = any_estimated or est
        c["savings_rate"] = 0.0    # 单品组合无套餐折扣结构
        c["halo_score"] = 0.5

    all_cands = set_cands + combo_cands

    # P2-5：换一批——排除已展示候选（套餐按名，组合按成员名任一命中即排除）
    excl = [x.strip() for x in args.exclude.split("，") if x.strip()]
    excl = [y for x in excl for y in x.split(",") if y.strip()]
    if excl:
        def excluded(c):
            if c["kind"] == "set_meal":
                return any(e == c["name"] or e in c["name"] for e in excl)
            return any(e in i["name"] for e in excl for i in c["items"])
        all_cands = [c for c in all_cands if not excluded(c)]

    if not all_cands:
        print(json.dumps({"ok": False, "error": "无可用候选组合：过滤后品类池不足或区间过紧", "filter_report": report}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    # --- 统一打分（两段式：先归一化营养单价，再合成总分） ---
    for c in all_cands:
        c["_pp"] = c["price_ref"] / max(c["totals"]["protein_g"], 1)
    pps = [c["_pp"] for c in all_cands]
    pp_min, pp_max = min(pps), max(pps)
    for c in all_cands:
        norm = 1.0 if pp_max == pp_min else 1 - (c["_pp"] - pp_min) / (pp_max - pp_min)
        vf = round(V_SAVINGS * c["savings_rate"] + V_PROTEIN_PRICE * norm + V_HALO * c["halo_score"], 3)
        c["value_fit"] = vf
        bonus = W_SET_BONUS if c["kind"] == "set_meal" else 0.0
        c["score"] = round(W_NUTRITION * c["_nf"] + W_EMOTION * c["_ef"] + W_VALUE * vf + bonus, 3)
        c["score_breakdown"] = {"nutrition": round(c["_nf"], 3), "emotion": round(c["_ef"], 3),
                                "value": vf, "set_bonus": bonus}

    all_cands.sort(key=lambda c: -c["score"])

    # 去重 + 取前 OUT_CANDIDATES
    seen, uniq = set(), []
    for c in all_cands:
        key = ("S", c["name"]) if c["kind"] == "set_meal" else ("C", tuple(sorted(i["name"] for i in c["items"])))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
        if len(uniq) >= OUT_CANDIDATES:
            break

    out_cands = []
    for idx, c in enumerate(uniq):
        cid = chr(ord("A") + idx)
        if c["kind"] == "set_meal":
            out_cands.append({
                "combo_id": cid, "kind": "set_meal", "name": c["name"],
                "menu_code_hint": c.get("menu_code_hint", ""),
                "composition": c["composition"],
                "totals": c["totals"],
                "kcal_default": c["kcal_default"],
                "price_ref": c["price_ref"],
                "original_price_ref": c["original_price_ref"],
                "savings": c["savings"],
                "swap_options": c["swap_options"],
                "nutrition_estimated": c["nutrition_estimated"],
                "score": c["score"],
                "score_breakdown": c["score_breakdown"],
            })
        else:
            over = bool(band and c["totals"]["kcal"] > band["kcal_range"][1])
            out_cands.append({
                "combo_id": cid, "kind": "combo",
                "items": [{"name": i["name"], "kcal": i["energy_kcal"], "protein": i["protein_g"], "category": i["category"]} for i in c["items"]],
                "totals": c["totals"],
                "price_ref": c["price_ref"],
                "score": c["score"],
                "score_breakdown": c["score_breakdown"],
                "over_budget": over,
                "downsize_options": build_downsize(c["items"], band) if over else [],
            })

    fmt_out({
        "ok": True, "mode": "personal",
        "emotion_palette": {
            "mood_key": mood_cfg["mood_key"], "colors": mood_cfg["colors"],
            "experience_tag": mood_cfg["experience_tag"], "tag_display": mood_cfg["tag_display"],
            "palette_copy": mood_cfg["palette_copy"],
        },
        "nutrition_band": band,
        "set_meal_status": {"candidates": len(set_cands), "degrade_note": degrade_note},
        "weights": {"nutrition": W_NUTRITION, "emotion": W_EMOTION, "value": W_VALUE, "set_bonus": W_SET_BONUS},
        "candidates": out_cands,
        "filter_report": report,
        "warnings": ([WARN_DISCLAIMER] +
                     (["部分单品价格未收录，按品类均价估价（卡片价格仅供参考，以门店实价为准）"] if any_estimated else [])),
    })


if __name__ == "__main__":
    main()
