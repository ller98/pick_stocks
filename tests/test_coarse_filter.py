"""粗筛四道闸单元测试。"""
from picker import coarse_filter as cf

EXCLUDED = {"chinext", "star", "bse"}


def _g(code, pe, pb, growth, sw="化工"):
    return cf.mk({"code": code, "name": code, "pe": pe, "pb": pb, "growth": growth},
                 "growth", sw_map={code: sw})


def _c(code, pe, pb, sw="银行"):
    return cf.mk({"code": code, "name": code, "pe": pe, "pb": pb},
                 "cyclical", sw_map={code: sw})


def test_mk_computes_peg():
    r = _g("000415", 5.87, 0.9, 258.73)
    assert r["peg"] == round(5.87 / 258.73, 4)


def test_mk_no_peg_for_cyclical():
    r = _c("600016", 5.5, 0.27)
    assert r["peg"] is None


def test_sector_key_granularity():
    assert cf.sector_key("银行") == "银行"
    assert cf.sector_key("有色金属-工业金属-铝") == "铝"


def test_dedup_by_sector_keeps_max_per():
    stocks = [{"code": str(i), "sw": "银行", "peg": i} for i in range(10)]
    kept, dropped = cf.dedup_by_sector(stocks, 3, lambda s: s["peg"])
    assert len(kept) == 3
    assert len(dropped) == 7


def test_apply_coarse_filters_all_gates():
    growth = [
        _g("300750", 35, 3.5, 50, "电力设备"),  # 创业板 -> 第0道剔除
        _g("600016", 5.5, 0.27, 5),              # 增速<20 -> 第3道剔除
        _g("600031", 40, 2.0, 25),               # PEG=1.6>1 -> 第2道剔除
        _g("000858", 20, 3.8, 20, "食品饮料"),   # 通过
    ]
    cyclical = [_c("600000", 5.3, 0.55)]
    pool = cf.apply_coarse_filters(growth, cyclical, max_per=8, excluded=EXCLUDED)

    codes = [s["code"] for s in pool["coarse_filtered"]]
    assert "300750" not in codes          # 板块权限
    assert "600016" not in codes          # 增速质量
    assert "600031" not in codes          # PEG 合理性
    assert "000858" in codes              # 保留
    assert "600000" in codes             # 周期线保留
    assert len(pool["board_filtered_out"]) == 1
    assert pool["coarse_max_per_sector"] == 8


def test_apply_coarse_filters_sector_dedup():
    # 9 只同行业(银行)成长线，max_per=8 -> 应剔除 1 只
    growth = [_g(f"60000{i}", 6.0, 0.6, 25, "银行") for i in range(1, 10)]
    pool = cf.apply_coarse_filters(growth, [], max_per=8, excluded=EXCLUDED)
    kept_banks = [s for s in pool["coarse_filtered"] if s["sw"] == "银行"]
    assert len(kept_banks) == 8
    assert len(pool["sector_over_quota_dropped"]) == 1
