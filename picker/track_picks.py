#!/usr/bin/env python3
"""track_picks.py - 记录每日选股结果到 _pick_history.jsonl
在 _quant_summary.json 生成后调用，把通过风险筛查的标的追加写入历史记录。
"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from board_filter import classify_board, load_excluded_boards

WORK_DIR = Path(__file__).parent
SUMMARY_FILE = WORK_DIR / "_quant_summary.json"
HISTORY_FILE = WORK_DIR / "_pick_history.jsonl"


def classify_tier(name, pb, code="", industry=""):
    """分类：bank(金融) / cyclical / growth
    银行与 非银金融(券商/保险/信托/期货/金控) 归 bank(金融) 组，
    避免券商被误算为成长、污染成长组胜率。
    """
    if "银行" in name or industry.startswith("非银金融") or any(
            k in name for k in ("证券", "券商", "保险", "期货", "信托", "财富", "金控")):
        return "bank"
    if pb and pb < 0.8:
        return "cyclical"
    return "growth"


def main():
    if not SUMMARY_FILE.exists():
        print("[track] _quant_summary.json not found, skip")
        return

    summary = json.load(open(SUMMARY_FILE, encoding="utf-8"))
    date = summary.get("date", "")
    close_date = summary.get("close_date", "")

    pe_pct = summary.get("pe_pct_10y", 0)
    sentiment = summary.get("sentiment", "")
    position = summary.get("position", "")
    action = summary.get("action", "")
    market_adj_score = summary.get("market_adj_score", 0)

    # 仅跟踪模型"最终备选"(final_picks)：已包含同板块去重/行业截断后的实际推荐列表，
    # 避免把同板块多只(如8只银行)全部计入胜率，造成"假分散"误导(2026-08-31 新增)
    fp_codes = set()
    for s in summary.get("final_picks", []):
        c = (s.split()[0] if isinstance(s, str) and s.split() else "")
        if c:
            fp_codes.add(c)

    # 先读取已有记录，避免重复写入同一天的同一只股票
    existing = set()
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    existing.add((rec.get("date", ""), rec.get("code", "")))
                except json.JSONDecodeError:
                    continue

    records = []
    for d in summary.get("detail", []):
        if d.get("status") in ("pass", "warn"):
            if fp_codes and d["code"] not in fp_codes:
                continue
            key = (date, d["code"])
            if key in existing:
                continue
            tier = classify_tier(d.get("name", ""), d.get("PB"), d.get("code", ""), d.get("industry", ""))
            # 每只票用自身收盘日作为入场日，避免被偷换成全池第一只票的 close_date
            cd = d.get("close_date") or close_date or date
            _board = classify_board(d["code"])
            rec = {
                "date": date,
                "close_date": cd,
                "tier": tier,
                "board": _board,
                "tradable": _board not in load_excluded_boards(),
                "code": d["code"],
                "name": d["name"],
                "industry": d.get("industry", ""),
                "pe_ttm": round(d.get("PE_TTM", 0), 2),
                "pb": round(d.get("PB", 0), 3),
                "peg": round(d.get("peg", 0), 4) if d.get("peg") else None,
                "growth": round(d.get("np_deduct_growth", 0), 2) if d.get("np_deduct_growth") else None,
                "roe_ttm": round(d.get("ROE_TTM", 0), 2),
                "reasons": d.get("reasons", []),
                "pe_pct_10y": pe_pct,
                "sentiment": sentiment,
                "position": position,
                "action": action,
                "market_adj_score": market_adj_score,
                # 因子记录字段（record_only，不参与评分/判定），供后续回溯验证
                "mom_score": d.get("mom_score"),
                "mom": d.get("mom"),
                "rsi_6": d.get("rsi_6"),
                "kdj_j": d.get("kdj_j"),
                "tech_date": d.get("tech_date"),
            }
            records.append(rec)

    if not records:
        print(f"[track] {date}: no new picks to record (already tracked)")
        return

    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[track] {date}: {len(records)} picks appended to {HISTORY_FILE}")


if __name__ == "__main__":
    main()
