"""板块权限过滤单元测试（测试工程师最该秀的：纯函数全覆盖）。"""
from picker import board_filter as bf


def test_normalize_code_variants():
    assert bf.normalize_code("002440") == "002440"
    assert bf.normalize_code("sz002440") == "002440"
    assert bf.normalize_code("002440.SZ") == "002440"
    assert bf.normalize_code("sh.600016") == "600016"
    assert bf.normalize_code("SZ301219") == "301219"


def test_classify_board():
    assert bf.classify_board("300750") == "chinext"
    assert bf.classify_board("301219") == "chinext"
    assert bf.classify_board("688111") == "star"
    assert bf.classify_board("920599") == "bse"
    assert bf.classify_board("600016") == "main"
    assert bf.classify_board("002440") == "main"
    assert bf.classify_board("000001") == "main"


def test_is_blocked_with_excluded():
    excluded = {"chinext", "star", "bse"}
    assert bf.is_blocked("300750", excluded) is True
    assert bf.is_blocked("688111", excluded) is True
    assert bf.is_blocked("600016", excluded) is False


def test_filter_stocks_removes_blocked_only():
    excluded = {"chinext"}
    stocks = [{"code": "300750", "name": "宁德"}, {"code": "600016", "name": "民生"}]
    kept, removed = bf.filter_stocks(stocks, excluded=excluded)
    assert [s["code"] for s in kept] == ["600016"]
    assert [s["code"] for s in removed] == ["300750"]
    assert removed[0].get("_blocked_board") == "chinext"


def test_filter_stocks_no_excluded_is_noop():
    stocks = [{"code": "300750"}, {"code": "600016"}]
    kept, removed = bf.filter_stocks(stocks, excluded=set())
    assert len(kept) == 2 and len(removed) == 0
