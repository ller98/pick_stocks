#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量化选股模型 - 全市场初选版（westock-mcp 驱动，mx-ds-mcp 备用）
背景：2026-08-12 起 westock-tool 下架，第一层全市场初选改用「东方财富妙想 MCP」的
      mx_stocks_screener 自然语言多条件筛选（等价替代 filter）。
分工：agent 调用 mx_stocks_screener 完成「发现层」，结果写入 _mx_pool.json；
      本脚本读取 _mx_pool.json，按行业分流（周期股→PB / 成长制造→PEG），
      再用 westock-data（CLI）做财务复核 + 风险筛查 + 市场环境研判 + 报告生成。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 确保同目录 board_filter / sector_filter 可导入
from board_filter import BOARD_LABELS, filter_stocks, load_excluded_boards
from sector_filter import dedup_by_sector, load_sector_config, sector_l1

WORKSPACE = os.environ.get("QUANT_WS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 报告模板：豆包环境用环境变量 REPORT_TEMPLATE 覆盖；否则优先用原绝对路径，
# 再回退到工作区内 report-template.html（随脚本一起上传即可自动识别）。
_TPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-template.html")
TEMPLATE = os.environ.get("REPORT_TEMPLATE") or (_TPL if os.path.exists(_TPL) else None)
if TEMPLATE is None:
    _local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-template.html")
    TEMPLATE = _local if os.path.exists(_local) else None
POOL_FILE = os.path.join(WORKSPACE, "_mx_pool.json")
DEGRADED_FLAG = os.path.join(WORKSPACE, "_degraded_flag.json")
REPORT_DATE = datetime.now().strftime("%Y%m%d")
REPORT_DATE_CN = datetime.now().strftime("%Y-%m-%d")

# 周期/价值股行业（→ PB 估值体系）；其余归成长/制造（→ PEG 盈利指标）
CYCLICAL_INDUSTRIES = {"银行", "非银金融", "房地产", "建筑装饰", "交通运输",
                       "公用事业", "钢铁", "建筑材料", "煤炭"}
# 商品周期行业（价格驱动，PEG 在景气高位失效，需景气分位保护 · 坑3/坑6）
COMMODITY_PEAK_INDUSTRIES = {"有色金属", "化工", "石油石化", "航运", "煤炭", "钢铁", "建筑材料"}
# 强政策/景气敏感行业（脚本化叙事护栏，扩大静态政策风险提示 · 坑15）
POLICY_RISK_INDUSTRIES = {"医药生物", "医疗器械", "光伏", "电源设备", "电池", "创新药",
                          "半导体", "农林牧渔", "房地产", "建筑材料", "钢铁", "化工", "有色金属"}
GROWTH_TOP = 20
CYCLICAL_TOP = 20
FINANCE_CHUNK = 15

# 因板块权限被兜底拦截的标的（供报告展示）
BOARD_BLOCKED = []
SECTOR_DROPPED = []


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def score_stock(r, fcfg=None):
    """对最终备选标的计算 0-100 综合评分（百分制），分行业差异化评估。

    维度与权重：估值吸引力(35) + 盈利质量(35) + 财务安全(30) + 价格成熟度惩罚(配置 points 分)。
    动量/成熟度维度：method=umd 时 compute_momentum 的 mom_score 以 points/100 加权叠加；
    method=maturity_penalty 时把 r["maturity_score"](0-100, 高=已兑现)翻转成 (100-maturity_score) 加权叠加；
    无数据时不叠加（保持纯基本面评分）。
    - 周期/价值/金融线(is_cyclical) 用 PB 估值；成长/制造线用 PEG 估值（无 PEG 退化用 PB 略打折）。
    - 盈利质量 = ROE_TTM(满分20) + 扣非增速(满分15)；低基数/不可持续高增长按 0.4 倍打折。
    - 财务安全 = 状态基础分(pass=25/warn=18) 减去各风险预警扣分（每条 2~5 分）。
    纯模型推演结果，非投资建议；明细（score_detail）供报告展示与可追溯。
    """
    is_cycl = r.get("is_cyclical", False)
    status = r.get("status", "warn")
    pb = r.get("PB") or 0
    peg = r.get("peg")
    roe = r.get("ROE_TTM") or 0
    g = r.get("np_deduct_growth")          # 倍数，1 = 零增长
    warns = r.get("warn_notes") or []
    warn_text = "｜".join(warns)

    # —— 估值分 (35) ——
    if is_cycl:
        val = _clamp((1.5 - pb) / 1.5, 0, 1) * 35
    else:
        if peg is not None:
            val = _clamp((0.8 - peg) / 0.8, 0, 1) * 35
        else:
            val = _clamp((1.5 - pb) / 1.5, 0, 1) * 35 * 0.8 + 7   # 无 PEG 退化用 PB，略打折
    val = round(val, 1)

    # —— 盈利质量分 (35) = ROE(20) + 增速(15) ——
    roe_score = _clamp(roe / 15.0, 0, 1) * 20
    low_base = any(k in warn_text for k in ("低基数恢复", "增速>500%", "两年复合仅", "单年扣非增速"))
    if g is None:
        grow_score, grow_note = 0.0, "增速数据缺失"
    elif low_base:
        grow_score = _clamp((g - 1) / 0.5, 0, 1) * 15 * 0.4    # 虚假/不可持续高增长打折
        grow_note = "低基数不可持续·已打折"
    elif g >= 1.5:
        grow_score, grow_note = 15.0, ""
    elif g >= 1.2:
        grow_score, grow_note = 11.0, ""
    elif g >= 1.0:
        grow_score, grow_note = 7.0, ""
    else:
        grow_score, grow_note = 0.0, "扣非净利负增长"
    qual = round(roe_score + grow_score, 1)

    # —— 财务安全分 (30) ——
    base = 25.0 if status == "pass" else 18.0
    penalty = 0.0
    penalty_detail = []
    for w in warns:
        if "毛利率连续下滑" in w:
            penalty += 4; penalty_detail.append("毛利率连续下滑-4")
        elif "应收增速" in w:
            penalty += 3; penalty_detail.append("应收增速异常-3")
        elif "经营现金流/净利润" in w:
            penalty += 4; penalty_detail.append("利润含金量低-4")
        elif "商品周期景气高位" in w or "PEG失效" in w:
            penalty += 5; penalty_detail.append("周期景气高位/PEG失效-5")
        elif "增速数据缺失" in w or "扣非净利润增速数据缺失" in w:
            penalty += 3; penalty_detail.append("增速数据缺失-3")
        elif "低基数恢复" in w or "增速>500%" in w:
            penalty += 4; penalty_detail.append("低基数恢复-4")
        else:
            penalty += 2; penalty_detail.append("其他预警-2")
    safe = round(_clamp(base - penalty, 0, 30), 1)

    fundamental = val + qual + safe   # 财务安全分已含 penalty 扣减
    _mom_cfg = (fcfg or {}).get("momentum", {}) or {}
    mom_method = _mom_cfg.get("method", "umd")
    mom_score = r.get("mom_score")    # 由 compute_momentum 预计算的原始 UMD 动量分（0-100 或 None），保留为记录字段
    points = _mom_cfg.get("points", 12)
    use_in_score = _mom_cfg.get("use_in_score", True)
    w_mom = max(0.0, min(0.5, points / 100.0)) if use_in_score else 0.0
    _mom_contrib = None
    if not use_in_score or w_mom == 0:
        total = round(fundamental)
    elif mom_method == "maturity_penalty":
        # 价格成熟度惩罚：已兑现(涨得多/超买)→低分；未兑现→高分。翻转加权项 (100-maturity_score)
        _mat = r.get("maturity_score")
        if _mat is None:
            total = round(fundamental)
        else:
            _mom_contrib = round(100 - _mat, 1)   # 翻转后实际贡献分（已兑现→低）
            total = round(fundamental * (1 - w_mom) + _mom_contrib * w_mom)
    elif mom_method == "maturity_bonus":
        # 价格成熟度反转(2026-10-15)：已涨/强势→加分；弱势/超卖→降权。与信号狩猎结论一致
        _mat = r.get("maturity_score")
        if _mat is None:
            total = round(fundamental)
        else:
            _mom_contrib = round(_mat, 1)          # 高成熟度(已涨/强势)→实际贡献分高
            total = round(fundamental * (1 - w_mom) + _mom_contrib * w_mom)
    else:
        if mom_score is None:
            total = round(fundamental)
        else:
            _mom_contrib = mom_score
            total = round(fundamental * (1 - w_mom) + mom_score * w_mom)
    total = max(0, total)
    grade = ("优秀" if total >= 85 else "良好" if total >= 70 else
             "中性" if total >= 55 else "偏弱")
    return {
        "score": total, "grade": grade,
        "val_score": val, "qual_score": qual, "safe_score": safe,
        "mom_score": mom_score, "mom": r.get("mom"), "mom_points": points,
        "maturity_score": r.get("maturity_score"), "maturity_contrib": _mom_contrib,
        "roe_score": round(roe_score, 1), "grow_score": round(grow_score, 1),
        "grow_note": grow_note, "penalty": round(penalty, 1),
        "penalty_detail": penalty_detail, "is_cyclical": is_cycl,
    }


def score_cell(r):
    """生成最终备选表中『综合评分』列的 HTML（分数+评级+三维度明细）。"""
    sd = r.get("score_detail", {})
    s = r.get("score", 0)
    color = {"优秀": "#1D9E75", "良好": "#3FB950", "中性": "#D29922", "偏弱": "#E0533D"}.get(
        sd.get("grade"), "#888")
    _mom_s = sd.get("mom_score")
    _mat = sd.get("maturity_score")
    _mc = sd.get("maturity_contrib")
    if _mat is not None and _mc is not None:
        _mom_part = f"｜成熟度{_mat:.0f}(已兑现扣{100 - _mat:.0f})"
        _mom_line = f"·熟{_mat:.0f}"
    elif _mom_s is not None:
        _mom_part = f"｜动量{_mom_s}（{(sd.get('mom') or 0)*100:.0f}%）"
        _mom_line = f"·动{_mom_s}"
    else:
        _mom_part = "｜动量—"
        _mom_line = ""
    title = (f"估值{sd.get('val_score')} 质量{sd.get('qual_score')} 安全{sd.get('safe_score')}"
             f"{_mom_part}｜ROE分{sd.get('roe_score')} 增速分{sd.get('grow_score')} 扣分{sd.get('penalty')}")
    return (f'<td><span style="font-weight:700;color:{color}">{s}</span> '
            f'<span style="font-size:11px;color:{color}">{sd.get("grade","")}</span>'
            f'<br><span style="font-size:10px;color:#888" title="{title}">'
            f'估值{sd.get("val_score")}·质{sd.get("qual_score")}·安{sd.get("safe_score")}{_mom_line}</span></td>')


def run_cli(args):
    cmd = ["npx", "-y", "westock-data-skillhub@1.0.5"] + args
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="ignore", shell=True)
    out = res.stdout.strip()
    try:
        return json.loads(out)
    except Exception:
        print(f"[ERROR] 解析失败 {' '.join(args)}: {out[:300]}", file=sys.stderr)
        return None


def run_cli_retry(args, retries=1, delay=0.5):
    """带重试的 run_cli：瞬时失败（限流/超时导致的解析失败或错误对象）重试 retries 次。

    判定为"有效结果"：list（含空列表=合法无数据）或含 list 型 data 字段的 dict。
    None / 其他结构（CLI 报错 JSON）→ 重试。每次重试间隔 delay 秒。
    """
    for attempt in range(retries + 1):
        kl = run_cli(args)
        if isinstance(kl, list) or (isinstance(kl, dict) and isinstance(kl.get("data"), list)):
            return kl
        if attempt < retries:
            if isinstance(kl, dict):
                print(f"[WARN] CLI 返回异常(第{attempt + 1}次) {' '.join(args[:2])}: {str(kl)[:200]}，{delay}s 后重试",
                      file=sys.stderr)
            time.sleep(delay)
    return kl


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_westock_code(code):
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    # 北交所代码段：83/87/88/43x 老段 + 920/921 新段；9 开头在A股筛选语境下仅北交所
    if code.startswith(("4", "8", "9")):
        return "bj" + code
    return "sh" + code


def _days_since(date_str):
    """财报披露日距今天数；无法解析返回 None（坑13 披露日护栏用）"""
    if not date_str:
        return None
    import re
    s = str(date_str).strip().replace("/", "-")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        m2 = re.search(r"(\d{4})(\d{2})(\d{2})", s)
        if not m2:
            return None
        y, mo, d = map(int, m2.groups())
    else:
        y, mo, d = map(int, m.groups())
    try:
        from datetime import date
        return (date.today() - date(y, mo, d)).days
    except Exception:
        return None


def _cagr2(income):
    """近2年同期归母净利润复合增速；数据不足返回 None（坑7 用）"""
    if len(income) < 2:
        return None
    md = (income[0].get("_date") or "")[5:10]
    cur = pick(income[0], "NPParentCompanyOwners")
    bases = [pick(q, "NPParentCompanyOwners") for q in income[1:]
             if (q.get("_date") or "")[5:10] == md]
    if not bases or cur is None:
        return None
    base = bases[-1]
    if cur > 0 and base > 0:
        return (cur / base) ** 0.5 - 1
    return None


def get_stock_data(codes):
    """批量获取 finance（三表）与 kline（最新收盘），分块调用，返回 {code: {...}}"""
    data = {c: {"income": [], "balance": [], "cash": [], "close": None, "close_date": None} for c in codes}
    for i in range(0, len(codes), FINANCE_CHUNK):
        batch = codes[i:i + FINANCE_CHUNK]
        fin = run_cli(["finance", ",".join(batch), "--num", "5", "--raw"])
        if isinstance(fin, dict) and isinstance(fin.get("sections"), list):
            secs = fin["sections"]
            keymap = [("income", 0), ("balance", 1), ("cash", 2)]
            for field, idx in keymap:
                if len(secs) > idx and isinstance(secs[idx], list):
                    for rec in secs[idx]:
                        c = rec.get("code") or rec.get("symbol")
                        if c in data:
                            data[c][field].append(rec)
        kl = run_cli_retry(["kline", ",".join(batch), "--period", "day", "--limit", "1", "--raw"])
        kitems = []
        if isinstance(kl, list):
            kitems = kl
        elif isinstance(kl, dict) and isinstance(kl.get("data"), list):
            kitems = kl["data"]
        for item in kitems:
            c = item.get("symbol") or item.get("code")
            close = item.get("last") or item.get("close")
            if c in data and close:
                data[c]["close"] = safe_float(close)
                data[c]["close_date"] = item.get("date") or ""
    return data


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return safe_float(d[k])
    return 0.0


def assess(code, name, industry, sd):
    """风险筛查 + 估值推算，返回评估 dict"""
    income = sorted(sd["income"], key=lambda x: x.get("_date", ""), reverse=True)
    balance = sorted(sd["balance"], key=lambda x: x.get("_date", ""), reverse=True)
    cash = sorted(sd["cash"], key=lambda x: x.get("_date", ""), reverse=True)
    m = {"code": code, "name": name, "industry": industry, "close": sd.get("close"),
         "close_date": sd.get("close_date")}
    if not balance or not income:
        if code.startswith("bj"):
            # 北交所标的：westock-data 财务覆盖度低，无数据直接剔除（不进备选池）
            m.update({"status": "reject", "reasons": ["北交所财务数据未覆盖（westock-data 无数据），自动剔除"],
                      "peg": None, "PE_TTM": 0, "PB": 0, "ROE_TTM": 0,
                      "np_deduct_growth": None, "receivables_ratio": 0,
                      "goodwill_ratio": 0, "ocf_ttm": 0, "gm_declining_quarters": 0})
        else:
            m.update({"status": "warn", "reasons": ["财务数据不完整，需人工复核"],
                      "peg": None, "PE_TTM": 0, "PB": 0, "ROE_TTM": 0,
                      "np_deduct_growth": None, "receivables_ratio": 0,
                      "goodwill_ratio": 0, "ocf_ttm": 0, "gm_declining_quarters": 0})
        return m

    lb, li = balance[0], income[0]
    lc = cash[0] if cash else {}

    np_q = pick(li, "NPParentCompanyOwners_Q")
    eps = pick(li, "BasicEPS")
    shares = np_q / eps if eps > 0 else 0
    equity = pick(lb, "TotalShareholderEquity", "SEWithoutMI", "TotalEquity")
    np_ttm = pick(li, "NPParentCompanyOwnersTTM")
    rev_ttm = pick(li, "OperatingRevenueTTM")
    close = sd.get("close") or 0

    m["PE_TTM"] = (close * shares / np_ttm) if (close and shares and np_ttm > 0) else 0
    m["PB"] = (close * shares / equity) if (close and shares and equity > 0) else 0
    m["ROE_TTM"] = (np_ttm / equity * 100) if equity > 0 else 0

    reasons, warns = [], []
    is_bank = ("银行" in industry) or ("银行" in name)
    # 非银金融（券商/保险/信托/期货/金控等）与银行同属金融业，资产负债表结构不同于
    # 一般工商企业：应收占比、经营现金流质量、流动比率等阈值对其不适用，需享受同等豁免
    # （否则券商/保险会被"现金流为负""流动比率<1"等硬规则误剔）。
    is_finance = is_bank or industry.startswith("非银金融") or any(
        k in name for k in ("证券", "券商", "保险", "期货", "信托", "财富", "金控"))
    is_commodity = any(industry.startswith(c) for c in COMMODITY_PEAK_INDUSTRIES)

    # —— 坑13：财报披露日新鲜度护栏（仅捕捉未来日期/远超披露窗口的异常陈旧数据）——
    # A股季报/半年报披露节奏：Q1(3/31)最晚4/30披露，Q2(6/30)最晚8/31，Q3(9/30)最晚10/31，
    # 年报(12/31)最晚次年4/30。故任意已披露报告距今天数正常上限约150~155天；
    # 若>200天（约跨过一个完整报告期仍未更新）或日期在未来，才属异常（未来函数/数据错误）。
    report_date = lb.get("_date") or ""
    ds = _days_since(report_date)
    if report_date and ds is not None and (ds < 0 or ds > 200):
        warns.append(f"财报日期 {report_date} 距今天数异常({ds}天)，疑似未披露/数据异常，需人工确认")

    # —— 应收 / 商誉 基础风险 ——
    recv = pick(lb, "BillAccReceivable") + pick(lb, "OtherReceivableED")
    recv_prev = (pick(balance[1], "BillAccReceivable") + pick(balance[1], "OtherReceivableED")) if len(balance) > 1 else 0
    m["receivables_ratio"] = recv / rev_ttm if rev_ttm else 0
    if not is_finance and m["receivables_ratio"] > 0.30:
        reasons.append(f"应收账款占营收 {m['receivables_ratio']:.1%} > 30%")

    gw = pick(lb, "GoodWill")
    gw_prev = pick(balance[1], "GoodWill") if len(balance) > 1 else 0
    m["goodwill_ratio"] = gw / equity if equity else 0
    if m["goodwill_ratio"] > 0.30:
        reasons.append(f"商誉占净资产 {m['goodwill_ratio']:.1%} > 30%")

    # —— 坑5：现金流质量比（替换仅查<0 的弱条件）——
    m["ocf_ttm"] = pick(lc, "NetOperateCashFlowTTM")
    if not is_finance and m["ocf_ttm"] is not None:
        if np_ttm > 0:
            ocf_ratio = m["ocf_ttm"] / np_ttm
            if ocf_ratio < 0.2:
                reasons.append(f"经营现金流/净利润 {ocf_ratio:.2f} < 0.2，纸面利润")
            elif ocf_ratio < 0.5:
                warns.append(f"经营现金流/净利润 {ocf_ratio:.2f} < 0.5，利润含金量低")
        elif m["ocf_ttm"] < 0:
            reasons.append("TTM经营现金流为负")

    ta = pick(lb, "TotalCurrentAssets") + pick(lb, "TotalNonCurrentAssets")
    cash_eq = pick(lb, "CashEquivalents")
    int_debt = pick(lb, "InterestBearDebt")
    m["cash_ratio"] = cash_eq / ta if ta else 0
    m["debt_ratio"] = int_debt / ta if ta else 0
    if ta > 0 and m["cash_ratio"] > 0.40 and m["debt_ratio"] > 0.40:
        reasons.append("存贷双高")

    tca = pick(lb, "TotalCurrentAssets")
    tcl = pick(lb, "TotalCurrentLiability")
    m["current_ratio"] = tca / tcl if tcl else 99
    if m["current_ratio"] < 1 and not is_finance:
        reasons.append(f"流动比率 {m['current_ratio']:.2f} < 1")

    gm_list = []
    for q in income:
        gp = pick(q, "GrossProfitTTM")
        rev = pick(q, "OperatingRevenueTTM")
        if rev > 0:
            gm_list.append((q.get("_date"), gp / rev, rev))
    declining = 0
    for i in range(1, len(gm_list)):
        if gm_list[i][1] < gm_list[i-1][1] and gm_list[i][2] >= gm_list[i-1][2]:
            declining += 1
        else:
            break
    m["gm_declining_quarters"] = declining
    if declining >= 2:
        warns.append("毛利率连续下滑且营收增长")

    # —— 增速（扣非为主，封堵归母回退 · 坑1）——
    latest_np = pick(lb, "NPDeductNonRecurringPL")
    mmdd = (lb.get("_date") or "")[5:10]
    sply = 0.0
    for q in balance[1:]:
        if (q.get("_date") or "")[5:10] == mmdd:
            sply = pick(q, "NPDeductNonRecurringPL")
            break
    m["np_deduct_growth"] = (latest_np / sply - 1) if (latest_np > 0 and sply > 0) else None

    def yoy(rows, key):
        cur = pick(rows[0], key)
        md = (rows[0].get("_date") or "")[5:10]
        for q in rows[1:]:
            if (q.get("_date") or "")[5:10] == md:
                prev = pick(q, key)
                return (cur / prev - 1) if prev > 0 else None
        return None
    m["rev_yoy"] = yoy(income, "OperatingRevenue")
    m["np_yoy"] = yoy(income, "NPParentCompanyOwners")
    m["recv_yoy"] = (recv / recv_prev - 1) if (recv > 0 and recv_prev > 0) else None

    # —— 坑1：扣非为 None 时不再回退归母净利润 ——
    g = m["np_deduct_growth"]
    if g is None:
        warns.append("扣非净利润增速数据缺失，增长质量未知，需人工复核")

    # —— 历史常态均值 / 两年复合（供坑3/6/7/8 复用，无条件计算）——
    all_ttm = [pick(q, "NPParentCompanyOwnersTTM") for q in income
               if pick(q, "NPParentCompanyOwnersTTM") > 0]
    avg_ttm = sum(all_ttm) / len(all_ttm) if len(all_ttm) >= 2 else None
    cagr2 = _cagr2(income)

    low_base_rejected = False
    peak_flag = False

    if g and g > 3:
        cond_b = sply > 0 and sply < 50_000_000
        cond_d = (np_ttm > 0 and avg_ttm is not None and np_ttm < avg_ttm * 1.1)
        data_ok = sply > 0 and avg_ttm is not None
        if g > 5:
            if data_ok and (cond_b or cond_d):
                reasons.append("低基数恢复：扣非增速>500%但基数过低或利润未超历史常态，增长质量存疑")
                low_base_rejected = True
            else:
                warns.append("利润增速>500%，疑似低基数恢复")
        elif g > 3:
            if data_ok and (cond_b or cond_d):
                warns.append("利润增速>300%，疑似低基数恢复")
            else:
                warns.append("利润增速>300%，高增长需复核")
        # 坑8：绝对体量小
        if np_ttm > 0 and np_ttm < 200_000_000 and g > 2:
            warns.append("绝对体量小(<2亿)，增速波动大，警惕微量高增长")

    # —— 坑3/坑6：商品周期景气高位保护（镜像 D 项）——
    if is_commodity and avg_ttm is not None and np_ttm > 0 and np_ttm > avg_ttm * 1.5:
        warns.append(f"商品周期景气高位：TTM净利为3年均值{avg_ttm/1e8:.1f}亿的{np_ttm/avg_ttm:.1f}倍，PEG失效，警惕回落")
        m["peg"] = None
        peak_flag = True

    # —— 坑4：应收增速远超营收增速 ——
    if m["recv_yoy"] is not None and m["rev_yoy"] is not None:
        if m["recv_yoy"] > m["rev_yoy"] * 1.5:
            if m["recv_yoy"] > 0.4 and (m["rev_yoy"] or 0) < 0.15:
                reasons.append(f"应收增速{m['recv_yoy']:.0%}远超营收{m['rev_yoy']:.0%}，赊销虚增收入风险")
            else:
                warns.append(f"应收增速{m['recv_yoy']:.0%}高于营收{m['rev_yoy']:.0%}，关注赊销节奏")

    # —— 坑2：并购并表识别 ——
    if gw > 0 and equity > 0:
        gw_delta_ratio = (gw - gw_prev) / equity if gw_prev >= 0 else 0
        if gw_delta_ratio > 0.08:
            warns.append("商誉较上期大幅增加，利润或含并购并表，内生增长待核")
        elif (m["rev_yoy"] is not None and g is not None and g > 0
              and m["rev_yoy"] > g * 1.5):
            warns.append("营收增速远超扣非增速，警惕并购并表虚增")

    # —— 坑7：单年暴增但两年复合低 ——
    if g and g > 1 and cagr2 is not None and cagr2 < 0.15:
        warns.append(f"单年扣非增速{g:.0%}但两年复合仅{cagr2:.0%}，增长不可持续")

    # —— PEG 计算（扣非口径，已封堵归母回退）——
    if low_base_rejected or peak_flag:
        m["peg"] = None
    elif g and g > 5:
        m["peg"] = None
    elif g and g > 0 and m["PE_TTM"] > 0:
        m["peg"] = m["PE_TTM"] / (g * 100)
    else:
        m["peg"] = None

    # PE≤0 处理：普通标的直接剔除；周期行业底部“报表亏但实际经营有盈利”留人工复核
    if m["PE_TTM"] <= 0:
        is_cyclical = any(industry.startswith(c) for c in CYCLICAL_INDUSTRIES)
        if is_cyclical:
            # 报表亏但实际经营有盈利（扣非净利润为正）
            if latest_np > 0:
                warns.append("PE≤0但扣非净利润为正，周期行业底部报表亏损，待人工复核")
            else:
                reasons.append("PE≤0，周期行业扣非亏损")
        else:
            reasons.append("PE≤0，直接剔除")

    if reasons:
        m["status"] = "reject"
        m["reasons"] = reasons
    elif warns:
        m["status"] = "warn"
        m["reasons"] = warns
    else:
        m["status"] = "pass"
        m["reasons"] = []
    # 拆分 reject_reasons / warn_notes 两个语义字段（避免下游 monitor_exits 把 warn 误判为 EXIT）
    m["reject_reasons"] = reasons
    m["warn_notes"] = warns
    return m


def detect_mode():
    """检测 _mx_pool.json 的初选日期，判定当日是否需要降级。
    返回 (mode, pool_date, reason)：
      mode='full'      -> 初选成功（generated_at/date 为今日）
      mode='degraded'  -> 未发现今日初选（mx 授权失败或脚本未跑第一步）
    """
    try:
        with open(POOL_FILE, "r", encoding="utf-8") as f:
            pool = json.load(f)
    except FileNotFoundError:
        return "degraded", None, "pool_missing"
    gen = pool.get("generated_at") or pool.get("date") or ""
    today = datetime.now().strftime("%Y-%m-%d")
    if gen == today:
        return "full", gen, None
    return "degraded", gen, f"mx_auth_failed_or_unrun:pool_date={gen}"


def load_pool():
    with open(POOL_FILE, "r", encoding="utf-8") as f:
        pool = json.load(f)
    seen = set()
    universe = []
    coarse = pool.get("coarse_filtered") or []
    if coarse:
        groups = ("coarse_filtered",)
    else:
        groups = ("growth_raw", "cyclical_raw")
    for grp in groups:
        for it in pool.get(grp, []):
            code = it["code"]
            if code in seen:
                continue
            seen.add(code)
            it["is_cyclical"] = any((it.get("sw") or "").startswith(c) for c in CYCLICAL_INDUSTRIES)
            universe.append(it)

    # ---- 板块权限兜底过滤 ----
    # _build_pool.py 每日由自动化重新生成，可能漏掉板块过滤；此处二次拦截，
    # 确保无交易权限的板块标的绝不进入财务复核与最终备选。
    excluded = load_excluded_boards()
    if excluded:
        universe, blocked = filter_stocks(universe, verbose=True, stage="主脚本兜底")
        if blocked:
            BOARD_BLOCKED.extend(
                {"code": s.get("code"), "name": s.get("name"), "board": s.get("_blocked_board")}
                for s in blocked
            )
    return universe


def load_market_gate():
    """加载市场门控配置，控制成长线入选上限 + 总仓区间。

    择时去主观化（2026-08-31）：优先用 quantify_market() 基于 market-overview 硬指标数值分
    计算市场热度 heat 与反指仓位决策，替代原定性"情绪狂热/恐慌"词。
    market_narrative.json 的 market_gate 仍可手动覆盖 growth_limit / defense_boost /
    reason / position_range（manual_override=true 时）。
    返回 dict 含：growth_limit, defense_boost, reason, trend, sentiment,
                  heat, valuation_score, sentiment_score, volume_energy_score,
                  stock_width_score, regime_label, position_range, action, style。
    """
    gate_file = os.path.join(WORKSPACE, "market_narrative.json")
    manual_gate = {}
    if os.path.exists(gate_file):
        try:
            with open(gate_file, "r", encoding="utf-8") as f:
                manual_gate = json.load(f).get("market_gate", {})
        except Exception:
            pass

    # 复用 market-overview 硬指标（含 VALUATION_SCORE/SENTIMENT_SCORE/VOLUME_ENERGE_SCORE/STOCK_WIDTH_SCORE）
    summary = run_cli(["market-overview", "--raw"])
    srow = {}
    if isinstance(summary, list) and len(summary) > 0 and isinstance(summary[0], dict):
        srow = summary[0].get("row", {})
    elif isinstance(summary, dict) and isinstance(summary.get("data"), list) and len(summary["data"]) > 0:
        d = summary["data"][0]
        srow = d.get("row", d) if isinstance(d, dict) else {}

    trend = srow.get("TREND_SHORT_DIRECTION_STATUS", "")
    sentiment = srow.get("SENTIMENT_STATUS", "")

    # 量化择时：硬指标 → heat → 反指仓位决策（替代定性情绪词）
    tcfg = load_timing_config()
    regime = quantify_market(srow, tcfg)
    dec = regime["decision"]

    growth_limit = dec["growth_limit"]
    defense_boost = dec["defense_boost"]
    reason = regime["contrarian_note"]
    position_range = dec["position_range"]
    action = dec["action"]
    style = dec["style"]

    # 手动覆盖（可选）：market_narrative.json 的 market_gate 仍可微调
    # 择时去主观化：默认 False，配置键缺失时回退到「硬指标唯一可信源」，不启用手动覆盖
    if tcfg.get("manual_override", False):
        if "growth_limit" in manual_gate:
            growth_limit = manual_gate["growth_limit"]
        if "defense_boost" in manual_gate:
            defense_boost = manual_gate["defense_boost"]
        if manual_gate.get("reason"):
            reason = manual_gate["reason"]
        if manual_gate.get("position_range"):
            position_range = manual_gate["position_range"]

    return {
        "growth_limit": growth_limit,
        "defense_boost": defense_boost,
        "reason": reason,
        "trend": trend,
        "sentiment": sentiment,
        "heat": regime["heat"],
        "valuation_score": regime["valuation_score"],
        "sentiment_score": regime["sentiment_score"],
        "volume_energy_score": regime["volume_energy_score"],
        "stock_width_score": regime["stock_width_score"],
        "regime_label": regime["regime_label"],
        "position_range": position_range,
        "action": action,
        "style": style,
    }


# ---------------------------------------------------------------------------
# 持仓建议模块（2026-08-27 新增）
# 结合「计划总仓位」与「用户真实持仓(_my_holdings.json)」，对每日入选标的
# 计算建议占比(总仓口径)、建议金额，并标注 新建仓/加仓/减仓/维持。
# 配置见 _position_config.json，改配置即生效，无需动代码。
# ---------------------------------------------------------------------------
def load_position_config():
    cfg_path = os.path.join(WORKSPACE, "_position_config.json")
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            pass
    return {"total_planned": 200000, "holdings_file": "_my_holdings.json",
            "per_stock_cap": 0.12, "tier_weights": {"pass": 1.0, "warn": 0.5},
            "enabled": True}


def load_timing_config():
    """读取量化择时配置 _timing_config.json；缺失时返回默认权重。"""
    cfg_path = os.path.join(WORKSPACE, "_timing_config.json")
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": True,
            "weights": {"valuation": 0.45, "sentiment": 0.30, "volume_energy": 0.25},
            "breadth_tilt": {"stock_width_low": 30, "tilt_points": 8},
            "heat_bands": {"overheat": 70, "neutral_high": 55, "neutral_low": 40, "panic": 30},
            "decisions": {}, "manual_override": False}


def _norm_dim_score(x, default=50.0):
    """把 market-overview 的维度得分归一化到 0-100。

    ⚠️ 2026-09-01 修复的量纲 bug：接口返回的是 **1-5 分制**（5=最热/最贵/最强，
    如 估值水平=4 表示 PE分位70%~90%、情绪指标=5 表示狂热），此前代码直接按 0-100
    加权，导致 heat 恒落在 0~5 区间、被 clamp 成 0，模型每天都误判"恐慌低位·逐步加仓"，
    与真实市场（狂热高位应减仓）完全相反。
    映射：1→0, 2→25, 3→50(中性), 4→75, 5→100。
    兼容性：若接口未来改为百分制（>5），视为已是 0-100 直接使用。
    """
    v = safe_float(x, -1)
    if v is None or v < 0:
        return default
    if v <= 5.0:                                  # 1-5 分制 → 0-100
        return _clamp((v - 1.0) / 4.0 * 100.0, 0, 100)
    return _clamp(v, 0, 100)                      # 已是百分制


def quantify_market(srow, tcfg=None):
    """择时去主观化：用 market-overview 的硬指标数值分计算市场热度 heat(0-100)。

    输入 srow 为 market-overview --raw 的首条 row，含 VALUATION_SCORE(估值分) /
    SENTIMENT_SCORE(情绪分) / VOLUME_ENERGE_SCORE(量能分) / STOCK_WIDTH_SCORE(宽度分)。
    **这些原始分是 1-5 分制**，先经 _norm_dim_score() 归一化到 0-100 再加权（见该函数注释）。
    heat = w_val*估值分 + w_sent*情绪分 + w_vol*量能分；higher=越贵越热越应减仓。
    宽度分(个股宽度)低位→反向倾斜降低 heat（恐慌特征→偏加仓，反指）。
    反指逻辑：heat 高→减仓；heat 低(恐慌)→加仓。返回全部数值分量，可回测可纠错。
    """
    if tcfg is None:
        tcfg = load_timing_config()
    w = tcfg.get("weights", {"valuation": 0.45, "sentiment": 0.30, "volume_energy": 0.25})
    bands = tcfg.get("heat_bands", {"overheat": 70, "neutral_high": 55, "neutral_low": 40, "panic": 30})
    decisions = tcfg.get("decisions", {})

    val_raw = safe_float(srow.get("VALUATION_SCORE"), None)
    sent_raw = safe_float(srow.get("SENTIMENT_SCORE"), None)
    vol_raw = safe_float(srow.get("VOLUME_ENERGE_SCORE"), None)
    width_raw = safe_float(srow.get("STOCK_WIDTH_SCORE"), None)

    val = _norm_dim_score(val_raw)
    sent = _norm_dim_score(sent_raw)
    vol = _norm_dim_score(vol_raw)
    width = _norm_dim_score(width_raw)

    heat = (w.get("valuation", 0.45) * val
            + w.get("sentiment", 0.30) * sent
            + w.get("volume_energy", 0.25) * vol)
    # 宽度低位（市场恐慌、个股普跌）→ 反向倾斜降低 heat，偏向加仓（反指）
    tilt = tcfg.get("breadth_tilt", {})
    if width <= safe_float(tilt.get("stock_width_low", 30), 30):
        heat -= safe_float(tilt.get("tilt_points", 8), 8)
    heat = _clamp(heat, 0, 100)

    if heat >= bands.get("overheat", 70):
        band = "overheat"
    elif heat >= bands.get("neutral_high", 55):
        band = "neutral_high"
    elif heat >= bands.get("neutral_low", 40):
        band = "neutral_low"
    else:
        band = "panic"

    dec = decisions.get(band, {"position_range": "30%-50%", "action": "观望为主",
                               "style": "价值防御", "growth_limit": 6, "defense_boost": False})
    if band == "overheat":
        note = (f"heat={heat:.0f} 过热区间（估值分{val:.0f}/情绪分{sent:.0f}/量能分{vol:.0f}）："
                f"一致看多+高位，反指警惕二次下跌，减仓")
    elif band == "panic":
        note = (f"heat={heat:.0f} 恐慌低位（估值分{val:.0f}/情绪分{sent:.0f}/量能分{vol:.0f}）："
                f"一致看空+低位，反指看多，主升/布局")
    else:
        note = (f"heat={heat:.0f} 中性区间（估值分{val:.0f}/情绪分{sent:.0f}/量能分{vol:.0f}）：均衡配置")

    # ===== 甲：heat 二次确认降级（2026-09-02 新增）=====
    # heat 跌进恐慌区(<heat_bands.panic)不再无条件判"逐步加仓"；
    # 须估值分与情绪分同步跌到低位，确认为真恐慌低位(便宜)才加仓，
    # 否则降级为 downgrade_band(维持防御观望)，避免高位跌/下跌中继接飞刀。
    rc = tcfg.get("reversal_confirm", {})
    if rc.get("enabled", False) and band == "panic":
        cv = safe_float(rc.get("confirm_valuation_max"), 35)
        cs = safe_float(rc.get("confirm_sentiment_max"), 30)
        if not (val <= cv and sent <= cs):
            down = rc.get("downgrade_band", "neutral_low")
            band = down
            dec = decisions.get(down, {"position_range": "30%-50%", "action": "观望为主",
                                       "style": "价值防御", "growth_limit": 6, "defense_boost": False})
            note = (f"heat={heat:.0f} 跌进恐慌区但估值分{val:.0f}> {cv:.0f} 或情绪分{sent:.0f}> {cs:.0f}，"
                    f"判定为高位跌/下跌中继(非真低位)，维持防御观望，不抄底")
    return {
        "heat": round(heat, 1), "valuation_score": round(val, 1), "sentiment_score": round(sent, 1),
        "volume_energy_score": round(vol, 1), "stock_width_score": round(width, 1),
        # 原始 1-5 分制留档，便于回测核对归一化是否正确（2026-09-01 量纲修复）
        "raw_scores_1to5": {"valuation": val_raw, "sentiment": sent_raw,
                            "volume_energy": vol_raw, "stock_width": width_raw},
        "regime_label": dec.get("label", band), "band": band, "contrarian_note": note,
        "decision": {"position_range": dec.get("position_range"), "action": dec.get("action"),
                      "style": dec.get("style"), "growth_limit": dec.get("growth_limit", 6),
                      "defense_boost": dec.get("defense_boost", False)},
    }


def load_exit_rules():
    """读取硬止损/退出纪律配置 _exit_rules.json；缺失时返回默认。"""
    cfg_path = os.path.join(WORKSPACE, "_exit_rules.json")
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": True, "max_drawdown_pct": 8, "break_ma": "MA20",
            "time_stop_days": 60, "time_stop_min_pct": -10, "hard_exit": True}


def load_factor_config():
    """读取因子配置 _factor_config.json；缺失时返回默认（动量启用、12-1月、占12分）。"""
    cfg_path = os.path.join(WORKSPACE, "_factor_config.json")
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            pass
    return {"momentum": {"enabled": True, "method": "umd", "use_in_score": True, "lookback_months": 12,
                         "skip_months": 1, "points": 12, "strong_up": 0.50, "strong_down": -0.30}}


def get_ma_levels(codes, window=60):
    """批量取个股 MA20/MA60 与最新收盘（kline --raw 按日，最新在前）。返回 {code:{close,ma20,ma60}}。"""
    out = {}
    if not codes:
        return out
    kl = run_cli_retry(["kline", ",".join(codes), "--period", "day", "--limit", str(window), "--raw"])
    items = []
    if isinstance(kl, list):
        items = kl
    elif isinstance(kl, dict) and isinstance(kl.get("data"), list):
        items = kl["data"]
    by_code = {}
    for it in items:
        c = it.get("symbol") or it.get("code")
        if c:
            by_code.setdefault(c, []).append(it)
    for c in codes:
        bars = by_code.get(c, [])
        if not bars:
            continue
        bars_sorted = list(reversed(bars))  # 最新在前→时间升序
        closes = [safe_float(b.get("last") or b.get("close")) for b in bars_sorted
                  if (b.get("last") or b.get("close"))]
        if not closes:
            continue
        cur = closes[-1]
        ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else sum(closes) / len(closes)
        ma60 = sum(closes[-60:]) / len(closes[-60:]) if len(closes) >= 60 else sum(closes) / len(closes)
        out[c] = {"close": cur, "ma20": round(ma20, 3), "ma60": round(ma60, 3)}
    return out


def compute_momentum(codes, mcfg=None):
    """计算个股 UMD 动量因子（经典 12-1 月），作为复合评分第4维度。

    返回 {code: {"mom": 浮点收益率或None, "mom_score": 0-100或None}}。
    - 数据：westock-data kline 日线，按日期范围逐码取足够历史（(lookback+skip+3)月日历日）。
      注意：批量 kline 接口有 ~500 条总返回上限，8 码同批会被压到每码约 60 条，不足 12-1 月
      （需约 274 条）；故改为逐码按 --start/--end 日期范围取，单码不受总上限影响。
    - UMD = 收盘(t-skip_m) / 收盘(t-(skip+lookback)_m) - 1（跳过最近 skip_m 月规避短期反转）。
    - 动量分映射：mom>=strong_up→100；mom<=strong_down→0；区间内线性；无足够历史→None（评分时不叠加动量）。
    """
    out = {}
    if not codes:
        return out
    mcfg = mcfg or {}
    if not mcfg.get("enabled", True):
        return out
    lookback_m = int(mcfg.get("lookback_months", 12))
    skip_m = int(mcfg.get("skip_months", 1))
    strong_up = float(mcfg.get("strong_up", 0.50))
    strong_down = float(mcfg.get("strong_down", -0.30))
    needed_bars = (lookback_m + skip_m) * 21 + 6     # 1月≈21交易日 + 缓冲
    skip_days = skip_m * 21
    lookback_days = lookback_m * 21
    today = datetime.now().date()
    # 单码 kline 接口每调用上限 ~250 条，12-1 月需约 274 条 → 按日期范围逐页向后翻补齐
    start_far = today - __import__("datetime").timedelta(days=(lookback_m + skip_m + 6) * 31)
    start_far_s = start_far.strftime("%Y-%m-%d")
    end_s = today.strftime("%Y-%m-%d")
    for c in codes:
        bars = []
        cur_end = end_s
        prev_earliest = None
        for _ in range(4):                            # 最多翻4页补齐历史
            kl = run_cli_retry(["kline", c, "--period", "day", "--start", start_far_s,
                                "--end", cur_end, "--limit", "250", "--raw"])
            chunk = []
            if isinstance(kl, list):
                chunk = kl
            elif isinstance(kl, dict) and isinstance(kl.get("data"), list):
                chunk = kl["data"]
            chunk = [b for b in chunk if (b.get("symbol") or b.get("code")) == c] or chunk
            seen = {b.get("date") for b in bars}
            new_bars = [b for b in chunk if b.get("date") and b.get("date") not in seen]
            bars = new_bars + bars                    # 新取更早的放前面
            if len(bars) >= needed_bars:
                break
            dates = [b.get("date") for b in bars if b.get("date")]
            if not dates:
                break
            earliest = min(dates)
            if earliest == prev_earliest:
                break
            prev_earliest = earliest
            cur_end = (datetime.strptime(earliest, "%Y-%m-%d")
                       - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
        if not bars:
            out[c] = {"mom": None, "mom_score": None}
            continue
        bars_sorted = sorted(bars, key=lambda b: b.get("date") or "")   # 时间升序（最旧→最新）
        closes = []
        for b in bars_sorted:
            p = safe_float(b.get("last") or b.get("close"))
            if p > 0:
                closes.append(p)
        n = len(closes)
        idx_recent = n - 1 - skip_days               # t-skip_m
        idx_past = n - 1 - (skip_days + lookback_days)  # t-(skip+lookback)_m
        if idx_past < 0 or idx_recent < 0 or closes[idx_past] <= 0:
            out[c] = {"mom": None, "mom_score": None}
            continue
        mom = closes[idx_recent] / closes[idx_past] - 1.0
        if mom >= strong_up:
            mom_score = 100.0
        elif mom <= strong_down:
            mom_score = 0.0
        else:
            mom_score = _clamp((mom - strong_down) / (strong_up - strong_down), 0, 1) * 100
        out[c] = {"mom": round(mom, 4), "mom_score": round(mom_score, 1)}
    return out


def compute_tech_indicators(codes, end_date=None):
    """取每只票最新 RSI_6 / KDJ_J（技术指标），作为记录字段供后续回溯（不参与评分、不参与任何判定）。

    返回 {code: {"rsi_6": float或None, "kdj_j": float或None, "tech_date": "YYYY-MM-DD"或None}}。
    - 数据：westock-data technical 接口，支持批量 codes（单次调用即可覆盖全池）。
    - 取每只票区间（end_date 往前 120 自然日）内最新一条的值，等价于"入选日当时"的技术状态。
    - 仅记录：不进入 score_stock 复合分、不作为买入/卖出判定依据（与 UMD 同属 record_only 字段）。
    - 解析兼容两种返回：批量（data[].symbol + data[].data.items）与单码（data 即 items 列表）。
    """
    out = {}
    if not codes:
        return out
    end_s = (end_date or datetime.now().date()).strftime("%Y-%m-%d")
    start_s = (datetime.now().date() - __import__("datetime").timedelta(days=120)).strftime("%Y-%m-%d")
    codes_arg = ",".join(codes)
    d = run_cli(["technical", codes_arg, "--indicator", "rsi,kdj",
                 "--start", start_s, "--end", end_s, "--raw"])
    data = (d.get("data") if isinstance(d, dict) else d) if d else []
    by_code = {c: [] for c in codes}
    if isinstance(data, list) and data:
        if isinstance(data[0], dict) and ("symbol" in data[0] or "items" in (data[0].get("data") or {})):
            # 批量模式：每条 entry 带 symbol，内层 data.items 为指标序列
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                sym = entry.get("symbol") or (entry.get("data") or {}).get("code")
                items = (entry.get("data") or {}).get("items") or []
                if sym:
                    by_code.setdefault(sym, []).extend(items or [])
        else:
            # 单码模式：data 即为 items 列表（无 symbol），归到唯一 code
            only = codes[0] if len(codes) == 1 else None
            if only:
                by_code[only] = data
    for c in codes:
        rows = by_code.get(c) or []
        dated = []
        for it in rows:
            if not isinstance(it, dict):
                continue
            dt = it.get("date")
            if not dt:
                continue
            rsi = (it.get("rsi") or {}).get("RSI_6")
            kdj = (it.get("kdj") or {}).get("KDJ_J")
            dated.append((str(dt)[:10], rsi, kdj))
        dated.sort(key=lambda x: x[0])
        if dated:
            dt, rsi, kdj = dated[-1]
            out[c] = {"rsi_6": rsi, "kdj_j": kdj, "tech_date": dt}
        else:
            out[c] = {"rsi_6": None, "kdj_j": None, "tech_date": None}
    return out


def parse_position_band(position_range):
    """把 '30%-50%' 解析为 (下限, 上限) 比例，如 (0.30, 0.50)；解析失败返回 (0.30, 1.0)。"""
    try:
        s = str(position_range).replace("%", "").strip()
        parts = [float(p) for p in s.split("-") if p.strip()]
        lo = max(0.0, min(1.0, parts[0] / 100.0))
        hi = max(0.0, min(1.0, (parts[-1] if len(parts) > 1 else parts[0]) / 100.0))
        return lo, hi
    except Exception:
        return 0.30, 1.0


def compute_position_plan(final_all, lower_pct=0.30, upper_pct=1.0):
    """返回每只入选标的的建议持仓方案；若未启用则返回 None。

    严格服从择时门控的总仓区间 [lower, upper]：
    - 本轮部署目标 = 区间中点 target_pct（给后续交易日留余地，不一次打满）
    - 本轮可新增现金 = total × target_pct − 已持仓（已持仓无论是否在池内都占用额度）
    - 该预算池在入选标的间按层级权重分配，单票受 cap 与硬上限约束
    这样总仓永远不会突破 upper，且始终为「明天的好股票」预留额度。
    """
    cfg = load_position_config()
    if not cfg.get("enabled", True):
        return None
    total = float(cfg.get("total_planned", 200000))
    cap = float(cfg.get("per_stock_cap", 0.12))
    tier = cfg.get("tier_weights", {"pass": 1.0, "warn": 0.5})
    # warn 标的处置策略：observe_only=只观察不配新增资金；half_weight=按半权分配（旧行为）
    warn_policy = cfg.get("warn_policy", "observe_only")

    # 读取用户真实持仓（无论是否在今日模型池，都计入已占用额度）
    holdings = {}
    existing_total = 0.0
    hp = os.path.join(WORKSPACE, cfg.get("holdings_file", "_my_holdings.json"))
    if os.path.exists(hp):
        try:
            hd = json.load(open(hp, encoding="utf-8"))
            for h in hd.get("holdings", []):
                holdings[h["code"]] = float(h.get("value", 0))
            existing_total = float(hd.get("total_value", sum(holdings.values())))
        except Exception:
            pass

    lower_pct = max(0.0, min(1.0, lower_pct))
    upper_pct = max(lower_pct, min(1.0, upper_pct))
    target_pct = (lower_pct + upper_pct) / 2.0          # 本轮部署目标（区间中点）
    target_total = total * target_pct                   # 本轮目标总持仓市值
    hard_cap_total = total * upper_pct                  # 硬上限（区间上沿）
    available_new = max(0.0, target_total - existing_total)  # 本轮可新增现金预算池
    # 超配减仓提示（2026-09-01 新增）：过热区间下调总仓上限后，现有持仓可能已超硬上限。
    # 原逻辑只把 available_new 置 0（"不加仓"），却不告知"该减多少"，与 action=逐步减仓 脱节。
    over_cap = max(0.0, existing_total - hard_cap_total)   # 超出硬上限、需减仓的金额
    over_target = max(0.0, existing_total - target_total)  # 超出区间中点、建议削减的金额

    # 评分加权仓位（2026-08-31）：综合评分分档 × 状态权重，把 alpha 集中到确定性高的票
    # 优秀(≥85)=1.5×、良好(≥70)=1.2×、中性(≥55)=1.0×、偏弱(<55)=0.5×；仍受单票 cap 与择时总仓约束
    def _score_weight(s):
        if s is None:
            return 1.0
        if s >= 85:
            return 1.5
        if s >= 70:
            return 1.2
        if s >= 55:
            return 1.0
        return 0.5

    weights = {}
    score_w_map = {}
    for r in final_all:
        _sw = _score_weight(r.get("score"))
        _tw = tier.get(r["status"], 1.0)
        score_w_map[r["code"]] = _sw
        weights[r["code"]] = _tw * _sw
    total_w = sum(weights.values()) or 1.0

    plan = []
    for r in final_all:
        ex = holdings.get(r["code"], 0.0)               # 已持仓市值
        is_warn = (r["status"] == "warn")
        # warn 标的：warn_policy=observe_only 时不配新增资金；=half_weight 时按半权(0.5)降配分配
        if is_warn and warn_policy == "observe_only":
            target_new = 0.0
        else:
            target_new = available_new * (weights[r["code"]] / total_w)  # 本轮新增现金分配
        max_new = max(0.0, total * cap - ex)            # 单票上限(占计划总仓cap)约束
        target_new = max(0.0, min(target_new, max_new))
        ratio = target_new / total                      # 本轮新增占计划总仓比例
        if available_new <= 1 and target_new <= 1:
            # 择时总仓已打满/超配（如过热区间下调上限）→ 本轮无任何新增额度。
            # 2026-09-01 修复：此前会输出"新建仓 ¥0"这类自相矛盾的文案。
            if ex > 0:
                op, op_note = "维持(不加仓)", f"已持仓 ¥{ex:,.0f}｜总仓已超择时上限，本轮无新增额度"
            else:
                op, op_note = "候选(本轮不建仓)", "总仓已超择时上限，本轮无新增额度；待仓位回落后再评估"
        elif is_warn and warn_policy == "observe_only":
            if ex > 0:
                op, op_note = "维持", f"已持仓 ¥{ex:,.0f}｜预警项需人工复核"
            else:
                op, op_note = "观察(不新建)", "预警标的，仅观察、暂不配置新增资金"
        elif is_warn and warn_policy == "half_weight":
            # 半权降配：warn 标的按 tier_weights["warn"]=0.5 权重分配新增资金，可被建议加仓(需人工复核)
            if ex > 0:
                op, op_note = f"加仓(半权) ¥{target_new:,.0f}", f"已持仓 ¥{ex:,.0f}｜预警标的按半权降配，需人工复核"
            else:
                op, op_note = f"新建仓(半权) ¥{target_new:,.0f}", "预警标的，按半权降配、需人工复核"
        elif ex > 0:
            if target_new > 1:
                op, op_note = f"加仓 ¥{target_new:,.0f}", f"已持仓 ¥{ex:,.0f}"
            else:
                op, op_note = "维持", f"已持仓 ¥{ex:,.0f}"
        else:
            op, op_note = f"新建仓 ¥{target_new:,.0f}", "—"
        plan.append({
            "code": r["code"], "name": r["name"], "status": r["status"],
            "score": r.get("score"), "score_weight": score_w_map.get(r["code"]),
            "ratio": ratio, "amount": target_new, "existing": ex,
            "add_cash": target_new, "op": op, "op_note": op_note,
        })

    return {
        "total_planned": total, "gate_lower_pct": lower_pct,
        "gate_upper_pct": upper_pct, "target_pct": target_pct,
        "target_total": target_total, "hard_cap_total": hard_cap_total,
        "existing_total": existing_total, "available_new": available_new,
        "over_cap": over_cap, "over_target": over_target,
        "required_new": sum(p["add_cash"] for p in plan),
        "per_stock_cap": cap, "n": len(final_all),
        "warn_policy": warn_policy,
        "equal_ratio": (target_pct / len(final_all)) if final_all else 0,
        "stocks": plan,
    }


def main():
    MODE, POOL_DATE, DEGRADE_REASON = detect_mode()
    print(f"[{REPORT_DATE_CN}] 全市场初选模式（westock-mcp 驱动）... mode={MODE}")

    # 降级标记文件：mx 授权失败/初选未跑时写入，正常模式自动清除
    if MODE == "degraded":
        flag = {
            "date": REPORT_DATE_CN,
            "pool_date": POOL_DATE,
            "reason": DEGRADE_REASON,
            "message": ("全市场初选未执行或数据源授权失败（主源 westock-mcp tool_filter / 备用 mx-ds-mcp 均不可用），"
                        "已自动降级沿用昨日初选池做财务复核。请检查连接器授权，"
                        "次日自动化将恢复正常全市场初选。"),
        }
        with open(DEGRADED_FLAG, "w", encoding="utf-8") as f:
            json.dump(flag, f, ensure_ascii=False, indent=2)
        print(f"[WARN] 降级模式：{flag['message']}")
    else:
        if os.path.exists(DEGRADED_FLAG):
            os.remove(DEGRADED_FLAG)

    universe = load_pool()

    growth = [u for u in universe if not u["is_cyclical"]]
    cyclical = [u for u in universe if u["is_cyclical"]]

    for u in growth:
        g = u.get("growth") or 0
        u["peg"] = (u["pe"] / g) if g > 0 else 999
    growth.sort(key=lambda x: x["peg"])
    cyclical.sort(key=lambda x: x.get("pb") or 999)

    growth_sel = growth[:GROWTH_TOP]
    cyclical_sel = cyclical[:CYCLICAL_TOP]

    print(f"发现池：成长 {len(growth)} / 周期 {len(cyclical)}；"
          f"入选复核：成长 {len(growth_sel)} / 周期 {len(cyclical_sel)}")

    all_codes = [to_westock_code(u["code"]) for u in growth_sel + cyclical_sel]
    sd = get_stock_data(all_codes)

    growth_assessed = [assess(to_westock_code(u["code"]), u["name"], u["sw"], sd.get(to_westock_code(u["code"]), {}))
                      for u in growth_sel]
    cyclical_assessed = [assess(to_westock_code(u["code"]), u["name"], u["sw"], sd.get(to_westock_code(u["code"]), {}))
                        for u in cyclical_sel]

    growth_assessed.sort(key=lambda x: (x["status"] == "reject", x.get("peg") is None, x.get("peg") or 999))
    cyclical_assessed.sort(key=lambda x: x.get("PB") or 999)

    passed = [r for r in growth_assessed if r["status"] == "pass"]
    warned = [r for r in growth_assessed if r["status"] == "warn"]
    rejected = [r for r in growth_assessed if r["status"] == "reject"]

    # 方案1：择时门控——根据市场趋势/情绪动态控制成长线入选上限
    gate = load_market_gate()
    growth_limit = gate.get("growth_limit", 8)

    # 同板块去重（申万一级行业，每板块仅保留综合最优 max_per_sector 只）—— 2026-08-31 新增，配置化
    # 候选集：成长线 + 周期/价值线 全部 pass/warn；去重后按原排序截到总上限。
    # 仅作用于最终备选/推荐列表（stage=final），初选池(growth_html/cyclical_html)保留全量。
    # 候选集标记 is_cyclical，供同板块去重按"成长线用PEG/周期线用PB"正确分流
    for _r in growth_assessed:
        _r.setdefault("is_cyclical", False)
    for _r in cyclical_assessed:
        _r.setdefault("is_cyclical", True)
    candidates = [r for r in growth_assessed + cyclical_assessed if r["status"] in ("pass", "warn")]
    _scfg = load_sector_config()
    if _scfg.get("enabled"):
        candidates, sector_dropped = dedup_by_sector(
            candidates,
            max_per_sector=_scfg.get("max_per_sector", 1),
            verbose=True,
            stage="最终备选",
        )
        SECTOR_DROPPED.extend(sector_dropped)
    # 总数量门控：去重后按原排序（PEG/PB 升序、pass 在前）截到上限；
    # 原结构 成长上限 + 银行2 + 周期4 ≈ 14，这里用 growth_limit + 6 作为总上限
    FINAL_CAP = growth_limit + 6
    final_all = candidates[:FINAL_CAP]

    # ★ 五项重点复核结构化（2026-09-20）：把原 Layer2B 人工精读的五项维度自动核查
    #   数据源 = westock-data CLI 公告（减持/质押/商誉减值/监管函）+ assess 财务字段（商誉/毛利率）+ 行业代理
    #   否决项（如近期减持 / 商誉>30%减值）移出最终备选，warn 项保留并标红
    _fr_map = {}
    _fr_rejected = []
    try:
        from _five_review import review_all as _fr_review_all
        _fr_map = _fr_review_all(final_all)
        for _r in final_all:
            _r["five_review"] = _fr_map.get(_r["code"])
            if (_r.get("five_review") or {}).get("overall") == "reject":
                _fr_rejected.append(f"{_r['name']}({_r['code']})")
        if _fr_rejected:
            final_all = [r for r in final_all
                         if (r.get("five_review") or {}).get("overall") != "reject"]
            print(f"[五项复核] 否决 {len(_fr_rejected)} 只并移出最终备选：{', '.join(_fr_rejected)}")
        else:
            print(f"[五项复核] 完成 {len(final_all)} 只候选结构化复核，无否决项")
    except Exception as _e:
        print(f"[WARN] 五项复核模块异常（跳过，不影响主流程）：{_e}", file=sys.stderr)

    # 动量因子（UMD）预计算：经典 12-1 月动量，作为复合评分第4维度（配置驱动）
    _fcfg = load_factor_config()
    if _fcfg.get("momentum", {}).get("enabled", True):
        _mom_codes = [to_westock_code(r["code"]) for r in final_all]
        _mom_map = compute_momentum(_mom_codes, _fcfg.get("momentum", {}))
        for _r in final_all:
            _mc = to_westock_code(_r["code"])
            _r["mom"] = _mom_map.get(_mc, {}).get("mom")
            _r["mom_score"] = _mom_map.get(_mc, {}).get("mom_score")

    # 技术指标记录字段（RSI_6 / KDJ_J）：仅作回溯数据落盘，不参与评分、不参与任何判定
    _tech_map = compute_tech_indicators([to_westock_code(r["code"]) for r in final_all])
    for _r in final_all:
        _tc = to_westock_code(_r["code"])
        _tv = _tech_map.get(_tc, {})
        _r["rsi_6"] = _tv.get("rsi_6")
        _r["kdj_j"] = _tv.get("kdj_j")
        _r["tech_date"] = _tv.get("tech_date")

    # 价格成熟度惩罚（method=maturity_penalty）：把 mom / RSI_6 合成 maturity_score(0-100)，
    # 高=已兑现/超买=应惩罚；极端透支标 maturity_warn。供 score_stock 软降权（不硬剔除）。
    _mom_method = _fcfg.get("momentum", {}).get("method")
    if _mom_method in ("maturity_penalty", "maturity_bonus"):
        _mcfg = _fcfg.get("momentum", {})
        _mom_mature = float(_mcfg.get("mom_mature", 0.60))
        _rsi_lo = float(_mcfg.get("rsi_low", 50))
        _rsi_hi = float(_mcfg.get("rsi_high", 80))
        _w_mom = float(_mcfg.get("w_mom", 0.6))
        _w_rsi = float(_mcfg.get("w_rsi", 0.4))
        _warn_mom = float(_mcfg.get("warn_mom", 0.80))
        _warn_rsi = float(_mcfg.get("warn_rsi", 70))
        _warn_mom_down = float(_mcfg.get("warn_mom_down", -0.30))
        _warn_rsi_low = float(_mcfg.get("warn_rsi_low", 30))
        for _r in final_all:
            _mv = _r.get("mom")
            _rv = _r.get("rsi_6")
            if _mv is None and _rv is None:
                _r["maturity_score"] = None
                _r["maturity_warn"] = False
                continue
            _mm = _clamp((_mv or 0.0) / _mom_mature, 0, 1) if _mv is not None else 0.0
            _rr = (_clamp(((_rv or _rsi_lo) - _rsi_lo) / max(1e-6, _rsi_hi - _rsi_lo), 0, 1)
                   if _rv is not None else 0.0)
            _mat = _w_mom * _mm + _w_rsi * _rr
            _r["maturity_score"] = round(_mat * 100, 1)
            if _mom_method == "maturity_penalty":
                # 惩罚模式：已兑现/超买 → 警告（原逻辑）
                _r["maturity_warn"] = bool(
                    (_mv is not None and _mv >= _warn_mom) or
                    (_rv is not None and _rv >= _warn_rsi))
            else:
                # 反转(bonus)模式：极端弱势/超卖 → 警告（正是被降权的对象）
                _r["maturity_warn"] = bool(
                    (_mv is not None and _mv <= _warn_mom_down) or
                    (_rv is not None and _rv <= _warn_rsi_low))

    # 最终备选百分制综合评分（估值35+质量35+安全30+价格成熟度惩罚，分行业差异化）；用于报告排序与展示
    for _r in final_all:
        _r["score_detail"] = score_stock(_r, _fcfg)
        _r["score"] = _r["score_detail"]["score"]
    # 报告按综合评分降序展示（不影响 final_picks 跟踪顺序）
    final_ranked = sorted(final_all, key=lambda r: r.get("score", 0), reverse=True)

    # 硬止损/退出纪律（写入选股标准，非仅监控）：为每只最终备选计算止损价/破位线
    # 建仓参考价 = 当日收盘价（报告日）；回撤≥max_drawdown_pct% 或跌破 break_ma 即强制减仓
    _ercfg = load_exit_rules()
    _ma_codes = [to_westock_code(r["code"]) for r in final_all]
    _ma = get_ma_levels(_ma_codes, 60)
    for _r in final_all:
        _c = to_westock_code(_r["code"])
        _close = (sd.get(_c) or {}).get("close") or (_ma.get(_c) or {}).get("close")
        _mr = _ma.get(_c, {})
        _break = _mr.get("ma20") if _ercfg.get("break_ma") == "MA20" else _mr.get("ma60")
        _stop = round(_close * (1 - _ercfg.get("max_drawdown_pct", 8) / 100.0), 3) if _close else None
        _r["exit_rule"] = {
            "entry_ref": round(_close, 3) if _close else None,
            "stop_price": _stop,
            "break_ma": _ercfg.get("break_ma"),
            "break_price": round(_break, 3) if _break else None,
            "max_drawdown_pct": _ercfg.get("max_drawdown_pct", 8),
            "rule": (f"回撤≥{_ercfg.get('max_drawdown_pct', 8)}% 或跌破{_ercfg.get('break_ma', 'MA20')}"
                     f"（≈¥{round(_break, 3) if _break else 'NA'}）即减仓"
                     ) if _close else "收盘价缺失，无法计算止损线",
        }

    # 持仓建议：计划总仓 + 现有真实持仓 → 每只入选标的建议占比/金额
    # （注意：plan 在下方择时门控算出 upper_pct 后再计算，避免无视总仓上限）

    # 市场环境
    summary = run_cli(["market-overview", "--raw"])
    detail = run_cli(["market-overview", "--type", "valuation,technical,updown", "--raw"])

    def row_of(obj, idx=0):
        if isinstance(obj, list) and len(obj) > idx and isinstance(obj[idx], dict):
            return obj[idx].get("row", {})
        if isinstance(obj, dict) and isinstance(obj.get("data"), list) and len(obj["data"]) > idx:
            d = obj["data"][idx]
            return d.get("row", d) if isinstance(d, dict) else {}
        return {}

    srow = row_of(summary)
    vrow = row_of(detail, 0)
    trow = row_of(detail, 1)
    urow = row_of(detail, 2)

    adj_score = safe_float(srow.get("ADJ_SCORE"), 50)
    pe_pct = safe_float(vrow.get("PE_TTM_PCT_10Y"), 50)
    sentiment = srow.get("SENTIMENT_STATUS", "")

    # 择时门控已由 load_market_gate() 量化（heat 反指）给出 position_range/action/style，
    # 替代原定性"情绪狂热/恐慌"分支决策；growth_limit 取自量化门控（含手动覆盖）
    position_range, action, style = gate["position_range"], gate["action"], gate["style"]

    # 持仓建议：用择时门控的总仓区间约束金额表，避免一天打满总仓
    lower_pct, upper_pct = parse_position_band(position_range)
    plan = compute_position_plan(final_all, lower_pct, upper_pct)

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()

    if MODE == "full":
        src_banner = ('<div class="risk-alert" style="border-left:4px solid #1D9E75;background:#04342C">'
                      '<strong>✅ 全市场初选模式（westock-mcp 驱动）：</strong>'
                      '第一层全市场初选由「东方财富妙想 MCP」多条件筛选完成（替代已下架的 westock-tool），'
                      '覆盖 A股 成长/制造（PEG）与周期/价值（PB）双线；财务复核与风险筛查由 westock-data 完成。</div>')
    else:
        src_banner = ('<div class="risk-alert" style="border-left:4px solid #E0533D;background:#3A1410">'
                      '<strong>⚠️ 降级模式（mx-ds-mcp 授权失败）：</strong>'
                      f'东方财富妙想 MCP（mx-ds-mcp）今日初选未成功执行（最后有效初选日期：{POOL_DATE or "未知"}），'
                      '本报告自动降级沿用<strong>昨日存量初选池</strong>做财务复核，不构成今日全市场初选结果。'
                      '请重新授权 mx-ds-mcp 连接器，次日自动化将恢复正常全市场初选。'
                      '（本降级为兜底机制，不会包含当日新进入筛选范围的标的。）</div>')

    def tag(status):
        return {"pass": '<span class="tag tag-pass">✅ 通过</span>',
                "warn": '<span class="tag tag-warn">⚠️ 预警</span>',
                "reject": '<span class="tag tag-fail">❌ 剔除</span>'}[status]

    growth_html = src_banner
    growth_html += '<div class="industry-group"><h4>成长/制造股线（PEG 盈利指标体系，按PEG升序）</h4><table><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>PE_TTM</th><th>PB</th><th>扣非增速</th><th>PEG</th><th>ROE_TTM(%)</th><th>风险标记</th><th>原因</th></tr></thead><tbody>'
    for r in growth_assessed:
        peg = f"{r['peg']:.3f}" if r.get("peg") else "-"
        g = r.get("np_deduct_growth")
        gstr = f"{g*100:.1f}%" if g is not None else "-"
        rs = "；".join(r["reasons"]) if r["reasons"] else "-"
        growth_html += (f'<tr><td>{r["code"]}</td><td>{r["name"]}</td><td>{r["industry"]}</td>'
                        f'<td>{r.get("PE_TTM",0):.1f}</td><td>{r.get("PB",0):.2f}</td><td>{gstr}</td>'
                        f'<td>{peg}</td><td>{r.get("ROE_TTM",0):.1f}</td><td>{tag(r["status"])}</td><td>{rs}</td></tr>')
    growth_html += '</tbody></table></div>'

    cyclical_html = '<div class="industry-group"><h4>周期/价值股线（PB 估值体系，按PB升序）</h4><table><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>PB</th><th>PE_TTM</th><th>ROE_TTM(%)</th><th>风险标记</th></tr></thead><tbody>'
    for r in cyclical_assessed:
        cyclical_html += (f'<tr><td>{r["code"]}</td><td>{r["name"]}</td><td>{r["industry"]}</td>'
                          f'<td>{r.get("PB",0):.2f}</td><td>{r.get("PE_TTM",0):.1f}</td>'
                          f'<td>{r.get("ROE_TTM",0):.1f}</td><td>{tag(r["status"])}</td></tr>')
    cyclical_html += '</tbody></table></div>'

    gate_banner = (f'<p style="font-size:13px;color:#8b949e;margin-bottom:10px;">'
                   f'【择时门控·量化】市场热度 heat={gate["heat"]:.0f}/100 '
                   f'（估值分{gate["valuation_score"]:.0f}/情绪分{gate["sentiment_score"]:.0f}/'
                   f'量能分{gate["volume_energy_score"]:.0f}/宽度分{gate["stock_width_score"]:.0f}）'
                   f' ｜ 区间判定：{gate["regime_label"]} ｜ 成长线上限：{growth_limit}只 ｜ '
                   f'策略：{"防御优先" if gate["defense_boost"] else "均衡配置"} ｜ 原因：{gate["reason"]}</p>')
    plan_map = {p["code"]: p for p in (plan["stocks"] if plan else [])}
    # 一致性自检：被建议配置新增资金的标的，其 reject_reasons 必须为空
    # （防止出现"选股侧建议加仓 / 退出侧建议退出"的同名矛盾）
    _consistency_ok = True
    for r in final_all:
        pm = plan_map.get(r["code"])
        if pm and pm["amount"] > 1 and r.get("reject_reasons"):
            _consistency_ok = False
            print(f"[WARN] 一致性自检失败：{r['name']} 被建议配置 ¥{pm['amount']:,.0f} "
                  f"但存在剔除理由 {r['reject_reasons']}")
    if _consistency_ok:
        print("[OK] 一致性自检通过：无标的既被分配新增资金又被标记剔除")
    final_html = gate_banner + ('<p style="font-size:12px;color:#8b949e;margin-bottom:6px;">'
        '【综合评分·百分制】估值吸引力(35) + 盈利质量(35) + 财务安全(30)，分行业差异化评估'
        '（成长线用PEG / 周期·价值·金融线用PB）。价格成熟度惩罚（原 UMD 维度翻转）已于 2026-09-09 落地：'
        '12-1月涨幅越大、RSI_6 越高→该维得分越低（软降权），即对已涨/超买票降权；'
        '极端透支（mom≥80% 或 RSI≥70）标 ⚠️。纯模型推演，非投资建议。下表按评分降序排列。</p>'
        '<table><thead><tr><th>代码</th><th>名称</th><th>综合评分</th><th>价格成熟度惩罚</th><th>行业</th>'
        '<th>关键指标</th><th>增长来源判定</th><th>建议占比/金额</th>'
        '<th>止损/破位纪律</th><th>风险标记</th></tr></thead><tbody>')
    for r in final_ranked:
        er = r.get("exit_rule") or {}
        if er.get("stop_price") is not None:
            _bpx = er.get("break_price")
            _broken = (_bpx is not None and er.get("entry_ref") is not None and _bpx < er["entry_ref"])
            exit_txt = (f'<td style="font-size:12px;line-height:1.5">'
                        f'止损¥{er["stop_price"]}<br>破位¥{_bpx}（{er.get("break_ma")}）<br>'
                        f'<span style="color:{"#E24B4A" if _broken else "#8b949e"}">'
                        f'回撤≥{er.get("max_drawdown_pct")}%</span></td>')
        else:
            exit_txt = '<td style="font-size:12px;color:#888">—</td>'
        if r.get("peg"):
            key = f"PE={r['PE_TTM']:.1f}, 扣非增速={(r.get('np_deduct_growth') or 0)*100:.1f}%, PEG={r['peg']:.3f}, ROE={r.get('ROE_TTM',0):.1f}%"
            src = "主营业务内生增长（PEG筛选进入）"
        else:
            key = f"PB={r.get('PB',0):.2f}, PE={r.get('PE_TTM',0):.1f}, ROE={r.get('ROE_TTM',0):.1f}%"
            src = "低 PB 价值防御（银行/周期）"
        pm = plan_map.get(r["code"])
        if pm:
            sug = f"{pm['ratio']*100:.1f}% ｜ ¥{pm['amount']:,.0f}<br><span style='font-size:11px;color:#888'>{pm['op']}（{pm['op_note']}）</span>"
        else:
            sug = "-"
        _mat = r.get("maturity_score")
        if _mat is not None:
            _warn = " ⚠️" if r.get("maturity_warn") else ""
            _mom_txt = (f'<td style="font-size:12px;line-height:1.4">成熟度<b>{_mat}</b>{_warn}<br>'
                        f'12-1月 {(r.get("mom") or 0)*100:.1f}%｜RSI{r.get("rsi_6") or "—"}{_warn}</td>')
        else:
            _mom_txt = '<td style="font-size:12px;color:#888">—</td>'
        final_html += (f'<tr><td>{r["code"]}</td><td>{r["name"]}</td>{score_cell(r)}{_mom_txt}'
                       f'<td>{r["industry"]}</td><td>{key}</td><td>{src}</td>'
                       f'<td>{sug}</td>{exit_txt}<td>{tag(r["status"])}</td></tr>')
    final_html += '</tbody></table>'
    if SECTOR_DROPPED:
        _drops = "; ".join(f"{r.get('name')}({r.get('_sector_dropped')})" for r in SECTOR_DROPPED)
        final_html += (f'<p style="font-size:12px;color:#8b949e;margin-top:8px;">'
                       f'【同板块去重】每申万一级行业仅保留综合最优 {_scfg.get("max_per_sector", 1)} 只（pass优先→估值最优→ROE最高），'
                       f'已剔除同板块其他 {len(SECTOR_DROPPED)} 只：{_drops}</p>')
    # 硬止损/退出纪律说明（写入选股标准，非仅监控）
    if _ercfg.get("enabled"):
        final_html += (f'<p style="font-size:12px;color:#E24B4A;margin-top:8px;">'
                       f'【硬止损/退出纪律（强制执行）】每只标的以报告日收盘价作为建仓参考价，'
                       f'个股从建仓价<b>回撤≥{_ercfg.get("max_drawdown_pct", 8)}%</b> 或收盘价<b>跌破{_ercfg.get("break_ma", "MA20")}</b>'
                       f'即强制减仓；持有超 {_ercfg.get("time_stop_days", 60)} 日且浮亏≥{_ercfg.get("time_stop_min_pct", -10)}% 即评估退出。'
                       f'止损价/破位线见上表"止损/破位纪律"列。以上为模型纪律，非实操建议。</p>')

    review_items = []
    for r in final_all:
        notes = []
        if r.get("goodwill_ratio", 0) > 0.1:
            notes.append(f"商誉占净资产 {r['goodwill_ratio']:.1%}，关注后续减值")
        if r.get("industry") == "医药生物":
            notes.append("关注集采/监管政策风险")
        ind0 = r.get("industry", "").split("-")[0]
        if ind0 in POLICY_RISK_INDUSTRIES and ind0 != "医药生物":
            notes.append("关注行业政策/景气拐点风险（强政策敏感行业）")
        if "银行" not in r.get("industry", "") and r.get("receivables_ratio", 0) > 0.15:
            notes.append(f"应收账款占营收 {r['receivables_ratio']:.1%}，关注上下游议价拐点")
        if r.get("gm_declining_quarters", 0) >= 2:
            notes.append("毛利率连续下滑，警惕基本面反转")
        _fr = r.get("five_review")
        if _fr and _fr.get("items"):
            _lvl_map = {"pass": "✅", "warn": "⚠️", "reject": "❌"}
            _name_map = {"goodwill": "商誉", "policy": "政策", "holder": "股东", "margin": "毛利", "overseas": "汇率"}
            _fr_badges = [f"{_name_map.get(k, k)}{_lvl_map.get(d['level'], '?')}"
                          for k, d in _fr["items"].items() if d["level"] != "pass"]
            if _fr_badges:
                notes.append("五项复核：" + " ".join(_fr_badges))
        if not notes:
            notes.append("未发现明显重点复核风险信号")
        review_items.append(f"<li><strong>{r['name']}</strong>：{'; '.join(notes)}</li>")
    review_html = "<ul>" + "".join(review_items) + "</ul>"

    market_html = f"""
    <div class="summary-stats">
      <div class="stat-item"><div class="stat-value">{srow.get('ADJ_SCORE','')}</div><div class="stat-label">市场画像调整评分</div></div>
      <div class="stat-item"><div class="stat-value">{vrow.get('PE_TTM_PCT_10Y','')}%</div><div class="stat-label">中证全指PE十年分位</div></div>
      <div class="stat-item"><div class="stat-value">{urow.get('RATIO_UP','')}%</div><div class="stat-label">上涨个股占比</div></div>
      <div class="stat-item"><div class="stat-value">{urow.get('CNT_REACH_UPLIMIT','')}</div><div class="stat-label">涨停家数</div></div>
    </div>
    <p><strong>估值：</strong>中证全指 PE_TTM {vrow.get('PE_TTM','')}，十年分位 {vrow.get('PE_TTM_PCT_10Y','')}%；PB_LF {vrow.get('PB_LF','')}。</p>
    <p><strong>技术：</strong>MACD={trow.get('MACD','')}，DIF={trow.get('DIF','')}，DEA={trow.get('DEA','')}，RSI6={trow.get('RSI_6','')}；短期趋势{srow.get('TREND_SHORT_DIRECTION_STATUS','')}，趋势强度{srow.get('TREND_SHORT_STRENGTH_STATUS','')}。</p>
    <p><strong>情绪：</strong>{sentiment}；涨跌比 {urow.get('RATIO_UPDOWN','')}。</p>
    <p><strong>风格：</strong>{srow.get('STYLE_ROTATION_STATUS','')}，{srow.get('CAP_ROTATION_STATUS','')}，板块{srow.get('SECTOR_WIDTH_STATUS','')}。</p>
    <div class="highlight-box"><strong>行情形态预判：</strong>当前延续"高估值（PE十年分位{pe_pct:.0f}%）+情绪{sentiment}"组合。高估值区间叠加情绪过热时，反弹持续性有限，需警惕二次下跌；若后续缩量回调且情绪重回恐慌区间，则可能形成二次下跌黄金机会。历史对标仅作参考，需结合流动性与情绪位置修正。</div><p style="font-size:12px;color:#8b949e;margin-top:6px;">注：当前财报数据窗口为 Q1（3/31），Q2 半年报预计 8/31 前披露；模型严格仅使用已披露数据，无未来函数风险。</p>
    """

    # 持仓建议卡片 + 明细表（计划总仓 + 现有真实持仓）
    capital_card = ""
    plan_html = ""
    if plan:
        capital_card = (f'<div class="position-card"><div class="label">计划总资金</div>'
                        f'<div class="value">¥{plan["total_planned"]:,.0f}</div>'
                        f'<div class="action">已持仓 ¥{plan["existing_total"]:,.0f} ｜ 本轮可新增 ¥{plan["available_new"]:,.0f}</div></div>')
        rows = []
        for p in plan["stocks"]:
            rows.append(
                f'<tr><td>{p["name"]}</td><td>{p["status"]}</td>'
                f'<td style="text-align:right">{p["ratio"]*100:.1f}%</td>'
                f'<td style="text-align:right">¥{p["amount"]:,.0f}</td>'
                f'<td style="text-align:right">{p["op_note"]}</td>'
                f'<td>{p["op"]}</td></tr>'
            )
        add_sum = sum(p["add_cash"] for p in plan["stocks"])
        # 超配减仓提示（2026-09-01）：过热区间下调总仓后，现有持仓已超硬上限时必须明确"该减多少"，
        # 否则报告只显示"可新增 ¥0"，与择时给出的"逐步减仓"动作脱节。
        reduce_note = ""
        if plan.get("over_cap", 0) > 1:
            reduce_note = (
                f'<p style="font-size:13px;color:#c92a2a;font-weight:600;margin-top:10px;'
                f'padding:10px;background:#fff5f5;border-left:4px solid #c92a2a;border-radius:4px;">'
                f'🔻 <b>需减仓 ¥{plan["over_cap"]:,.0f}</b>：当前已持仓 ¥{plan["existing_total"]:,.0f}，'
                f'已<b>超过</b>本轮择时区间硬上限 ¥{plan["hard_cap_total"]:,.0f}'
                f'（{plan["gate_upper_pct"]*100:.0f}%）。'
                f'若回落至区间中点 {plan["target_pct"]*100:.0f}%（¥{plan["target_total"]:,.0f}），'
                f'则需削减 ¥{plan.get("over_target", 0):,.0f}。'
                f'本轮不新增任何仓位，减仓优先从<b>价格成熟度高分（已兑现/超买）、评分偏弱</b>的标的开始。</p>')
        elif plan.get("over_target", 0) > 1:
            reduce_note = (
                f'<p style="font-size:12px;color:#e8590c;margin-top:10px;">'
                f'⚠️ 当前已持仓 ¥{plan["existing_total"]:,.0f} 高于本轮目标中点 ¥{plan["target_total"]:,.0f}'
                f'（尚未触及硬上限 ¥{plan["hard_cap_total"]:,.0f}）：本轮<b>不新增</b>，'
                f'建议逢强削减约 ¥{plan["over_target"]:,.0f} 向目标仓位收敛。</p>')
        over_note = ""
        if plan["required_new"] > plan["available_new"] + 1:
            over_note = (f'<p style="font-size:12px;color:#e8590c;margin-top:8px;">'
                         f'⚠️ 本轮新建/加仓所需新增资金合计 ¥{plan["required_new"]:,.0f}，'
                         f'已超本轮可新增额度 ¥{plan["available_new"]:,.0f}。'
                         f'请优先满足 pass 标的，或按比例缩减；warn 标的已自动降权。</p>')
        plan_html = (
            f'<div class="highlight-box" style="margin-top:14px;">'
            f'<strong>📊 持仓建议（计划总仓 ¥{plan["total_planned"]:,.0f} 口径）</strong>'
            f'<p style="font-size:12px;color:#666;margin:6px 0;">本轮择时建议总仓位区间 '
            f'<b>{plan["gate_lower_pct"]*100:.0f}%-{plan["gate_upper_pct"]*100:.0f}%</b>，'
            f'本轮目标部署至区间中点 <b>{plan["target_pct"]*100:.0f}%</b>（市值 ¥{plan["target_total"]:,.0f}，硬上限 ¥{plan["hard_cap_total"]:,.0f}）；'
            f'当前已持仓 ¥{plan["existing_total"]:,.0f}，<b>本轮可新增约 ¥{plan["available_new"]:,.0f}</b>。'
            f'共 {plan["n"]} 只入选标的，按综合评分加权分配（优秀1.5×／良好1.2×／中性1.0×／偏弱0.5×，'
            f'仍受单票{plan["per_stock_cap"]*100:.0f}%封顶）；高评分标的拿更多、低评分拿更少，alpha 集中到确定性高的票。单票新增占比见下表。'
            f'本轮仅使用择时允许的预算、部署至区间中点，<b>未打满总仓</b>；若明日模型出现更优标的，将从剩余可投额度中分配，不会突破总仓上限。</p>'
            f'<table><thead><tr><th>标的</th><th>状态</th><th>建议占比(本轮新增)</th><th>建议金额(本轮新增)</th><th>已持仓</th><th>操作</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>{reduce_note}{over_note}'
            f'<p style="font-size:11px;color:#999;margin-top:6px;">注：「建议占比/金额」为<b>本轮新增</b>部分（占计划总仓口径），已持仓标的历史仓位不重复计算；整体受择时门控总仓区间约束。仅作推演参考，不构成实操建议。</p>'
            f'</div>'
        )

    position_html = f"""
    <div class="position-card"><div class="label">建议总仓位</div><div class="value">{position_range}</div><div class="action">{action}</div></div>
    <div class="position-card"><div class="label">风格配置</div><div class="value">{style.split('、')[0]}</div><div class="action">{'、'.join(style.split('、')[1:])}</div></div>
    {capital_card}
    </div>
    """
    position_detail = """
    <p><strong>加仓条件：</strong>市场缩量回调、情绪回落至恐慌区间、中证全指PE分位降至70%以下且流动性维持中性偏松。</p>
    <p><strong>减仓条件：</strong>情绪持续狂热但成交量萎缩、MACD 死叉确认、估值分位维持85%以上。</p>
    <p><strong>黄金机会触发判断：</strong>当前未触发（需同时满足：经历一轮下跌反弹、反弹乏力缩量、情绪回恐慌、流动性未恶化）。</p>
    """

    html = html.replace("{{DATE}}", REPORT_DATE_CN)
    html = html.replace("{{TOTAL_GROWTH}}", str(len(growth_assessed)))
    html = html.replace("{{TOTAL_CYCLICAL}}", str(len(cyclical_assessed)))
    html = html.replace("{{TOTAL_PASSED}}", str(len(passed)))
    html = html.replace("{{TOTAL_REJECTED}}", str(len(rejected)))
    html = html.replace("<!-- GROWTH_STOCKS_HTML -->", growth_html)
    html = html.replace("<!-- CYCLICAL_STOCKS_HTML -->", cyclical_html)
    html = html.replace("<!-- FINAL_PICKS_HTML -->", final_html)
    html = html.replace("<!-- KEY_REVIEW_HTML -->", review_html)
    html = html.replace("<!-- MARKET_ANALYSIS_HTML -->", market_html)
    html = html.replace("<!-- POSITION_CARDS_HTML -->", position_html)
    html = html.replace("<!-- POSITION_DETAIL_HTML -->", position_detail)
    html = html.replace("<!-- POSITION_PLAN_HTML -->", plan_html)

    out_path = os.path.join(WORKSPACE, f"量化选股模型报告_{REPORT_DATE}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"报告已保存：{out_path}")

    # detail 同时包含成长线 + 周期/价值线（银行/券商/基建等），供 monitor_exits 完整匹配真实持仓状态
    growth_keys = ("code", "name", "status", "industry", "PE_TTM", "PB", "ROE_TTM", "peg",
                   "np_deduct_growth", "rev_yoy", "np_yoy", "recv_yoy",
                   "receivables_ratio", "ocf_ttm", "reject_reasons", "warn_notes", "reasons",
                   "close_date", "mom", "mom_score", "maturity_score", "maturity_warn",
                   "rsi_6", "kdj_j", "tech_date")
    cyclical_keys = ("code", "name", "status", "industry", "PE_TTM", "PB", "ROE_TTM",
                     "reject_reasons", "warn_notes", "reasons", "close_date",
                     "mom", "mom_score", "maturity_score", "maturity_warn",
                     "rsi_6", "kdj_j", "tech_date")
    _detail = [{k: r.get(k) for k in growth_keys} for r in growth_assessed] + \
              [{k: r.get(k) for k in cyclical_keys} for r in cyclical_assessed]

    summary_obj = {
        "date": REPORT_DATE_CN, "mode": MODE,
        "pool_date": POOL_DATE, "degraded_reason": DEGRADE_REASON,
        "growth_count": len(growth_assessed), "cyclical_count": len(cyclical_assessed),
        "passed": len(passed), "warned": len(warned), "rejected": len(rejected),
        "final_picks": [f"{r['code']} {r['name']}" for r in final_all],
        "rejected_names": [f"{r['code']} {r['name']}({'；'.join(r['reasons'])})" for r in rejected],
        "position": position_range, "action": action, "style": style,
        "market_adj_score": adj_score, "pe_pct_10y": pe_pct, "sentiment": sentiment,
        "trend": srow.get("TREND_SHORT_DIRECTION_STATUS", ""),
        "ratio_up": urow.get("RATIO_UP", ""), "uplimit": urow.get("CNT_REACH_UPLIMIT", ""),
        "close_date": (sd.get(all_codes[0]) or {}).get("close_date", ""),
        "file": out_path,
        "market_gate": gate,
        "market_regime": {
            "heat": gate.get("heat"), "valuation_score": gate.get("valuation_score"),
            "sentiment_score": gate.get("sentiment_score"),
            "volume_energy_score": gate.get("volume_energy_score"),
            "stock_width_score": gate.get("stock_width_score"),
            "regime_label": gate.get("regime_label"),
            "contrarian_note": gate.get("reason"),
        },
        "exit_rules": _ercfg,
        "factor_momentum": {
            "enabled": _fcfg.get("momentum", {}).get("enabled", True),
            "method": _fcfg.get("momentum", {}).get("method", "umd"),
            "use_in_score": _fcfg.get("momentum", {}).get("use_in_score", True),
            "lookback_months": _fcfg.get("momentum", {}).get("lookback_months", 12),
            "skip_months": _fcfg.get("momentum", {}).get("skip_months", 1),
            "points": _fcfg.get("momentum", {}).get("points", 12),
            "strong_up": _fcfg.get("momentum", {}).get("strong_up", 0.50),
            "strong_down": _fcfg.get("momentum", {}).get("strong_down", -0.30),
            "mom_mature": _fcfg.get("momentum", {}).get("mom_mature"),
            "rsi_low": _fcfg.get("momentum", {}).get("rsi_low"),
            "rsi_high": _fcfg.get("momentum", {}).get("rsi_high"),
            "w_mom": _fcfg.get("momentum", {}).get("w_mom"),
            "w_rsi": _fcfg.get("momentum", {}).get("w_rsi"),
            "warn_mom": _fcfg.get("momentum", {}).get("warn_mom"),
            "warn_rsi": _fcfg.get("momentum", {}).get("warn_rsi"),
            "record_only": {"mom": True, "rsi_6": True, "kdj_j": True},
        },
        "exit_levels": [
            {"code": r["code"], "name": r["name"],
             "entry_ref": (r.get("exit_rule") or {}).get("entry_ref"),
             "stop_price": (r.get("exit_rule") or {}).get("stop_price"),
             "break_ma": (r.get("exit_rule") or {}).get("break_ma"),
             "break_price": (r.get("exit_rule") or {}).get("break_price"),
             "max_drawdown_pct": (r.get("exit_rule") or {}).get("max_drawdown_pct")}
            for r in final_ranked
        ],
        "position_plan": plan,
        "board_excluded": sorted(load_excluded_boards()),
        "board_blocked": BOARD_BLOCKED,
        "sector_dedup": {
            "enabled": _scfg.get("enabled"),
            "granularity": _scfg.get("granularity"),
            "max_per_sector": _scfg.get("max_per_sector"),
            "keep_criterion": _scfg.get("keep_criterion"),
        },
        "sector_dropped": [f"{r.get('name')}({r.get('_sector_dropped')})" for r in SECTOR_DROPPED],
        "final_scores": [
            {"code": r["code"], "name": r["name"], "score": r.get("score"),
             "grade": r.get("score_detail", {}).get("grade"),
             "is_cyclical": r.get("is_cyclical"),
             "val_score": r.get("score_detail", {}).get("val_score"),
             "qual_score": r.get("score_detail", {}).get("qual_score"),
             "safe_score": r.get("score_detail", {}).get("safe_score"),
             "mom_score": r.get("score_detail", {}).get("mom_score"),
             "maturity_score": r.get("score_detail", {}).get("maturity_score"),
             "maturity_warn": r.get("maturity_warn"),
             "mom": r.get("score_detail", {}).get("mom"),
             "rsi_6": r.get("rsi_6"),
             "kdj_j": r.get("kdj_j"),
             "tech_date": r.get("tech_date"),
             "penalty": r.get("score_detail", {}).get("penalty"),
             "five_review": (r.get("five_review") or {}).get("overall")}
            for r in final_ranked
        ],
        "five_review": _fr_map if "_fr_map" in locals() else {},
        "five_review_rejected": _fr_rejected if "_fr_rejected" in locals() else [],
        "detail": _detail,
        "cyclical_detail": [{k: r.get(k) for k in cyclical_keys} for r in cyclical_assessed],
    }
    with open(os.path.join(WORKSPACE, "_quant_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary_obj, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary_obj, ensure_ascii=False))


if __name__ == "__main__":
    main()
