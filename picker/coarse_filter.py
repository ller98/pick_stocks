#!/usr/bin/env python3
"""coarse_filter.py - 第二步粗筛：构建候选池 data/pool.json

数据与代码分离：输入是纯数据 JSON（由 DataProvider 产出），本脚本保持稳定、可单测。

输入：
  data/screener_raw.json   初选原始结果（成长线 + 周期线）
  config/screener_config.json  初选表达式 + 粗筛行业配额
  data/sw_map.json         申万一级行业映射
  config/board_config.json 禁用板块（无交易权限的板块）

输出：
  data/pool.json           含 growth_raw / cyclical_raw / coarse_filtered / board_filtered_out

流程（顺序不可调整）：
  第0道 板块权限过滤（最前置，避免无权限标的占用行业配额与 PEG 排名）
  第1道 行业集中度去重（每行业最多 N 只）
  第2道 PEG 合理性过滤（PEG > 1 剔除）
  第3道 增速质量过滤（增速 < 20% 剔除）

用法：
  python -m picker.coarse_filter              # 正常粗筛（读取 data/ 下输入）
  COARSE_MAX_PER=3 python -m picker.coarse_filter  # 临时覆盖行业配额做敏感性验证

模块级纯函数（mk / sector_key / dedup_by_sector / apply_coarse_filters）可被单元测试直接 import，
不触发任何文件 I/O。
"""
import json
import os
import subprocess
import sys
from collections import defaultdict

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 确保同目录 board_filter 可导入
sys.path.insert(0, WORK_DIR)
from board_filter import filter_stocks, load_excluded_boards  # noqa: E402

RAW_FILE = os.path.join(WORK_DIR, "data", "screener_raw.json")
CFG_FILE = os.path.join(WORK_DIR, "config", "screener_config.json")
MAP_FILE = os.path.join(WORK_DIR, "data", "sw_map.json")
POOL_FILE = os.path.join(WORK_DIR, "data", "pool.json")

# 模块级行业映射（测试可注入），由 main() 在运行前填充
SW_MAP = {}


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _sw_lookup(code):
    """兼容 sw_map 两种键格式（带前缀 / 无前缀）"""
    c = str(code).strip()
    if c in SW_MAP:
        return SW_MAP[c]
    if len(c) >= 8 and c[:2] in ("sh", "sz", "bj"):
        return SW_MAP.get(c[2:], "未知")
    return "未知"


def mk(item, layer, sw_map=None):
    """把初选条目转换为内部统一结构，并附带 PEG（成长线）。"""
    if sw_map is not None:
        _sw_lookup.__globals__["SW_MAP"] = sw_map
    code = str(item["code"]).strip()
    pe = item.get("pe")
    growth = item.get("growth")
    peg = None
    if layer == "growth" and growth and growth > 0 and pe and pe > 0:
        peg = round(pe / growth, 4)
    return {
        "code": code,
        "name": item.get("name", ""),
        "sw": item.get("sw") or _sw_lookup(code),
        "pe": pe,
        "pb": item.get("pb"),
        "growth": growth,
        "peg": peg,
        "layer": layer,
    }


def sector_key(sw):
    """兼容两种行业粒度：mx 三级『有色金属-工业金属-铝』取三级；westock 一级直接用"""
    parts = str(sw).split("-")
    return parts[2] if len(parts) >= 3 else parts[0]


def dedup_by_sector(stocks, max_per, sort_key):
    """同行业按 sort_key 排序后仅保留前 max_per 只，返回 (kept, dropped)。"""
    groups = defaultdict(list)
    for s in stocks:
        groups[sector_key(s["sw"])].append(s)
    kept, dropped = [], []
    for sector, items in groups.items():
        items.sort(key=sort_key)
        kept.extend(items[:max_per])
        dropped.extend(items[max_per:])
    return kept, dropped


def apply_coarse_filters(growth_raw, cyclical_raw, max_per, excluded=None, sw_map=None):
    """纯函数：对成长/周期两条线执行第0~3道过滤，返回 pool 字典。

    不读写文件，便于单元测试。excluded 为禁用板块集合（None 表示不过滤）。
    """
    if sw_map is not None:
        _sw_lookup.__globals__["SW_MAP"] = sw_map

    # 第0道：板块权限过滤（最前置）
    g_f, g_removed = filter_stocks(growth_raw, excluded=excluded, stage="成长线")
    c_f, c_removed = filter_stocks(cyclical_raw, excluded=excluded, stage="周期线")
    board_filtered_out = [{"code": s["code"], "name": s["name"],
                           "board": s.get("_blocked_board", "?")}
                          for s in g_removed + c_removed]

    # 第1道：行业集中度去重
    growth_dedup, g_drop = dedup_by_sector(
        g_f, max_per, sort_key=lambda s: (s.get("peg") if s.get("peg") is not None else 999,))
    cyclical_dedup, c_drop = dedup_by_sector(
        c_f, max_per, sort_key=lambda s: (s.get("pb") if s.get("pb") is not None else 999,))

    # 第2、3道：PEG 合理性 + 增速质量
    coarse_filtered, drop_peg, drop_growth = [], [], []
    for s in growth_dedup:
        if s.get("peg") is not None and s["peg"] > 1:
            drop_peg.append(s["name"])
            continue
        if s.get("growth") is not None and s["growth"] < 20:
            drop_growth.append(s["name"])
            continue
        coarse_filtered.append(dict(s))
    for s in cyclical_dedup:
        if s.get("pe") is not None and s["pe"] <= 0:
            continue
        coarse_filtered.append(dict(s))

    return {
        "coarse_max_per_sector": max_per,
        "growth_raw": g_f,
        "cyclical_raw": c_f,
        "coarse_filtered": coarse_filtered,
        "board_filtered_out": board_filtered_out,
        "sector_over_quota_dropped": [{"code": s["code"], "name": s["name"], "sw": s["sw"]}
                                      for s in g_drop + c_drop],
        "_drop_peg": drop_peg,
        "_drop_growth": drop_growth,
    }


def main():
    cfg = load_json(CFG_FILE, {}) or {}
    max_per = int(os.environ.get("COARSE_MAX_PER", cfg.get("coarse_max_per_sector", 8)))

    raw = load_json(RAW_FILE)
    if not raw:
        sys.exit(f"❌ 缺少 {RAW_FILE}\n"
                 f"   请先由 DataProvider 产出初选结果并写入该文件。\n"
                 f"   表达式见 config/screener_config.json。")
    gen_at = raw.get("generated_at", "")
    source = raw.get("source", cfg.get("data_source", "westock-mcp tool_filter"))
    print(f"数据源: {source} | 初选日期: {gen_at} | 粗筛行业配额 max_per={max_per}")

    # 补齐申万行业（增量，自动调用 fetch_sw.py）
    global SW_MAP
    codes_needed = [str(s["code"]) for s in raw.get("growth", [])] + \
                   [str(s["code"]) for s in raw.get("cyclical", [])]
    SW_MAP = load_json(MAP_FILE, {}) or {}
    missing = [c for c in codes_needed if _sw_lookup(c) == "未知"]
    if missing:
        print(f"行业映射缺 {len(missing)} 只，自动调用 fetch_sw.py 补齐 ...")
        try:
            subprocess.run([sys.executable, os.path.join(WORK_DIR, "picker", "fetch_sw.py")],
                           cwd=WORK_DIR, timeout=900, check=False)
            SW_MAP = load_json(MAP_FILE, {}) or {}
        except Exception as e:
            print(f"⚠️ 行业补齐失败（{e}），缺失标的将标记『未知』")
    still_missing = [c for c in codes_needed if _sw_lookup(c) == "未知"]
    if still_missing:
        print(f"⚠️ 仍缺行业映射 {len(still_missing)} 只: {still_missing[:10]}")

    growth_raw = [mk(x, "growth") for x in raw.get("growth", [])]
    cyclical_raw = [mk(x, "cyclical") for x in raw.get("cyclical", [])]
    print(f"原始: 成长线 {len(growth_raw)} 只, 周期线 {len(cyclical_raw)} 只")

    excluded = load_excluded_boards()
    print(f"禁用板块: {excluded}")
    pool = apply_coarse_filters(growth_raw, cyclical_raw, max_per, excluded=excluded)
    pool["generated_at"] = gen_at
    pool["source"] = source

    g_drop = pool["sector_over_quota_dropped"]
    print(f"板块过滤后剔除 {len(pool['board_filtered_out'])} 只；"
          f"行业超配剔除 {len(g_drop)} 只")
    print(f"PEG>1 剔除 {len(pool['_drop_peg'])} 只, 增速<20% 剔除 {len(pool['_drop_growth'])} 只")
    print(f"粗筛后共 {len(pool['coarse_filtered'])} 只")

    out = {k: v for k, v in pool.items() if not k.startswith("_")}
    with open(POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"已写入 {POOL_FILE}")


if __name__ == "__main__":
    main()
