"""板块权限过滤模块（量化选股模型公用件）

用途：根据 _board_config.json 中的 excluded_boards 配置，在粗筛阶段自动剔除
      账户无交易权限的板块标的（如创业板 300/301）。

设计要点：
  1. 配置驱动——开通权限后只需改 _board_config.json，不动任何脚本代码。
  2. 代码前缀兼容——同时支持 "002440" / "sz002440" / "SZ002440" / "002440.SZ" 三种写法。
  3. 供多处复用——_build_pool.py（每日重写）、_coarse_filter.py、run_quant_model_mx.py
     均可 import，形成多层保险，避免任一环节漏过。

对外接口：
  classify_board(code)             -> 'main' | 'chinext' | 'star' | 'bse' | 'unknown'
  load_excluded_boards()           -> set，当前禁用板块集合
  is_blocked(code, excluded=None)  -> bool，该代码是否应被剔除
  filter_stocks(stocks, ...)       -> (kept, removed)，批量过滤股票 dict 列表
"""

import json
import os
import re

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(WORKSPACE, "_board_config.json")

BOARD_LABELS = {
    "main": "主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
    "unknown": "未知板块",
}


def normalize_code(code):
    """剥离交易所前缀/后缀，返回纯 6 位数字代码。

    兼容：002440 / sz002440 / SZ002440 / 002440.SZ / sh.600016
    """
    if code is None:
        return ""
    s = str(code).strip().upper()
    # 去掉 .SZ / .SH / .BJ 之类后缀
    s = re.sub(r"\.(SZ|SH|BJ|SS)$", "", s)
    # 去掉 SZ / SH / BJ 前缀（可能带点，如 sh.600016）
    s = re.sub(r"^(SZ|SH|BJ|SS)\.?", "", s)
    m = re.search(r"(\d{6})", s)
    return m.group(1) if m else s


def classify_board(code):
    """按代码判定所属板块。"""
    c = normalize_code(code)
    if not re.fullmatch(r"\d{6}", c):
        return "unknown"
    if c.startswith(("300", "301")):
        return "chinext"
    if c.startswith(("688", "689")):
        return "star"
    # 北交所：920 段，以及 430/83x/87x/88x 老三板转板段
    if c.startswith("920") or c.startswith("430") or c[0] == "8":
        return "bse"
    # 主板/中小板：600 601 603 605 606 / 000 001 002 003
    if c.startswith(("600", "601", "603", "605", "606", "000", "001", "002", "003")):
        return "main"
    return "unknown"


def load_excluded_boards():
    """读取配置中的禁用板块集合。配置缺失时返回空集合（不过滤）。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return set(cfg.get("excluded_boards") or [])
    except Exception:
        return set()


def is_blocked(code, excluded=None):
    """判定单个代码是否应被剔除。"""
    if excluded is None:
        excluded = load_excluded_boards()
    if not excluded:
        return False
    return classify_board(code) in excluded


def board_label(code):
    return BOARD_LABELS.get(classify_board(code), "未知板块")


def filter_stocks(stocks, code_key="code", excluded=None, verbose=False, stage=""):
    """批量过滤股票列表（每项为含 code 字段的 dict）。

    返回 (kept, removed)。removed 中每项附加 _blocked_board 字段说明原因。
    """
    if excluded is None:
        excluded = load_excluded_boards()
    kept, removed = [], []
    if not excluded:
        return list(stocks), []
    for s in stocks:
        code = s.get(code_key) if isinstance(s, dict) else s
        board = classify_board(code)
        if board in excluded:
            if isinstance(s, dict):
                s = dict(s)
                s["_blocked_board"] = board
            removed.append(s)
        else:
            kept.append(s)
    if verbose and removed:
        tag = f"[{stage}] " if stage else ""
        labels = "/".join(BOARD_LABELS.get(b, b) for b in sorted(excluded))
        print(f"{tag}板块权限过滤（排除 {labels}）：剔除 {len(removed)} 只")
        for s in removed:
            nm = s.get("name", "?") if isinstance(s, dict) else "?"
            cd = s.get(code_key, "?") if isinstance(s, dict) else s
            bd = BOARD_LABELS.get(s.get("_blocked_board"), "?") if isinstance(s, dict) else "?"
            print(f"    - {cd} {nm} [{bd}]")
    return kept, removed


if __name__ == "__main__":
    excluded = load_excluded_boards()
    labels = "、".join(BOARD_LABELS.get(b, b) for b in sorted(excluded)) or "（无）"
    print(f"当前禁用板块：{labels}")
    print()
    samples = [
        "002440", "sz301219", "300773", "688111", "920599",
        "sh600016", "430418", "001218", "605599",
    ]
    print("代码判定自检：")
    for c in samples:
        mark = "剔除" if is_blocked(c, excluded) else "保留"
        print(f"  {c:>10} -> {board_label(c):<6} {mark}")
