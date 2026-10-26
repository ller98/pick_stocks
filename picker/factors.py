#!/usr/bin/env python3
"""factors.py - 量化选股模型的「可测核心」：评分 / 择时 / 动量 / 仓位数学。

本模块把 model.py 中依赖 I/O 与数据后端的纯计算逻辑抽取为独立、无副作用、可单测的函数，
与 I/O 编排层（model.py / coarse_filter.py）解耦。生产代码 model.py 与这里的实现保持同源逻辑。

所有函数均为纯函数（给定相同输入必有相同输出），便于 pytest 覆盖与回归。
"""
import json
import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


# --------------------------------------------------------------------- 基础工具
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_timing_config():
    return load_json(os.path.join(CONFIG_DIR, "timing_config.json"), {})


def load_factor_config():
    return load_json(os.path.join(CONFIG_DIR, "factor_config.json"), {})


def load_position_config():
    return load_json(os.path.join(CONFIG_DIR, "position_config.json"),
                     {"enabled": True, "total_planned": 200000, "per_stock_cap": 0.12,
                      "tier_weights": {"pass": 1.0, "warn": 0.5}, "warn_policy": "observe_only"})


# --------------------------------------------------------------------- 综合评分
def score_stock(r, fcfg=None):
    """对最终备选标的计算 0-100 综合评分（百分制），分行业差异化评估。

    维度与权重：估值吸引力(35) + 盈利质量(35) + 财务安全(30) + 价格成熟度惩罚(配置 points 分)。
    r 需含字段：is_cyclical, status, PB, peg, ROE_TTM, np_deduct_growth,
                warn_notes(list), maturity_score(0-100 或 None), mom_score(0-100 或 None)。
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

    fundamental = val + qual + safe
    _mom_cfg = (fcfg or {}).get("momentum", {}) or {}
    mom_method = _mom_cfg.get("method", "umd")
    mom_score = r.get("mom_score")
    points = _mom_cfg.get("points", 12)
    use_in_score = _mom_cfg.get("use_in_score", True)
    w_mom = max(0.0, min(0.5, points / 100.0)) if use_in_score else 0.0
    _mom_contrib = None
    if not use_in_score or w_mom == 0:
        total = round(fundamental)
    elif mom_method == "maturity_penalty":
        _mat = r.get("maturity_score")
        if _mat is None:
            total = round(fundamental)
        else:
            _mom_contrib = round(100 - _mat, 1)   # 翻转后实际贡献分（已兑现→低）
            total = round(fundamental * (1 - w_mom) + _mom_contrib * w_mom)
    elif mom_method == "maturity_bonus":
        _mat = r.get("maturity_score")
        if _mat is None:
            total = round(fundamental)
        else:
            _mom_contrib = round(_mat, 1)
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
        "mom_score": mom_score, "maturity_score": r.get("maturity_score"),
        "maturity_contrib": _mom_contrib, "roe_score": round(roe_score, 1),
        "grow_score": round(grow_score, 1), "grow_note": grow_note,
        "penalty": round(penalty, 1), "penalty_detail": penalty_detail,
        "is_cyclical": is_cycl,
    }


# --------------------------------------------------------------------- 动量因子
def momentum_score(mom, mcfg=None):
    """把 12-1 月动量收益率映射为 0-100 动量分（纯函数，无数据获取）。

    mom >= strong_up -> 100；mom <= strong_down -> 0；区间内线性；无足够历史(mom=None) -> None。
    """
    if mom is None:
        return None
    mcfg = mcfg or {}
    strong_up = float(mcfg.get("strong_up", 0.50))
    strong_down = float(mcfg.get("strong_down", -0.30))
    if mom >= strong_up:
        return 100.0
    if mom <= strong_down:
        return 0.0
    return round(_clamp((mom - strong_down) / (strong_up - strong_down), 0, 1) * 100, 1)


# --------------------------------------------------------------------- 择时（市场热度）
def _norm_dim_score(x, default=50.0):
    """把 market-overview 的维度得分归一化到 0-100。

    接口原始分为 1-5 分制（5=最热/最贵/最强），映射：1→0, 2→25, 3→50, 4→75, 5→100。
    若未来改为百分制(>5) 直接使用。
    """
    v = safe_float(x, -1)
    if v is None or v < 0:
        return default
    if v <= 5.0:
        return _clamp((v - 1.0) / 4.0 * 100.0, 0, 100)
    return _clamp(v, 0, 100)


def quantify_market(srow, tcfg=None):
    """择时去主观化：用硬指标数值分计算市场热度 heat(0-100)。

    srow 含 VALUATION_SCORE / SENTIMENT_SCORE / VOLUME_ENERGE_SCORE / STOCK_WIDTH_SCORE（1-5 分制）。
    heat = w_val*估值 + w_sent*情绪 + w_vol*量能；宽度低位反向倾斜降低 heat（反指）。
    heat 高→减仓，heat 低(恐慌)→加仓。返回全部数值分量，可回测可纠错。
    """
    if tcfg is None:
        tcfg = load_timing_config()
    w = tcfg.get("weights", {"valuation": 0.45, "sentiment": 0.30, "volume_energy": 0.25})
    bands = tcfg.get("heat_bands", {"overheat": 70, "neutral_high": 55, "neutral_low": 40, "panic": 30})
    decisions = tcfg.get("decisions", {})

    val = _norm_dim_score(safe_float(srow.get("VALUATION_SCORE"), None))
    sent = _norm_dim_score(safe_float(srow.get("SENTIMENT_SCORE"), None))
    vol = _norm_dim_score(safe_float(srow.get("VOLUME_ENERGE_SCORE"), None))
    width = _norm_dim_score(safe_float(srow.get("STOCK_WIDTH_SCORE"), None))

    heat = (w.get("valuation", 0.45) * val
            + w.get("sentiment", 0.30) * sent
            + w.get("volume_energy", 0.25) * vol)
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

    # heat 二次确认降级（恐慌区需估值+情绪同步低位才加仓，否则降级防御观望）
    rc = tcfg.get("reversal_confirm", {})
    if rc.get("enabled", False) and band == "panic":
        cv = safe_float(rc.get("confirm_valuation_max"), 35)
        cs = safe_float(rc.get("confirm_sentiment_max"), 30)
        if not (val <= cv and sent <= cs):
            down = rc.get("downgrade_band", "neutral_low")
            band = down
            dec = decisions.get(down, {"position_range": "30%-50%", "action": "观望为主",
                                       "style": "价值防御", "growth_limit": 6, "defense_boost": False})

    return {
        "heat": round(heat, 1), "valuation_score": round(val, 1), "sentiment_score": round(sent, 1),
        "volume_energy_score": round(vol, 1), "stock_width_score": round(width, 1),
        "band": band, "decision": {"position_range": dec.get("position_range"),
                                   "action": dec.get("action"), "style": dec.get("style"),
                                   "growth_limit": dec.get("growth_limit", 6),
                                   "defense_boost": dec.get("defense_boost", False)},
    }


# --------------------------------------------------------------------- 仓位计划
def parse_position_band(position_range):
    """把 '30%-50%' 解析为 (下限, 上限) 比例，如 (0.30, 0.50)；失败返回 (0.30, 1.0)。"""
    try:
        s = str(position_range).replace("%", "").strip()
        parts = [float(p) for p in s.split("-") if p.strip()]
        lo = max(0.0, min(1.0, parts[0] / 100.0))
        hi = max(0.0, min(1.0, (parts[-1] if len(parts) > 1 else parts[0]) / 100.0))
        return lo, hi
    except Exception:
        return 0.30, 1.0


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


def compute_position_plan(final_all, total_planned=200000, lower_pct=0.30, upper_pct=1.0,
                          per_stock_cap=0.12, existing_total=0.0,
                          tier_weights=None, warn_policy="observe_only"):
    """返回每只入选标的的建议持仓方案（纯函数，无文件 I/O）。

    严格服从择时总仓区间 [lower, upper]：
      - 本轮部署目标 = 区间中点 target_pct（留余地，不一次打满）
      - 本轮可新增现金 = total × target_pct − 已持仓
      - 预算池在入选标的间按「评分档权重 × 状态权重」分配，单票受 cap 与硬上限约束
    这样总仓永不突破 upper，且始终为「明天的好股票」预留额度。
    """
    tier_weights = tier_weights or {"pass": 1.0, "warn": 0.5}
    lower_pct = max(0.0, min(1.0, lower_pct))
    upper_pct = max(lower_pct, min(1.0, upper_pct))
    target_pct = (lower_pct + upper_pct) / 2.0
    target_total = total_planned * target_pct
    hard_cap_total = total_planned * upper_pct
    available_new = max(0.0, target_total - existing_total)
    over_cap = max(0.0, existing_total - hard_cap_total)
    over_target = max(0.0, existing_total - target_total)

    plan = []
    weight_sum = 0.0
    for s in final_all:
        score = s.get("score") or (s.get("score_detail", {}) or {}).get("score") or 0
        tier = tier_weights.get(s.get("status", "pass"), 1.0)
        if s.get("status") == "warn" and warn_policy == "observe_only":
            tier = 0.0
        w = _score_weight(score) * tier
        weight_sum += w
        plan.append({"code": s.get("code"), "name": s.get("name"),
                     "score": score, "grade": s.get("grade"), "_weight": w})

    cap_abs = total_planned * per_stock_cap
    for p in plan:
        if weight_sum > 0 and available_new > 0:
            budget = available_new * p["_weight"] / weight_sum
        else:
            budget = 0.0
        p["budget"] = round(min(budget, cap_abs), 2)
        p.pop("_weight", None)

    return {
        "total_planned": total_planned,
        "target_pct": round(target_pct, 3),
        "target_total": round(target_total, 2),
        "hard_cap_total": round(hard_cap_total, 2),
        "existing_total": round(existing_total, 2),
        "available_new": round(available_new, 2),
        "over_cap": round(over_cap, 2),
        "over_target": round(over_target, 2),
        "per_stock_cap": per_stock_cap,
        "plan": plan,
    }
