#!/usr/bin/env python3
"""_fetch_sw_westock.py - 用 westock-data CLI 批量补齐申万一级行业映射（带增量缓存）

用途：westock-mcp 的 tool_filter 返回结果不含行业字段，但主脚本的 is_cyclical 判断、
      粗筛的行业集中度去重、_sector_filter 的同板块去重都依赖申万行业。
      本脚本调用 `westock-data profile <codes>` 批量补齐，全程走 westock 生态，不依赖 mx。

数据来源：_screener_raw.json（由 agent 每日写入 tool_filter 原始结果）
输出：    _sw_map.json  { "600016": "银行", ... }（key 为无前缀纯代码）

增量缓存：已存在于 _sw_map.json 的代码不再重复查询，只补新增标的。
          首日全量 125 只约需 5 批 / 1 分钟；次日通常仅需补 10-30 只。
用法：
    python _fetch_sw_westock.py            # 增量补齐
    python _fetch_sw_westock.py --force    # 全量重拉（行业分类调整后使用）
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_FILE = os.path.join(WORK_DIR, "_screener_raw.json")
MAP_FILE = os.path.join(WORK_DIR, "_sw_map.json")
CHUNK = 25
CLI = "npx -y westock-data-skillhub@1.0.5"


def with_prefix(code):
    """纯 6 位代码 -> westock 带市场前缀代码"""
    c = str(code).strip()
    if re.match(r"^(sh|sz|bj)\d{6}$", c):
        return c
    if c.startswith(("60", "68", "90")):
        return "sh" + c
    if c.startswith(("00", "30", "20")):
        return "sz" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    return "sz" + c


def load_raw():
    if not os.path.exists(RAW_FILE):
        sys.exit(f"缺少 {RAW_FILE}：请先由 agent 写入 westock tool_filter 初选结果")
    with open(RAW_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def run_chunk(codes):
    cmd = f'{CLI} profile {",".join(codes)}'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=WORK_DIR, timeout=180)
    return res.stdout or ""


def parse_table(text):
    """解析 CLI 输出的 markdown 表格，返回 {带前缀code: 申万一级行业}"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        code, industry = cells[0], cells[5]
        if code in ("code", "") or set(code) <= set("-: "):
            continue
        if not re.match(r"^(sh|sz|bj)\d{6}$", code):
            continue
        out[code] = industry
    return out


def _norm_code(c):
    c = str(c).strip()
    return c[2:] if len(c) >= 8 and c[:2] in ("sh", "sz", "bj") else c


def main():
    force = "--force" in sys.argv
    raw = load_raw()
    codes = [s["code"] for s in raw.get("growth", [])] + [s["code"] for s in raw.get("cyclical", [])]
    codes = list(dict.fromkeys(str(c).strip() for c in codes))
    print(f"初选池共 {len(codes)} 只（成长 {len(raw.get('growth', []))} + 周期 {len(raw.get('cyclical', []))}）")

    cache = {}
    if os.path.exists(MAP_FILE) and not force:
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"已有行业缓存 {len(cache)} 条")

    todo = [c for c in codes if _norm_code(c) not in cache]
    if force:
        todo = codes
        print("--force：全量重拉")
    print(f"待查询 {len(todo)} 只（命中缓存 {len(codes) - len(todo)} 只）")

    failed = []
    if todo:
        pref = [with_prefix(c) for c in todo]
        total_batch = (len(pref) + CHUNK - 1) // CHUNK
        for i in range(0, len(pref), CHUNK):
            chunk = pref[i:i + CHUNK]
            print(f"  [{i // CHUNK + 1}/{total_batch}] 查询 {len(chunk)} 只 ...", end=" ", flush=True)
            try:
                got = parse_table(run_chunk(chunk))
                for k, v in got.items():
                    cache[k[2:]] = v
                print(f"成功 {len(got)}/{len(chunk)}")
                miss = [c for c in chunk if c not in got]
                if miss:
                    print(f"    未返回: {miss}")
                    failed.extend(miss)
            except Exception as e:
                print(f"失败: {e}")
                failed.extend(chunk)
            time.sleep(0.5)

    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    still_missing = [c for c in codes if _norm_code(c) not in cache]
    print(f"\n行业映射累计 {len(cache)} 条；本池缺失 {len(still_missing)} 只")
    if still_missing:
        print(f"⚠️ 缺失清单（将标记为『未知』，不参与行业去重）: {still_missing}")
    if failed:
        print(f"查询失败: {failed}")
    print(f"已写入 {MAP_FILE}")

    cnt = Counter(cache.get(c, "未知") for c in codes)
    print("\n本池行业分布（申万一级）:")
    for ind, n in cnt.most_common():
        print(f"  {ind}: {n}")

    return 0 if not still_missing else 0


if __name__ == "__main__":
    sys.exit(main())
