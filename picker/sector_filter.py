#!/usr/bin/env python3
"""_sector_filter.py - 同板块去重（申万一级行业，每板块仅保留综合最优 N 只）

设计目标（2026-08-31）：
- 解决"假分散"问题：一日入选里 8 只银行实为同一笔板块赌注，应只留 1 只代表。
- 配置驱动：规则开关/每板块上限/粒度全在 `_sector_config.json`，不改代码即可调整。
- 仅作用于最终备选/推荐列表（stage=final），初选池保留全量供分析。
- 保留标准 keep_criterion=composite：pass 优先 → 估值最优(成长 PEG 低 / 周期 PB 低) → ROE 最高。

用法（在 run_quant_model_mx.py 的 final_all 生成后调用）：
    from sector_filter import dedup_by_sector, load_sector_config
    cfg = load_sector_config()
    if cfg.get("enabled"):
        final_all, dropped = dedup_by_sector(final_all, max_per_sector=cfg.get("max_per_sector", 1), verbose=True, stage="最终备选")
"""
import json
from collections import defaultdict
from pathlib import Path

WORK_DIR = Path(__file__).parent
CONFIG_FILE = WORK_DIR.parent / "config" / "sector_config.json"

DEFAULTS = {
    "enabled": False,
    "granularity": "sw_level1",
    "max_per_sector": 1,
    "keep_criterion": "composite",
    "stage": "final",
}


def load_sector_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
            merged = dict(DEFAULTS)
            merged.update(cfg)
            return merged
        except Exception as e:
            print(f"[sector] 读取 {CONFIG_FILE} 失败：{e}，使用默认关闭")
    return dict(DEFAULTS)


def sector_l1(industry):
    """申万一级行业：取 industry 字段第一段（如 '基础化工-化学原料-其他化学原料' -> '基础化工'）。"""
    if not industry:
        return "未知"
    return industry.split("-")[0].strip() or "未知"


def _status_rank(status):
    return {"pass": 0, "warn": 1, "reject": 2}.get(status, 2)


def composite_rank(r):
    """综合最优排序键（升序，越小越优）：
    pass 优先(0) > warn(1) > reject(2)；
    估值最优：成长线(非周期)用 PEG，周期/银行/非银用 PB（遵循模型约束#2：周期不硬算PEG）；
    ROE 越高越好（用 -ROE 使升序时高 ROE 排前）。
    注意：周期股可能仍被 assess 算出 PEG（低基数增长假象），必须按 is_cyclical 分流，
    不能仅看 peg 是否非空，否则会被虚假低 PEG 误导。
    """
    srank = _status_rank(r.get("status", "reject"))
    is_cycl = r.get("is_cyclical", False)
    peg = r.get("peg")
    pb = r.get("PB")
    if is_cycl:
        # 周期/银行/非银：只用 PB 评估
        primary = pb if pb is not None else 999
    else:
        # 成长/制造线：用 PEG；无 PEG 时回退 PB
        primary = peg if (peg is not None) else (pb if pb is not None else 999)
    roe = r.get("ROE_TTM") or 0
    return (srank, primary, -roe)


def dedup_by_sector(stocks, code_key="code", industry_key="industry",
                    max_per_sector=1, verbose=False, stage=""):
    """按申万一级行业去重，每板块保留综合最优 max_per_sector 只。

    返回 (kept, removed)；removed 项附加 _sector_dropped 字段（被剔除时所属板块）。
    """
    groups = defaultdict(list)
    for r in stocks:
        sec = sector_l1(r.get(industry_key, ""))
        groups[sec].append(r)

    kept, removed = [], []
    for sec, items in groups.items():
        if max_per_sector is None or max_per_sector <= 0:
            kept.extend(items)
            continue
        ranked = sorted(items, key=composite_rank)
        for i, r in enumerate(ranked):
            if i < max_per_sector:
                kept.append(r)
            else:
                rr = dict(r)
                rr["_sector_dropped"] = sec
                removed.append(rr)

    if verbose:
        by_sec = defaultdict(list)
        for r in removed:
            by_sec[r["_sector_dropped"]].append(r.get("name", ""))
        for sec, names in by_sec.items():
            print(f"[sector] {stage} 同板块({sec})去重剔除 {len(names)} 只：{', '.join(names)}")
    return kept, removed


if __name__ == "__main__":
    # 自检：构造 3 只银行 + 2 只化工，验证每板块仅留 1 且留综合最优
    sample = [
        {"code": "sh601166", "name": "兴业银行", "status": "pass", "industry": "银行", "PB": 0.55, "ROE_TTM": 12.0, "peg": None},
        {"code": "sh601398", "name": "工商银行", "status": "pass", "industry": "银行", "PB": 0.50, "ROE_TTM": 11.0, "peg": None},
        {"code": "sh600036", "name": "招商银行", "status": "warn", "industry": "银行", "PB": 0.95, "ROE_TTM": 15.0, "peg": None},
        {"code": "sz002648", "name": "卫星化学", "status": "pass", "industry": "基础化工-化学原料", "PB": 1.6, "ROE_TTM": 23.0, "peg": 0.066},
        {"code": "sz002440", "name": "闰土股份", "status": "pass", "industry": "基础化工-化学制品", "PB": 1.04, "ROE_TTM": 9.0, "peg": 0.12},
    ]
    k, rm = dedup_by_sector(sample, max_per_sector=1, verbose=True, stage="自检")
    print("保留：", [r["name"] for r in k])
    print("剔除：", [(r["name"], r["_sector_dropped"]) for r in rm])
    # 预期：银行留 PB 最低的工商银行(0.50)；基础化工(一级同属)留 PEG 最低的卫星化学
