"""核心评分 / 择时 / 动量 / 仓位数学单元测试。"""
from picker import factors as F

BASE = {
    "is_cyclical": False, "status": "pass", "PB": 1.2, "peg": 0.5,
    "ROE_TTM": 15.0, "np_deduct_growth": 1.3, "warn_notes": [],
    "maturity_score": None, "mom_score": None,
}


def test_score_stock_range_and_grade():
    s = F.score_stock(BASE)
    assert 0 <= s["score"] <= 100
    assert s["grade"] in ("优秀", "良好", "中性", "偏弱")


def test_score_stock_maturity_penalty_lowers_score():
    fcfg = {"momentum": {"method": "maturity_penalty", "use_in_score": True, "points": 8}}
    unexpired = {"maturity_score": 10, "mom_score": None}   # 未兑现(涨得少) -> 不被降权，得分高
    expired = {"maturity_score": 90, "mom_score": None}    # 已兑现(涨得多) -> 价格成熟度惩罚，得分低
    s_unexpired = F.score_stock({**BASE, **unexpired}, fcfg)
    s_expired = F.score_stock({**BASE, **expired}, fcfg)
    assert s_expired["score"] < s_unexpired["score"]


def test_score_stock_cyclical_uses_pb():
    cycl = {**BASE, "is_cyclical": True, "peg": None, "PB": 0.3}
    s = F.score_stock(cycl)
    assert s["val_score"] > 0


def test_momentum_score_mapping():
    assert F.momentum_score(0.6) == 100.0
    assert F.momentum_score(-0.3) == 0.0
    assert F.momentum_score(None) is None
    assert 0 < F.momentum_score(0.1) < 100


def test_quantify_market_bands():
    neutral = {"VALUATION_SCORE": 3, "SENTIMENT_SCORE": 3,
               "VOLUME_ENERGE_SCORE": 3, "STOCK_WIDTH_SCORE": 55}
    over = {"VALUATION_SCORE": 5, "SENTIMENT_SCORE": 5,
            "VOLUME_ENERGE_SCORE": 4, "STOCK_WIDTH_SCORE": 70}
    panic_highval = {"VALUATION_SCORE": 4, "SENTIMENT_SCORE": 2,
                     "VOLUME_ENERGE_SCORE": 2, "STOCK_WIDTH_SCORE": 10}
    assert F.quantify_market(neutral)["band"] in ("neutral_high", "neutral_low")
    assert F.quantify_market(over)["band"] == "overheat"
    # 恐慌区但估值仍高 -> 二次确认降级为防御观望（不抄底）
    assert F.quantify_market(panic_highval)["band"] == "neutral_low"


def test_quantify_market_heat_monotonic():
    lo = F.quantify_market({"VALUATION_SCORE": 1, "SENTIMENT_SCORE": 1,
                            "VOLUME_ENERGE_SCORE": 1, "STOCK_WIDTH_SCORE": 90})
    hi = F.quantify_market({"VALUATION_SCORE": 5, "SENTIMENT_SCORE": 5,
                            "VOLUME_ENERGE_SCORE": 5, "STOCK_WIDTH_SCORE": 90})
    assert hi["heat"] > lo["heat"]


def test_parse_position_band():
    assert F.parse_position_band("30%-50%") == (0.30, 0.50)
    assert F.parse_position_band("15%-30%") == (0.15, 0.30)
    assert F.parse_position_band("garbage") == (0.30, 1.0)


def test_compute_position_plan_budget_and_cap():
    final = [
        {"code": "A", "score": 90, "grade": "优秀", "status": "pass"},
        {"code": "B", "score": 60, "grade": "中性", "status": "pass"},
    ]
    plan = F.compute_position_plan(final, total_planned=200000, lower_pct=0.30,
                                   upper_pct=1.0, per_stock_cap=0.5, existing_total=0.0)
    cap = 200000 * 0.5
    assert all(p["budget"] <= cap + 1e-6 for p in plan["plan"])
    # 高分票拿到更多预算
    d = {p["code"]: p["budget"] for p in plan["plan"]}
    assert d["A"] > d["B"]
    # 已有持仓超硬上限 -> 提示减仓
    over = F.compute_position_plan(final, total_planned=200000, lower_pct=0.30,
                                    upper_pct=0.50, per_stock_cap=0.12, existing_total=150000)
    assert over["over_cap"] > 0
