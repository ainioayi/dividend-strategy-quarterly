import pytest
from quarterly_strategy import screen_dynamic_pool


def test_screen_dynamic_pool_basic():
    """动态池筛选：连续8年分红入选，不足排除。"""
    divs = {
        "000001": [{"year": y, "dps": 0.3} for y in range(2010, 2025)],
        "000002": [{"year": y, "dps": 0.2} for y in range(2015, 2025)],
        "000003": [{"year": y, "dps": 0.1} for y in range(2020, 2025)],
    }
    pool = screen_dynamic_pool(divs, "2024-01-31", min_consecutive_years=8)
    assert "000001" in pool
    assert "000002" in pool
    assert "000003" not in pool


def test_screen_dynamic_pool_july_switch():
    """7月后使用year-1为最新确认年份，1-6月使用year-2。"""
    divs = {
        "000001": [{"year": y, "dps": 0.3} for y in range(2010, 2024)],
    }
    # 2024 年 6 月尚未到切换月：结束年度为 2022，起始年度为 2015。
    pool_jun = screen_dynamic_pool(divs, "2024-06-30", min_consecutive_years=8)
    assert "000001" in pool_jun  # 2015-2022 共 8 年。
    # 2024 年 7 月已经切换：结束年度为 2023，起始年度为 2016。
    pool_jul = screen_dynamic_pool(divs, "2024-07-31", min_consecutive_years=8)
    assert "000001" in pool_jul  # 2016-2023 共 8 年。


def test_screen_dynamic_pool_no_lookahead():
    """不应使用尚未确认的分红年份。"""
    divs = {
        "000001": [{"year": y, "dps": 0.3} for y in range(2015, 2023)],
    }
    # as_of=2024-01: end_year=2022, 只能用2022年及之前
    pool = screen_dynamic_pool(divs, "2024-01-31", min_consecutive_years=8)
    assert "000001" in pool  # 2015-2022 共 8 年。

    # 缺少2015年 -> 只有7年，不够
    divs2 = {"000001": [{"year": y, "dps": 0.3} for y in range(2016, 2023)]}
    pool2 = screen_dynamic_pool(divs2, "2024-01-31", min_consecutive_years=8)
    assert "000001" not in pool2  # 2016-2022 只有 7 年，不足 8 年。


def test_screen_dynamic_pool_zero_dps_excluded():
    """DPS为0的年份不计入连续年数。"""
    divs = {
        "000001": [{"year": 2015, "dps": 0.3}, {"year": 2016, "dps": 0.3},
                   {"year": 2017, "dps": 0}, {"year": 2018, "dps": 0.3}] +
                  [{"year": y, "dps": 0.3} for y in range(2019, 2025)],
    }
    # 2015-2022 中 2017 DPS=0 -> 只有7年有效 -> 不够8年
    pool = screen_dynamic_pool(divs, "2024-01-31", min_consecutive_years=8)
    assert "000001" not in pool


def test_screen_dynamic_pool_uses_known_ex_dates_when_details_are_available():
    divs = {"000001": [{"year": y, "dps": 0.3} for y in range(2016, 2024)]}
    details = {"000001": [
        {"year": y, "ex_date": f"{y + 1}-06-30", "dps": 0.3}
        for y in range(2016, 2024)
    ]}
    # 2024 年 1 月时 2022 年分红尚未除权，不能仅凭年度汇总入池。
    assert screen_dynamic_pool(
        divs, "2024-01-31", min_consecutive_years=8,
        dividend_details_by_code=details,
    ) == []


def test_screen_dynamic_pool_requires_contiguous_years():
    divs = {"000001": [
        {"year": 2016, "dps": 0.3}, {"year": 2017, "dps": 0.3},
        {"year": 2019, "dps": 0.3}, {"year": 2020, "dps": 0.3},
        {"year": 2021, "dps": 0.3}, {"year": 2022, "dps": 0.3},
    ]}
    assert screen_dynamic_pool(divs, "2023-01-31", min_consecutive_years=6) == []


def test_screen_dynamic_pool_normalizes_string_years():
    """缓存年度为字符串时，必须与整数年度得到相同候选池。"""
    divs = {
        "000001": [{"year": str(y), "dps": "0.3"} for y in range(2015, 2023)],
    }
    assert screen_dynamic_pool(divs, "2024-01-31", min_consecutive_years=8) == ["000001"]

    details = {
        "000001": [
            {"year": str(y), "ex_date": f"{y + 1}-01-15", "dps": "0.3"}
            for y in range(2015, 2023)
        ],
    }
    assert screen_dynamic_pool(
        divs, "2024-01-31", min_consecutive_years=8,
        dividend_details_by_code=details,
    ) == ["000001"]


def test_screen_dynamic_pool_custom_switch_month():
    """切换月份可显式改变确认边界，且仍只使用已知年度。"""
    divs = {"000001": [{"year": y, "dps": 0.3} for y in range(2015, 2024)]}
    assert "000001" not in screen_dynamic_pool(
        divs, "2024-04-30", min_consecutive_years=9, pool_switch_month=5,
    )
    assert "000001" in screen_dynamic_pool(
        divs, "2024-05-31", min_consecutive_years=9, pool_switch_month=5,
    )


def test_screen_dynamic_pool_rejects_invalid_switch_month():
    divs = {"000001": [{"year": y, "dps": 0.3} for y in range(2015, 2024)]}
    for value in (0, 13, "bad"):
        try:
            screen_dynamic_pool(divs, "2024-07-31", min_consecutive_years=8,
                                pool_switch_month=value)
        except ValueError as exc:
            assert "pool_switch_month" in str(exc)
        else:
            raise AssertionError("非法切换月份必须被拒绝")
