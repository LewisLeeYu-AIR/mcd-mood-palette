#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""self_check.py — 发布前一键质量自查。

检查项（对照 SKILL_DEV_STANDARD.md 3.3）：
  1. 六件套文件齐全且文件名精确（大小写敏感）
  2. CONTEST_DECLARATION.md 与官方版本一致（SHA256 比对）
  3. mcp-config.example.json 可解析且仅含占位符
  4. 全仓库敏感信息扫描（疑似真实 Token 的 Bearer 模式）
  5. 全仓库合规禁用词扫描（SKILL.md/README/references 话术）
  6. references JSON 完整性（合法 JSON + 枚举值合法）
  7. scripts 语法检查（py_compile）
  8. 引擎端到端冒烟（用内置样例营养数据跑 6 组情绪×训练组合）
"""
import hashlib
import io
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from palette_common import (
    BANNED_WORDS, data_path, ref_path, skill_root,
)

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "status": PASS if ok else FAIL, "detail": detail})
    return ok


def walk_files(root):
    ignore = {".git", "__pycache__", "data"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def main():
    root = skill_root()

    # 1. 六件套
    required = ["README.md", "CONTEST_DECLARATION.md", "MCP_INTEGRATION.md",
                "mcp-config.example.json", "SKILL.md", "workbuddy.md"]
    actual = os.listdir(root)
    missing = [f for f in required if f not in actual]
    check("six_files", not missing, f"缺失: {missing}" if missing else "全部在场")

    # 2. 官方声明 SHA（仅当兄弟目录可见时严格比对；新环境降级为提示）
    official = os.path.join(os.path.dirname(root), "mcd-developer-innovation-challenge", "CONTEST_DECLARATION.md")
    if os.path.exists(official):
        sha_a = hashlib.sha256(open(official, "rb").read()).hexdigest()
        sha_b = hashlib.sha256(open(os.path.join(root, "CONTEST_DECLARATION.md"), "rb").read()).hexdigest()
        check("contest_declaration_sha", sha_a == sha_b, f"official={sha_a[:12]} local={sha_b[:12]}")
    else:
        check("contest_declaration_sha", True, "SKIP：官方模板不在本机，发布前在含官方仓库的环境跑一次严格比对")

    # 3. mcp-config.example.json
    try:
        cfg = json.load(open(os.path.join(root, "mcp-config.example.json"), encoding="utf-8"))
        s = json.dumps(cfg)
        placeholder_ok = "YOUR_MCP_TOKEN" in s and not re.search(r"Bearer\s+[A-Za-z0-9_\-]{20,}", s.replace("YOUR_MCP_TOKEN", ""))
        check("mcp_config", placeholder_ok, "结构合法且仅占位符" if placeholder_ok else "发现疑似真实 Token")
    except Exception as e:
        check("mcp_config", False, str(e))

    # 4. 全仓库 Token 扫描
    token_pat = re.compile(r"Bearer\s+[A-Za-z0-9_\-]{20,}")
    hits = []
    for p in walk_files(root):
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in token_pat.finditer(text):
            if "YOUR_MCP_TOKEN" not in m.group(0) and "${" not in m.group(0):
                hits.append(f"{os.path.relpath(p, root)}: {m.group(0)[:30]}...")
    check("no_token_leak", not hits, f"{len(hits)} 处疑似泄漏" if hits else "零命中")

    # 5. 禁用词扫描（仅检查面向用户的文案文件；列出禁用词表的文件自身豁免）
    word_files = ["SKILL.md", "README.md", "references/interaction_flows.md",
                  "references/compliance_wording.md", "references/emotion_palette.json"]
    banned_hits = []
    for wf in word_files:
        p = os.path.join(root, wf)
        if not os.path.exists(p):
            continue
        text = open(p, encoding="utf-8", errors="ignore").read()
        if wf.endswith("compliance_wording.md"):
            continue  # 禁用词表文档列出禁用词是合法的
        if wf == "SKILL.md":
            # 豁免「合规红线」章节（该章节本身在列举禁用词），只扫描该章节之前的内容
            marker = "## 合规红线"
            if marker in text:
                text = text.split(marker)[0]
        for w in BANNED_WORDS:
            if w in text:
                banned_hits.append(f"{wf}: {w}")
    check("no_banned_words", not banned_hits, f"命中: {banned_hits}" if banned_hits else "零命中")

    # 6. references JSON
    ref_files = ["emotion_palette.json", "nutrition_bands.json", "allergen_map.json",
                 "name_alias.json", "food_experience_map.json", "set_meals.json", "price_ref.json"]
    ref_errs = []
    for rf in ref_files:
        try:
            json.load(open(ref_path(rf), encoding="utf-8"))
        except Exception as e:
            ref_errs.append(f"{rf}: {e}")
    check("references_valid", not ref_errs, "; ".join(ref_errs) if ref_errs else f"{len(ref_files)} 个全部合法")

    # 6a. references JSON 重复键检测（JSON 解析器对重复键静默取最后值，必须源头拦截）
    # 用 object_pairs_hook 在解析期捕获任意层级的真实重复键（正则会误报嵌套公共字段名）
    dup_errs = []
    for rf in ref_files:
        found = []

        def hook(pairs):
            seen = set()
            for k, _ in pairs:
                if k in seen:
                    found.append(k)
                seen.add(k)
            return dict(pairs)

        try:
            json.load(open(ref_path(rf), encoding="utf-8"), object_pairs_hook=hook)
        except Exception as e:
            dup_errs.append(f"{rf}: 解析失败 {e}")
            continue
        if found:
            dup_errs.append(f"{rf}: 重复键 {sorted(set(found))}")
    check("no_dup_keys", not dup_errs, "; ".join(dup_errs) if dup_errs else "零重复键")

    # 6b. 套餐库结构校验（v2）
    sm_errs = []
    try:
        sm = json.load(open(ref_path("set_meals.json"), encoding="utf-8"))
        meals = sm.get("meals", [])
        if len(meals) < 5:
            sm_errs.append(f"套餐库仅 {len(meals)} 条（建议 ≥5）")
        for m in meals:
            for f in ["name", "composition", "nutrition", "price_ref", "original_price_ref", "tags", "periods"]:
                if f not in m:
                    sm_errs.append(f"{m.get('name', '?')}: 缺字段 {f}")
            n = m.get("nutrition", {})
            if not (n.get("kcal") and n.get("protein_g")):
                sm_errs.append(f"{m.get('name', '?')}: nutrition 不完整")
            if m.get("price_ref") and m.get("original_price_ref") and m["price_ref"] >= m["original_price_ref"]:
                sm_errs.append(f"{m.get('name', '?')}: 套餐价不低于原价，性价比存疑")
            for s in m.get("swappable", []):
                if not (s.get("from") and s.get("to") and s.get("kcal_delta") is not None):
                    sm_errs.append(f"{m.get('name', '?')}: swappable 条目不完整 {s}")
        pr = json.load(open(ref_path("price_ref.json"), encoding="utf-8"))
        if len(pr.get("prices", {})) < 20:
            sm_errs.append("price_ref 单品数量不足 20")
        if not pr.get("category_default"):
            sm_errs.append("price_ref 缺 category_default")
    except Exception as e:
        sm_errs.append(str(e))
    check("set_meals_valid", not sm_errs, "; ".join(sm_errs) if sm_errs else "套餐库与价格表结构完整")

    # 7. scripts 语法
    syntax_errs = []
    for f in os.listdir(os.path.join(root, "scripts")):
        if f.endswith(".py"):
            try:
                py_compile.compile(os.path.join(root, "scripts", f), doraise=True)
            except py_compile.PyCompileError as e:
                syntax_errs.append(f"{f}: {e}")
    check("scripts_syntax", not syntax_errs, "; ".join(syntax_errs) if syntax_errs else "全部可编译")

    # 8. 引擎端到端冒烟（需要营养缓存；无缓存时构造最小样例）
    smoke_ok, smoke_detail = run_smoke()
    check("engine_smoke", smoke_ok, smoke_detail)

    # 输出
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    all_pass = all(r["status"] == PASS for r in results)
    for r in results:
        out.write(f"[{r['status']}] {r['check']}: {r['detail']}\n")
    out.write("===SELF_CHECK_" + ("PASS===" if all_pass else "FAIL===") + "\n")
    sys.exit(0 if all_pass else 1)


def run_smoke():
    """用真实营养缓存（或最小样例）跑 6 组情绪×训练场景。"""
    nut = data_path("nutrition_cache.json")
    if not os.path.exists(nut):
        return False, "营养缓存不存在（先跑 M1 数据管道）"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    py = sys.executable
    engine = os.path.join(root_dir(), "scripts", "recommend_engine.py")
    scenarios = [
        ("celebrate", "力量", "减脂"), ("energized", "有氧", "增肌"), ("cozy", "休息日", "减脂"),
        ("steady", "力量", "保持"), ("share", "休息日", "无"), ("explore", "有氧", "保持"),
        # P3-6：补下午茶（历史缺陷高发时段）与早餐（套餐补录后新场景）
        ("cozy", "休息日", "减脂", "afternoon"), ("steady", "休息日", "保持", "breakfast"),
    ]
    ok_count, details = 0, []
    total = len(scenarios)
    for sc in scenarios:
        mood, training, goal, period = (sc + ("lunch",))[:4]
        cmd = [py, engine, "--mood", mood, "--training", training, "--goal", goal, "--period", period]
        try:
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=60, cwd=root_dir())
            if r.returncode == 0:
                data = json.loads(r.stdout.decode("utf-8"))
                n = len(data.get("candidates", []))
                top3 = data.get("candidates", [])[:3]
                s3 = sum(1 for c in top3 if c.get("kind") == "set_meal")
                ok_count += 1
                details.append(f"{mood}/{training}/{goal}/{period}:{n}cand/前3套餐{s3}")
            else:
                details.append(f"{mood}/{training}/{goal}/{period}:exit{r.returncode}")
        except Exception as e:
            details.append(f"{mood}: {type(e).__name__}")

    # P3-6：团餐模式冒烟（预算校验链路）
    try:
        rp = subprocess.run([py, engine, "--mode", "party", "--purpose", "部门团建",
                             "--headcount", "8", "--budget-per-head", "25"],
                            capture_output=True, env=env, timeout=60, cwd=root_dir())
        if rp.returncode == 0:
            pd = json.loads(rp.stdout.decode("utf-8"))
            c0 = pd["candidates"][0]
            est = c0.get("price_est_per_head", 0)
            if est <= 25 * 1.15:
                ok_count += 1
                total += 1
                details.append(f"party/8人/人均25:est¥{est} 预算内")
            else:
                total += 1
                details.append(f"party/8人/人均25:est¥{est} 超预算未拦截")
        else:
            total += 1
            details.append(f"party:exit{rp.returncode}")
    except Exception as e:
        total += 1
        details.append(f"party: {type(e).__name__}")
    return ok_count == total, f"{ok_count}/{total} 组通过 ({'; '.join(details)})"


def root_dir():
    return skill_root()


if __name__ == "__main__":
    main()
