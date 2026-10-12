#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""palette_common.py — 共享常量与工具函数（仅标准库）。

职责：
  - 情绪/训练/目标枚举校验
  - JSON 读写（UTF-8、原子写入）
  - 合规禁用词自检
  - 品类兜底推断（food_experience_map 未收录的餐品按名称关键词分类）
"""
import json
import os
import re
import sys
import tempfile

# ---------- 枚举常量 ----------
MOOD_KEYS = {"energized", "cozy", "celebrate", "steady", "share", "explore"}
TRAINING_KEYS = {"力量", "有氧", "休息日"}
GOAL_KEYS = {"减脂", "增肌", "保持", "无"}
PERIOD_KEYS = {"breakfast", "lunch", "afternoon", "dinner"}
CATEGORIES = {"main", "side", "drink", "dessert", "breakfast"}
PARTY_PURPOSES = {"项目庆功", "部门团建", "朋友聚会", "家庭聚餐"}

# 合规禁用词（对外话术严禁出现，防"宣扬不健康饮食"红线）
BANNED_WORDS = ["安慰", "解压", "治愈", "情绪化进食", "放纵", "罪恶感", "暴食", "报复性"]

# 品类兜底推断关键词（按顺序命中即归类）
CATEGORY_FALLBACK_RULES = [
    ("breakfast", ["麦满分", "早安营养卷", "炒双蛋", "薯饼", "油条", "粥", "图林根香肠", "厚松饼"]),
    ("main", ["堡", "巨无霸", "卷", "多肉", "牛气满满", "支撑Pro", "薯鱼"]),
    ("side", ["薯条", "麦乐鸡", "鸡翅", "鸡排", "脆汁鸡", "V翅", "趣鸡球", "玉米杯", "派", "多笋卷", "双翅", "快乐"]),
    ("dessert", ["新地", "麦旋风", "圆筒", "雪冰", "阿芙佳朵", "拉明顿", "奶冻", "爆珠款", "苹果片"]),
    ("drink", ["可乐", "雪碧", "咖啡", "美式", "奶铁", "玛奇朵", "卡布奇诺", "牛奶", "豆浆", "红茶", "苹果汁", "怡泉", "橙", "纯悦", "柠檬", "燕麦奶", "朱古力", "茶"]),
]

# 降档提示（build_downsize 输出后 LLM 话术参考；具体替换规则在 recommend_engine.build_downsize 内实现并做目标存在性校验）
ZERO_DRINK_HINT = "含糖饮料可换无糖可口可乐（约 0 kcal）"


def skill_root():
    """返回技能根目录（本文件位于 scripts/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ref_path(name):
    return os.path.join(skill_root(), "references", name)


def data_path(name):
    return os.path.join(skill_root(), "data", name)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic_write_json(path, obj):
    """原子写 JSON：先写临时文件再 rename，避免半截文件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def infer_category(name):
    """food_experience_map 未收录时按名称关键词兜底推断品类。"""
    for cat, kws in CATEGORY_FALLBACK_RULES:
        if any(k in name for k in kws):
            return cat
    return "other"


def check_banned_words(text):
    """返回文本中命中的禁用词列表（合规自检用）。"""
    return [w for w in BANNED_WORDS if w in text]


def fmt_out(obj):
    """统一 stdout JSON 输出（UTF-8）。"""
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
