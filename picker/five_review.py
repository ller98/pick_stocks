# -*- coding: utf-8 -*-
"""
五项重点复核 · 结构化模块 (2026-09-20)
把原 Layer2B 人工精读的「五项重点复核维度」结构化进选股模型：
  ① 商誉 / 股权激励减值风险
  ② 行业政策风险
  ③ 大股东质押 / 减持计划
  ④ 上下游议价拐点（毛利率持续下滑）
  ⑤ 海外业务占比高 → 汇率扰动
数据源：westock-data CLI `notice list <code>`（公告标题覆盖 ①③④ 风险信号）+
        assess() 已算的 goodwill_ratio / gm_declining_quarters（财务维度）+
        行业代理（政策敏感行业 / 出口导向行业）
设计原则：全部配置驱动、降级不阻断（CLI 取不到→标"需人工复核"）、复用主脚本 run_cli 取数方式。
"""
import os
import json
import re
import subprocess
import sys
from datetime import datetime

WORKSPACE = os.path.dirname(os.path.abspath(__file__))

# —— 公告标题关键词 ——
KW = {
    "reduce":    ["减持"],                                  # 大股东/高管减持计划
    "pledge":    ["质押"],                                  # 股权质押
    "incentive": ["员工持股计划", "股权激励", "限制性股票", "股票期权"],  # 股权激励（中性偏正，大规模解锁需关注）
    "goodwill":  ["商誉减值", "计提商誉"],                  # 商誉减值公告
    "regulate":  ["问询函", "监管函", "立案", "行政处罚", "通报批评"],     # 监管风险
}

# —— 政策敏感行业（坑15 集合，申万一级） ——
POLICY_RISK_INDUSTRIES = [
    "房地产", "医药生物", "农林牧渔", "食品饮料", "传媒",
    "基础化工", "石油石化", "有色金属", "钢铁", "煤炭", "建筑装饰",
    "电力设备", "国防军工", "计算机", "通信",
]

# —— 出口导向（海外汇率敞口）行业，申万一级 ——
OVERSEAS_INDUSTRIES = [
    "纺织服饰", "汽车", "机械设备", "家用电器", "电子",
    "轻工制造", "商贸零售", "航运港口",
]

NOTICE_WINDOW_DAYS = 180   # 近 180 天公告视为有效信号
NOTICE_LIMIT = 20          # 每只拉取公告条数


def to_westock_code(code):
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8", "9")):
        return "bj" + code
    return "sh" + code


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def run_cli(args):
    cmd = ["npx", "-y", "westock-data-skillhub@1.0.5"] + args
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="ignore", shell=True, timeout=90)
    except Exception as e:
        print(f"[five_review] CLI 调用失败 {' '.join(args)}: {e}", file=sys.stderr)
        return None
    out = (res.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        return None


def get_notices(code, limit=NOTICE_LIMIT):
    raw = run_cli(["notice", "list", code, "--limit", str(limit), "--raw"])
    if not raw:
        return []
    items = []
    if isinstance(raw, dict):
        d = raw.get("data")
        if isinstance(d, dict):
            items = d.get("data") or []
        elif isinstance(d, list):
            items = d
    elif isinstance(raw, list):
        items = raw
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def _notice_date(it):
    t = it.get("time") or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + "-" + m.group(2) + "-" + m.group(3), "%Y-%m-%d")
    except Exception:
        return None


def _scan(notices, cats):
    """扫描公告，返回 {cat: [命中...]}"""
    now = datetime.now()
    hits = {c: [] for c in cats}
    for it in notices:
        title = it.get("title", "")
        dt = _notice_date(it)
        recent = (dt is None) or ((now - dt).days <= NOTICE_WINDOW_DAYS)
        for cat in cats:
            if any(kw in title for kw in KW[cat]):
                hits[cat].append({"title": title, "time": it.get("time", ""), "recent": recent})
    return hits


def review_stock(m, notice_limit=NOTICE_LIMIT):
    """对单只 assess 结果 m 做五项复核，返回结构化结论 dict"""
    code = m.get("code", "")
    name = m.get("name", code)
    industry = m.get("sw") or m.get("industry") or ""
    wc = to_westock_code(code)
    notices = get_notices(wc, notice_limit)
    cats = ["reduce", "pledge", "incentive", "goodwill", "regulate"]
    hits = _scan(notices, cats)

    items = {}
    # ① 商誉 / 减值
    gr = safe_float(m.get("goodwill_ratio"))
    g_ev, g_lvl = [], "pass"
    if gr > 0.30:
        g_lvl = "reject"; g_ev.append(f"商誉/净资产={gr*100:.0f}%>30%，高减值风险")
    elif gr > 0.15:
        g_lvl = "warn"; g_ev.append(f"商誉/净资产={gr*100:.0f}%偏高")
    if any(h["recent"] for h in hits["goodwill"]):
        g_lvl = "reject" if g_lvl == "reject" else "warn"
        g_ev.append("近期商誉减值公告：" + hits["goodwill"][0]["title"])
    if not g_ev:
        g_ev.append("商誉占比正常，无减值公告")
    items["goodwill"] = {"level": g_lvl, "evidence": g_ev}

    # ② 行业政策
    p_ev, p_lvl = [], "pass"
    if any(ind in industry for ind in POLICY_RISK_INDUSTRIES):
        p_lvl = "warn"; p_ev.append(f"政策敏感行业（{industry}）")
    if any(h["recent"] for h in hits["regulate"]):
        p_lvl = "warn"; p_ev.append("近期监管函：" + hits["regulate"][0]["title"])
    if not p_ev:
        p_ev.append("非政策敏感行业")
    items["policy"] = {"level": p_lvl, "evidence": p_ev}

    # ③ 大股东减持 / 质押
    r_ev, r_lvl = [], "pass"
    if any(h["recent"] for h in hits["reduce"]):
        r_lvl = "reject"; r_ev.append("近期减持：" + hits["reduce"][0]["title"])
    elif any(h["recent"] for h in hits["pledge"]):
        r_lvl = "warn"; r_ev.append("近期股权质押：" + hits["pledge"][0]["title"])
    if not r_ev:
        r_ev.append("近半年无减持/质押公告")
    items["holder"] = {"level": r_lvl, "evidence": r_ev}

    # ④ 上下游议价拐点（毛利率）
    gm = m.get("gm_declining_quarters") or 0
    m_ev, m_lvl = [], "pass"
    if gm >= 2:
        m_lvl = "warn"; m_ev.append(f"毛利率连续{gm}期下滑（营收增长但毛利率降）")
    else:
        m_ev.append("毛利率稳定")
    items["margin"] = {"level": m_lvl, "evidence": m_ev}

    # ⑤ 海外汇率敞口
    o_ev, o_lvl = [], "pass"
    if any(ind in industry for ind in OVERSEAS_INDUSTRIES):
        o_lvl = "warn"; o_ev.append(f"出口导向行业（{industry}），需确认海外收入占比>30%")
    else:
        o_ev.append("非出口导向行业")
    items["overseas"] = {"level": o_lvl, "evidence": o_ev}

    levels = [v["level"] for v in items.values()]
    overall = "reject" if "reject" in levels else ("warn" if "warn" in levels else "pass")
    return {
        "code": code, "name": name, "industry": industry,
        "overall": overall, "items": items, "notice_count": len(notices),
    }


def review_all(candidates, notice_limit=NOTICE_LIMIT):
    out = {}
    for m in candidates:
        try:
            out[m.get("code")] = review_stock(m, notice_limit)
        except Exception as e:
            print(f"[five_review] {m.get('code')} 复核异常: {e}", file=sys.stderr)
            out[m.get("code")] = {"code": m.get("code"), "name": m.get("name"),
                                   "overall": "warn", "items": {}, "notice_count": 0,
                                   "error": str(e)}
    return out


if __name__ == "__main__":
    # 独立运行：从 _quant_summary.json 的 final_scores 取候选，跑复核，写 _five_review.json
    qpath = os.path.join(WORKSPACE, "_quant_summary.json")
    if not os.path.exists(qpath):
        print("缺少 _quant_summary.json，无法独立运行（请先跑 run_quant_model_mx.py）")
        sys.exit(1)
    q = json.load(open(qpath, encoding="utf-8"))
    cands = []
    for r in q.get("final_scores", []):
        cands.append({"code": r["code"], "name": r["name"],
                      "sw": r.get("industry", ""), "goodwill_ratio": None,
                      "gm_declining_quarters": None})
    fr = review_all(cands)
    out_path = os.path.join(WORKSPACE, "_five_review.json")
    json.dump({"generated_at": q.get("date"), "review": fr},
              open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n_pass = sum(1 for v in fr.values() if v["overall"] == "pass")
    n_warn = sum(1 for v in fr.values() if v["overall"] == "warn")
    n_rej = sum(1 for v in fr.values() if v["overall"] == "reject")
    print(f"[五项复核] 完成 {len(fr)} 只：通过{n_pass} / 警示{n_warn} / 否决{n_rej}")
    for v in fr.values():
        if v["overall"] != "pass":
            print(f"  {v['name']}({v['code']}) {v['overall']}: " +
                  "；".join(f"{k}={d['level']}" for k, d in v["items"].items()))
