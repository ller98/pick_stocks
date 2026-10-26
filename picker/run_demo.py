#!/usr/bin/env python3
"""run_demo.py - 离线可复现演示：不依赖任何外部数据后端 / 密钥。

流程：
  1. 读取 data/screener_raw.json（示例初选，纯数据，无个人隐私）
  2. 跑粗筛四道闸（coarse_filter.apply_coarse_filters）→ 产出 data/pool.json
  3. 演示择时（quantify_market）与动量映射（momentum_score）等核心数学

运行：python -m picker.run_demo
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from coarse_filter import apply_coarse_filters, mk  # noqa: E402
from board_filter import load_excluded_boards  # noqa: E402
from data_provider import SampleProvider  # noqa: E402
import factors  # noqa: E402


def main():
    provider = SampleProvider()
    raw = provider.screen()
    sw_map = factors.load_json(os.path.join(os.path.dirname(HERE), "data", "sw_map.json"), {}) or {}

    growth = [mk(x, "growth", sw_map=sw_map) for x in raw.get("growth", [])]
    cyclical = [mk(x, "cyclical", sw_map=sw_map) for x in raw.get("cyclical", [])]
    excluded = load_excluded_boards()
    max_per = int(raw.get("coarse_max_per_sector", 8))

    print("=" * 60)
    print("离线 demo · 量化选股粗筛（数据来自 data/screener_raw.json）")
    print("=" * 60)
    pool = apply_coarse_filters(growth, cyclical, max_per, excluded=excluded, sw_map=sw_map)
    print(f"初选：成长线 {len(growth)} 只 / 周期线 {len(cyclical)} 只")
    print(f"第0道 板块权限过滤：剔除 {len(pool['board_filtered_out'])} 只 -> "
          f"{[b['name'] for b in pool['board_filtered_out']]}")
    print(f"第1道 行业超配剔除：{len(pool['sector_over_quota_dropped'])} 只")
    print(f"第2/3道 PEG>1 / 增速<20% 剔除：{len(pool['_drop_peg'])} / {len(pool['_drop_growth'])} 只")
    print(f"粗筛后候选：{len(pool['coarse_filtered'])} 只")

    print("\n--- 择时核心（quantify_market）演示 ---")
    for label, srow in (
        ("中性市", {"VALUATION_SCORE": 3, "SENTIMENT_SCORE": 3,
                    "VOLUME_ENERGE_SCORE": 3, "STOCK_WIDTH_SCORE": 55}),
        ("过热市", {"VALUATION_SCORE": 5, "SENTIMENT_SCORE": 5,
                    "VOLUME_ENERGE_SCORE": 4, "STOCK_WIDTH_SCORE": 70}),
        ("恐慌但估值仍高(下跌中继)", {"VALUATION_SCORE": 4, "SENTIMENT_SCORE": 2,
                                     "VOLUME_ENERGE_SCORE": 2, "STOCK_WIDTH_SCORE": 10}),
    ):
        m = factors.quantify_market(srow)
        print(f"  {label:<16} heat={m['heat']:<5} 区间={m['band']:<12} "
              f"建议仓位={m['decision']['position_range']}")

    print("\n--- 动量映射（momentum_score）演示 ---")
    for mom in (0.6, 0.5, 0.1, -0.3, None):
        print(f"  mom={str(mom):<5} -> 动量分={factors.momentum_score(mom)}")

    # 落盘
    out_path = os.path.join(os.path.dirname(HERE), "data", "pool.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in pool.items() if not k.startswith("_")},
                  f, ensure_ascii=False, indent=2)
    print(f"\n已写出候选池：{out_path}")


if __name__ == "__main__":
    main()
