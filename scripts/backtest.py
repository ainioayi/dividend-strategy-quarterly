"""独立季度回测引擎（纯股息率信号版）。

数据源：东财历史K线 + 东财分红历史。
信号：真实股息率（DPS/price）入场/持有/退出。
所有历史数据当次拉取并落盘缓存，保证可重现。

用法：
    python scripts/backtest.py
    python scripts/backtest.py --json result.json
    python scripts/backtest.py --param entry_yield 4.5 hold_yield 4.0
    python scripts/backtest.py --reinvest
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quarterly_strategy import (
    DEFAULT_QUARTERLY_RULES,
    build_initial_ledger,
    rebalance_quarter,
    rebalance_equally,
    transaction_fees,
    _market_value,
    screen_dynamic_pool,
    momentum_filter,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "backtest_cache"
DEFAULT_MANIFEST = ROOT / "data" / "universe_manifest.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

CANDIDATE_CODES = [
    "000333", "000651", "000719",
    "002318", "002555", "002807", "002839", "002884",
    "600015", "600036", "600039", "600461",
    "600741", "600803", "600873", "600887", "600987",
    "601088", "601166", "601169", "601229", "601811",
    "601818", "601919", "601997", "603365", "603444", "603816",
    "000895", "600066", "600403",
    "601009", "000429",
    "000550",
    "600188", "600104",
    "600548",
]

REBALANCE_DATES_QUARTERLY = [
    "2016-03-31", "2016-06-30", "2016-09-30", "2016-12-30",
    "2017-03-31", "2017-06-30", "2017-09-29", "2017-12-29",
    "2018-03-30", "2018-06-29", "2018-09-28", "2018-12-28",
    "2019-03-29", "2019-06-28", "2019-09-30", "2019-12-31",
    "2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31",
    "2021-03-31", "2021-06-30", "2021-09-30", "2021-12-31",
    "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-30",
    "2023-03-31", "2023-06-30", "2023-09-28", "2023-12-29",
    "2024-03-29", "2024-06-28", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
]


def _generate_monthly_dates(klines_sample, start_year=2016, end_year=2026):
    """按全部缓存 K 线交易日并集生成确定性月末日期。"""
    from datetime import datetime, timedelta
    all_dates = {
        str(day)[:10]
        for prices in klines_sample.values()
        for day in prices
        if len(str(day)) >= 10
    }
    dates = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if m == 12:
                target = datetime(y + 1, 1, 1) - timedelta(days=1)
            else:
                target = datetime(y, m + 1, 1) - timedelta(days=1)
            month_end = target.strftime("%Y-%m-%d")
            month_key = "%04d-%02d" % (y, m)
            candidates = [day for day in all_dates if day <= month_end and day[:7] == month_key]
            if candidates:
                dates.append(max(candidates))
    return dates


def _get_monthly_dates(codes=None):
    """读取版本化月末日期缓存；永不复用旧 monthly_dates.json。"""
    cached_dates = _load_cache("monthly_dates_v3")
    if cached_dates is not None:
        return cached_dates
    # 汇总全部候选股票的缓存交易日，避免样本股缺失造成日期偏差。
    if codes is None:
        try:
            from universe_manifest import load_manifest
            codes = load_manifest(DEFAULT_MANIFEST)["codes"]
        except Exception:
            codes = CANDIDATE_CODES
    sample = {}
    for code in codes:
        kl = _load_cache("kl_" + code)
        if kl:
            sample[code] = kl
    if not sample:
        return list(REBALANCE_DATES_QUARTERLY)
    dates = _generate_monthly_dates(sample)
    _save_cache("monthly_dates_v3", dates)
    return dates


REBALANCE_DATES = REBALANCE_DATES_QUARTERLY  # fallback; updated at runtime


# 回测专用默认参数：PR 不限制（纯股息率策略：
# 回测优化参数（动态池+动量策略，网格搜索验证 2026-08-26）
BACKTEST_RULES = dict(DEFAULT_QUARTERLY_RULES)
BACKTEST_RULES["entry_pr"] = 999.0
BACKTEST_RULES["hold_pr"] = 999.0
BACKTEST_RULES["exit_pr"] = 999.0
# 止损和非对称退出经验证无效果，设为简化值
BACKTEST_RULES["stop_loss_pct"] = 0.0
BACKTEST_RULES["entry_yield"] = 7.5
BACKTEST_RULES["max_yield"] = 999.0
BACKTEST_RULES["rank_by"] = "yield"
BACKTEST_RULES["hold_yield"] = 5.5
BACKTEST_RULES["loss_hold_yield"] = 5.5
BACKTEST_RULES["momentum_months"] = 4
BACKTEST_RULES["momentum_threshold"] = 0.85
BACKTEST_RULES["pool_mode"] = "dynamic"
BACKTEST_RULES["pool_min_consecutive_years"] = 3
BACKTEST_RULES["pool_switch_month"] = 7
# 回测使用固定的月末信号序列；季度模型账本另行按快照更新。
BACKTEST_RULES["frequency"] = "monthly"
# 信号在月末收盘形成，默认下一交易日收盘执行；0 可用于与旧口径对照。
BACKTEST_RULES["execution_lag_days"] = 1
# 分红信息可得性延迟：0 表示使用截至除权日已知的明细；压力测试在第17轮覆盖。
BACKTEST_RULES["dividend_information_lag_days"] = 0


def _load_cache(name):
    p = CACHE_DIR / (name + ".json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save_cache(name, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _trading_calendar(klines: dict[str, dict[str, float]]) -> list[str]:
    """从缓存中生成确定性的全市场交易日并集。"""
    dates = {
        str(day)[:10]
        for series in klines.values()
        for day, price in (series or {}).items()
        if price and len(str(day)) >= 10
    }
    return sorted(dates)


def _next_trading_date(calendar: list[str], target: str, lag: int = 1) -> str | None:
    """返回 target 之后第 lag 个已知交易日。lag=0 返回 target（若存在）。"""
    if lag < 0:
        raise ValueError("execution_lag_days 不能为负数")
    if lag == 0:
        index = bisect_right(calendar, target) - 1
        return calendar[index] if index >= 0 and calendar[index] == target else None
    index = bisect_right(calendar, target) + lag - 1
    return calendar[index] if index < len(calendar) else None


def _exact_price(series: dict[str, float] | None, date: str) -> float | None:
    """只取指定交易日价格，不用停牌期间的陈旧回填值执行交易。"""
    try:
        value = float((series or {}).get(date))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_universe_codes(codes, dynamic_pool: bool, manifest_path=None):
    """解析回测宇宙，并返回代码与可审计来源。"""
    if codes is not None:
        resolved = sorted({str(code).zfill(6) for code in codes})
        return resolved, {"kind": "explicit", "count": len(resolved)}
    if not dynamic_pool:
        resolved = sorted(set(CANDIDATE_CODES))
        return resolved, {"kind": "curated_constant", "count": len(resolved)}
    path = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST
    if not path.is_absolute() and not path.exists():
        path = ROOT / path
    if path.exists():
        try:
            from universe_manifest import load_manifest
            manifest = load_manifest(path)
            resolved = [str(code) for code in manifest["codes"]]
            return resolved, {
                "kind": "manifest", "path": str(path),
                "as_of": manifest.get("as_of"),
                "records_sha256": manifest.get("records_sha256"),
                "price_format": (manifest.get("source") or {}).get("price_format"),
                "price_source": (manifest.get("source") or {}).get("price_source"),
                "count": len(resolved),
            }
        except Exception as exc:
            raise ValueError(f"候选池 manifest 无法加载: {path}: {exc}") from exc
    # 兼容尚未生成 manifest 的旧工作区，但把不可复现边界显式写入结果。
    resolved = sorted(f.stem[3:] for f in CACHE_DIR.glob("kl_*.json"))
    if not resolved:
        resolved = sorted(set(CANDIDATE_CODES))
    return resolved, {
        "kind": "cache_glob_fallback", "path": str(path),
        "count": len(resolved), "warning": "未找到 universe_manifest.json",
    }


def _fetch_kline_eastmoney(code, cutoff):
    """东财历史日线不复权接口，返回 {日期: 收盘价}。"""
    market = "1" if str(code).startswith("6") else "0"
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"{market}.{code}",
        "fields1": "f1",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "klt": "101",
        "fqt": "0",
        "beg": "20150101",
        "end": cutoff.replace("-", ""),
        "lmt": "10000",
    }
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
        timeout=20,
    )
    response.raise_for_status()
    rows = ((response.json().get("data") or {}).get("klines") or [])
    result = {}
    for raw in rows:
        fields = str(raw).split(",")
        if len(fields) < 3:
            continue
        day = fields[0][:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day > cutoff:
            continue
        try:
            close = float(fields[2])
        except (TypeError, ValueError):
            continue
        if close > 0:
            result[day] = close
    return result


def fetch_kline(code, as_of=None, refresh=False):
    """获取历史日 K 线（不复权），按明确截止日更新缓存。"""
    cutoff = str(as_of or datetime.now().strftime("%Y-%m-%d"))[:10]
    cached = _load_cache("kl_" + code)
    cached = dict(cached) if isinstance(cached, dict) else {}
    if cached and not refresh:
        return {day: price for day, price in cached.items() if str(day)[:10] <= cutoff}
    try:
        # refresh 明确表示重建，避免把旧的前复权价格与不复权价格混合。
        fresh = _fetch_kline_eastmoney(str(code).zfill(6), cutoff)
    except Exception:
        fresh = {}
    if fresh:
        _save_cache("kl_" + code, fresh)
        return fresh
    # 重建失败时不能把旧的前复权缓存当成不复权数据继续使用。
    if refresh:
        return {}
    return {day: price for day, price in cached.items() if str(day)[:10] <= cutoff}


def fetch_dividends(code):
    """东财分红送转历史，返回 [{year, dps, bonus_ratio, transfer_ratio}]。"""
    cached = _load_cache("dv_" + code)
    if cached is not None:
        return cached
    detail = _fetch_dividends_detail(code)
    if detail:
        # 将逐笔分红记录汇总为年度口径。
        by_year = {}
        for p in detail:
            y = p["year"]
            if y not in by_year:
                by_year[y] = {"dps": 0.0, "bonus_ratio": 0.0, "transfer_ratio": 0.0}
            by_year[y]["dps"] += p["dps"]
            by_year[y]["bonus_ratio"] = max(by_year[y]["bonus_ratio"], p.get("bonus_ratio", 0))
            by_year[y]["transfer_ratio"] = max(by_year[y]["transfer_ratio"], p.get("transfer_ratio", 0))
        result = [{"year": y, "dps": round(v["dps"], 4),
                   "bonus_ratio": v["bonus_ratio"], "transfer_ratio": v["transfer_ratio"]}
                  for y, v in sorted(by_year.items())]
        _save_cache("dv_" + code, result)
        return result
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_SHAREBONUS_DET"
        "&columns=ALL&filter=(SECURITY_CODE=%22" + code + "%22)"
        "&pageNumber=1&pageSize=50&sortColumns=REPORT_DATE&sortTypes=-1"
    )
    try:
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    rows = (data.get("result") or {}).get("data") or []
    by_year = {}
    for row in rows:
        progress = str(row.get("ASSIGNMENT_PROGRESS") or row.get("ASSIGN_PROGRESS") or "")
        if "实施" not in progress and "完成" not in progress:
            continue
        rd = str(row.get("REPORT_DATE") or "")[:10]
        if len(rd) < 4:
            continue
        year = int(rd[:4])
        pretax = row.get("PRETAX_BONUS_RMB")
        bonus = float(row.get("BONUS_RATIO") or 0)
        transfer = float(row.get("IT_RATIO") or row.get("TRANSFER_RATIO") or 0)
        dps = float(pretax) / 10.0 if pretax and float(pretax) > 0 else 0.0
        if year not in by_year:
            by_year[year] = {"dps": 0.0, "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        by_year[year]["dps"] += dps
        by_year[year]["bonus_ratio"] = max(by_year[year]["bonus_ratio"], bonus)
        by_year[year]["transfer_ratio"] = max(by_year[year]["transfer_ratio"], transfer)
    result = [{"year": y, "dps": round(v["dps"], 4),
               "bonus_ratio": v["bonus_ratio"], "transfer_ratio": v["transfer_ratio"]}
              for y, v in sorted(by_year.items())]
    _save_cache("dv_" + code, result)
    return result


def _fetch_dividends_detail(code):
    """东财分红送转历史（逐笔），返回 [{year, ex_date, dps, bonus_ratio, transfer_ratio}]。

    与 fetch_dividends 不同，保留每次分红的实际除权除息日，用于精确入账。
    """
    cached = _load_cache("dvd_" + code)
    if cached is not None:
        return cached
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_SHAREBONUS_DET"
        "&columns=ALL&filter=(SECURITY_CODE=%22" + code + "%22)"
        "&pageNumber=1&pageSize=50&sortColumns=REPORT_DATE&sortTypes=-1"
    )
    try:
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    rows = (data.get("result") or {}).get("data") or []
    payments = []
    for row in rows:
        progress = str(row.get("ASSIGNMENT_PROGRESS") or row.get("ASSIGN_PROGRESS") or "")
        if "\u5b9e\u65bd" not in progress and "\u5b8c\u6210" not in progress:
            continue
        rd = str(row.get("REPORT_DATE") or "")[:10]
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if len(rd) < 4:
            continue
        year = int(rd[:4])
        pretax = row.get("PRETAX_BONUS_RMB")
        bonus = float(row.get("BONUS_RATIO") or 0)
        transfer = float(row.get("IT_RATIO") or row.get("TRANSFER_RATIO") or 0)
        dps = float(pretax) / 10.0 if pretax and float(pretax) > 0 else 0.0
        payments.append({"year": year, "ex_date": ex_date, "dps": round(dps, 4),
                         "bonus_ratio": bonus, "transfer_ratio": transfer})
    _save_cache("dvd_" + code, payments)
    return payments


def _trailing_dps(div_hist, as_of):
    """计算不使用未来数据的修正后置每股分红。"""
    year = int(as_of[:4])
    month = int(as_of[5:7]) if len(as_of) >= 7 else 1
    # 7月开始使用上一年DPS：A股年报分红通常在6-8月除权除息，
    # 7月时大部分分红已实施确认，避免使用尚未派付的分红数据。
    if month >= 7:
        offsets = [1, 2, 3]
    else:
        offsets = [2, 3, 4]
    for offset in offsets:
        target = year - offset
        for item in div_hist:
            if item["year"] == target:
                return item["dps"]
    return None

def _cumulative_split_factor(dvd_detail_list, from_ex_date, to_date):
    """\u8ba1\u7b97 from_ex_date \u4e4b\u540e\u3001to_date \u4e4b\u524d\uff08\u542b\uff09\u7684\u7d2f\u8ba1\u9001\u8f6c\u80a1\u6bd4\u4f8b\u3002

    \u7528\u4e8e\u8c03\u6574\u5386\u53f2 DPS\uff1a\u5f53\u4f7f\u7528\u9001\u8f6c\u80a1\u4e4b\u524d\u7684 DPS \u914d\u5408\u9001\u8f6c\u80a1\u4e4b\u540e\u7684\u4ef7\u683c\u8ba1\u7b97\u6536\u76ca\u7387\u65f6\uff0c
    \u9700\u8981\u5c06 DPS \u9664\u4ee5\u7d2f\u8ba1\u9001\u8f6c\u80a1\u6bd4\u4f8b\uff0c\u5426\u5219\u4f1a\u865a\u589e\u6536\u76ca\u7387\u3002
    """
    factor = 1.0
    for p in dvd_detail_list:
        b = p.get("bonus_ratio", 0) or 0
        t = p.get("transfer_ratio", 0) or 0
        ex = p.get("ex_date", "")
        if (b > 0 or t > 0) and ex and from_ex_date <= ex <= to_date:
            factor *= 1.0 + (b + t) / 10.0
    return factor

def _split_adjusted_trailing_dps(div_hist, dvd_detail_list, as_of):
    """\u9001\u8f6c\u80a1\u8c03\u6574\u540e\u7684\u540e\u7f6e DPS\uff08\u65e0\u672a\u6765\u51fd\u6570\uff09\u3002

    \u4e0e _trailing_dps \u4e0d\u540c\uff0c\u6b64\u51fd\u6570\u6839\u636e DPS \u652f\u4ed8\u540e\u7684\u9001\u8f6c\u80a1\u8c03\u6574 DPS\uff0c
    \u907f\u514d\u6df7\u5408\u524d\u540e\u4e0d\u540c\u80a1\u672c\u57fa\u6570\u7684\u6536\u76ca\u7387\u8ba1\u7b97\u504f\u5dee\u3002
    """
    year = int(as_of[:4])
    month = int(as_of[5:7]) if len(as_of) >= 7 else 1
    if month >= 7:
        offsets = [1, 2, 3]
    else:
        offsets = [2, 3, 4]
    # 有逐笔明细时，实际除权日才是有效信息边界；不能回退到可能包含
    # 截止日后分红的年度汇总。
    known = [
        p for p in (dvd_detail_list or [])
        if len(str(p.get("ex_date") or "")[:10]) == 10
        and str(p.get("ex_date"))[:10] <= as_of
    ]
    for offset in offsets:
        target = year - offset
        payments = [
            p for p in known
            if _as_int(p.get("year")) == target and _as_float(p.get("dps")) > 0
        ]
        if not payments:
            continue
        adjusted = 0.0
        for payment in payments:
            ex_date = str(payment["ex_date"])[:10]
            # 每笔分红单独调整；两次分红之间若发生送转，两笔 DPS 对应的
            # 当前股本折算比例不同。
            factor = _cumulative_split_factor(dvd_detail_list, ex_date, as_of)
            adjusted += _as_float(payment.get("dps")) / factor
        return round(adjusted, 6)
    return None


def _point_in_time_trailing_dps(div_hist, dvd_detail_list, as_of):
    """返回 ``as_of`` 当天真正已经除权的后置 DPS。

    年度汇总会包含同一报告年度内尚未除权的后续分配，直接使用会产生
    前视偏差。回测优先按逐笔 ``ex_date`` 截断并汇总；没有逐笔数据时由
    调用方回退到旧的年度近似逻辑。
    """
    if not dvd_detail_list:
        return None
    known_by_year = {}
    for item in dvd_detail_list:
        ex_date = str(item.get("ex_date") or "")[:10]
        if len(ex_date) != 10 or ex_date > as_of:
            continue
        year = _as_int(item.get("year"))
        dps = _as_float(item.get("dps"))
        if year is None:
            continue
        if dps <= 0:
            continue
        known_by_year.setdefault(year, []).append(item)
    if not known_by_year:
        return None
    current_year = int(as_of[:4])
    # 只使用上一自然年及更早的报告年度，避免把当年中期分配当成年报 DPS。
    candidates = sorted((y for y in known_by_year if y < current_year), reverse=True)
    if not candidates:
        return None
    target = candidates[0]
    adjusted = 0.0
    for payment in known_by_year[target]:
        ex_date = str(payment.get("ex_date") or "")[:10]
        factor = _cumulative_split_factor(dvd_detail_list, ex_date, as_of)
        adjusted += _as_float(payment.get("dps")) / factor
    return round(adjusted, 6) if adjusted > 0 else None


def _apply_precise_dividends(state, divs_detail, curr_date, rules, credited):
    """\u6309\u5b9e\u9645\u9664\u6743\u9664\u606f\u65e5\u5165\u8d26\u5206\u7ea2\u548c\u9001\u8f6c\u80a1\u3002

    \u4e0e _apply_quarterly_dividends \u4e0d\u540c\uff0c\u6b64\u51fd\u6570\u4f7f\u7528\u6bcf\u7b14\u5206\u7ea2\u7684\u771f\u5b9e\u9664\u6743\u9664\u606f\u65e5\uff0c
    \u907f\u514d\u63d0\u524d\u5165\u8d26\u9020\u6210\u7684\u9690\u6027\u672a\u6765\u51fd\u6570\u504f\u5dee\u3002
    """
    cash = float(state.get("cash", 0))
    holdings = state.get("holdings", {})
    if not holdings:
        return state
    events = list(state.get("events") or [])

    for code, h in holdings.items():
        shares = int(h.get("shares", 0))
        if shares <= 0:
            continue
        # 接口通常按最新日期倒序返回；按除权日正序处理，才能让同一
        # 次检查中的送转股先后顺序与真实持仓股数一致。
        # 先去掉接口可能返回的完全重复记录，但保留同一除权日不同报告年度
        # 或不同金额的合法分配。后者必须逐笔处理，不能只用 code+日期去重。
        payments = []
        seen_signatures = set()
        for item in divs_detail.get(code, []) or []:
            ex = str(item.get("ex_date") or "")[:10]
            if len(ex) != 10:
                continue
            signature = json.dumps({
                "year": item.get("year"),
                "report_date": item.get("report_date"),
                "ex_date": ex,
                "dps": item.get("dps"),
                "bonus_ratio": item.get("bonus_ratio", 0),
                "transfer_ratio": item.get("transfer_ratio", 0),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            normalized = dict(item)
            normalized["ex_date"] = ex
            payments.append(normalized)
        payments.sort(key=lambda item: str(item.get("ex_date") or ""))
        date_counts = {}
        for item in payments:
            ex = str(item.get("ex_date") or "")[:10]
            date_counts[ex] = date_counts.get(ex, 0) + 1
        for p in payments:
            ex_date = str(p.get("ex_date", ""))[:10]
            if not ex_date or len(ex_date) < 10:
                continue
            base_key = "%s_%s" % (code, ex_date)
            # 同一除权日可能同时实施不同报告年度的分配；仅用日期做键会
            # 漏记现金。唯一日期保留短键以兼容旧账本，同日多笔按语义哈希
            # 区分，完全重复的接口记录则自然只记一次。
            credit_key = base_key
            if date_counts.get(str(ex_date)[:10], 0) > 1:
                signature = json.dumps({
                    "year": p.get("year"),
                    "report_date": p.get("report_date"),
                    "dps": p.get("dps"),
                    "bonus_ratio": p.get("bonus_ratio", 0),
                    "transfer_ratio": p.get("transfer_ratio", 0),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                suffix = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
                credit_key = base_key + "_" + suffix
            if credit_key in credited:
                continue
            if ex_date > curr_date or ex_date < "2015-01-01":
                continue
            # 修复前视偏差：只在持仓入场日之后（含）的除权日入账分红
            entry_date = str(h.get("entry_date", ""))[:10]
            # 除权日收盘买入也没有资格领取该次分红，因此必须严格晚于
            # 入场日；等于入场日的事件同样不能入账。
            if entry_date and ex_date <= entry_date:
                continue

            dps = p.get("dps", 0)
            if dps > 0:
                gross = dps * shares
                holding_days = 400
                if len(entry_date) >= 10:
                    try:
                        from datetime import date as dt_date
                        ed = dt_date.fromisoformat(entry_date[:10])
                        # 税务持有期应截至实际除权日，而不是回测检查日；
                        # 后者会把短持仓错误地算成长期持仓并低估税费。
                        tax_date = ex_date[:10]
                        xd = dt_date.fromisoformat(tax_date)
                        holding_days = max((xd - ed).days, 0)
                    except Exception:
                        pass
                tax_rate = 0.0 if holding_days > 365 else (0.10 if holding_days > 30 else 0.20)
                tax = gross * tax_rate
                net = gross - tax
                cash += net
                events.append({
                    "date": curr_date, "side": "dividend", "code": code,
                    "shares": shares, "gross": round(gross, 2),
                    "tax": round(tax, 2), "net_cash": round(net, 2),
                    "ex_date": ex_date,
                    "reason": "DPS %.4f x %d shares (ex=%s)" % (dps, shares, ex_date),
                })

            bonus = p.get("bonus_ratio", 0)
            transfer = p.get("transfer_ratio", 0)
            if bonus > 0 or transfer > 0:
                split_factor = 1.0 + (bonus + transfer) / 10.0
                new_shares = int(round(shares * split_factor))
                delta = new_shares - shares
                if delta > 0:
                    events.append({
                        "date": curr_date, "side": "split", "code": code,
                        "shares": new_shares, "shares_before": shares,
                        "split_factor": round(split_factor, 4),
                        "reason": "10\u9001%s\u8f6c%s -> +%d shares" % (bonus, transfer, delta),
                    })
                    h["shares"] = new_shares
                    shares = new_shares

            credited.add(credit_key)

    state["cash"] = round(cash, 2)
    state["events"] = events
    return state


def _find_price_with_date(kd, target):
    """返回 target 当日或之前最近价格及其实际日期。"""
    if not kd:
        return None, None
    try:
        if target in kd and float(kd[target]) > 0:
            return float(kd[target]), target
        d = datetime.strptime(target, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None, None
    for off in range(1, 15):
        c = (d - timedelta(days=off)).strftime("%Y-%m-%d")
        try:
            value = float(kd.get(c))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, c
    return None, None


def _find_price(kd, target):
    """取得 target 当日或之前最近交易日的收盘价。

    回测在检查点只能使用当日收盘（或之前）已知价格；向后搜索会把
    周末/停牌后的未来价格带回检查点，形成前视偏差。
    """
    return _find_price_with_date(kd, target)[0]


def build_snapshot(code, price, div_hist, as_of, dvd_detail_list=None):
    """构建纯股息率快照 row（PR 设为不限制值）。"""
    if dvd_detail_list is not None:
        dps = _point_in_time_trailing_dps(div_hist, dvd_detail_list, as_of)
        if dps is None:
            dps = _split_adjusted_trailing_dps(div_hist, dvd_detail_list, as_of)
    else:
        dps = _trailing_dps(div_hist, as_of)
    real_yield = (dps / price * 100.0) if dps and price > 0 else None
    return {
        "code": code, "name": code, "price": price,
        "yield": real_yield, "real_yield": real_yield,
        "pr": 0.5,  # 占位值，PR 过滤已设为不限制
        "dps": dps, "sustainability": "可持续",
        "industry": "未知行业", "sector": "未知行业", "bank": False,
    }


def _apply_quarterly_dividends(state, divs, prev_date, curr_date, rules, credited):
    """每季度为持仓入账分红和送转股。

    使用不复权价格交易，分红以现金入账，送转股按比例增加持仓股数。
    """
    cash = float(state.get("cash", 0))
    holdings = state.get("holdings", {})
    if not holdings:
        return state
    events = list(state.get("events") or [])
    curr_year = int(curr_date[:4])
    curr_month = int(curr_date[5:7]) if len(curr_date) >= 7 else 0
    credited = credited if credited is not None else set()

    for code, h in holdings.items():
        shares = int(h.get("shares", 0))
        if shares <= 0:
            continue
        dh = divs.get(code, [])
        # 中国 A 股分红通常在年中实施上一年度的分红
        if curr_month >= 4 and curr_month <= 9:
            target_year = curr_year - 1
        else:
            target_year = curr_year - 2
        credit_key = "%s_%d" % (code, target_year)
        if credit_key in credited:
            continue
        # 查找该年度记当
        record = None
        for item in dh:
            if item["year"] == target_year:
                record = item
                break
        if record is None:
            continue

        # 1. 现金分红入账
        dps = record.get("dps", 0)
        if dps > 0:
            gross = dps * shares
            # 持仓超1 年免税
            entry_date = str(h.get("entry_date", ""))
            holding_days = 400
            if len(entry_date) >= 10:
                try:
                    from datetime import date as dt_date
                    ed = dt_date.fromisoformat(entry_date[:10])
                    cd = dt_date.fromisoformat(curr_date[:10])
                    holding_days = max((cd - ed).days, 0)
                except Exception:
                    pass
            tax_rate = 0.0 if holding_days > 365 else (0.10 if holding_days > 30 else 0.20)
            tax = gross * tax_rate
            net = gross - tax
            cash += net
            events.append({
                "date": curr_date, "side": "dividend", "code": code,
                "shares": shares, "gross": round(gross, 2),
                "tax": round(tax, 2), "net_cash": round(net, 2),
                "reason": "DPS %.4f x %d shares" % (dps, shares),
            })

        # 2. 送转股调整持仓股数
        bonus = record.get("bonus_ratio", 0)
        transfer = record.get("transfer_ratio", 0)
        if bonus > 0 or transfer > 0:
            split_factor = 1.0 + (bonus + transfer) / 10.0
            new_shares = int(round(shares * split_factor))
            delta = new_shares - shares
            if delta > 0:
                events.append({
                    "date": curr_date, "side": "split", "code": code,
                    "shares": new_shares, "shares_before": shares,
                    "split_factor": round(split_factor, 4),
                    "reason": "10送%s转%s -> +%d shares" % (bonus, transfer, delta),
                })
                h["shares"] = new_shares

        credited.add(credit_key)

    state["cash"] = round(cash, 2)
    state["events"] = events
    return state


def _normalize_rebalance_dates(values, cutoff=None):
    """规范化调仓日期并按数据截止日截断。"""
    normalized = set()
    for value in values or []:
        text = str(value)[:10]
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        if cutoff and text > cutoff:
            continue
        normalized.add(text)
    return sorted(normalized)


def _rebalance_dates_hash(values) -> str:
    """按规范化日期序列计算调仓日期哈希。"""
    payload = json.dumps(list(values or []), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _portable_path(path: Path) -> str:
    """项目内路径优先写成相对形式，避免结果绑定本机绝对路径。"""
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _dynamic_pool_enabled(active: dict[str, Any], override: bool | None) -> bool:
    """解析候选池模式：显式覆盖优先，否则跟随当前规则配置。"""
    if override is not None:
        return bool(override)
    return str(active.get("pool_mode") or "").strip().lower() == "dynamic"


def run_backtest(rules=None, codes=None, rebalance_dates=None,
                 reinvest=False, verbose=True, dynamic_pool: bool | None = None,
                 manifest_path=None, rebalance_dates_path=None,
                 track_holdings: bool = False,
                 return_events: bool = False):
    active = dict(BACKTEST_RULES)
    if rules:
        active.update(rules)
    # CLI/调用方的显式开关必须真正传入策略账本；否则 --reinvest 仅出现在
    # 输出字段中，实际回测仍不会把分红现金买回股票。
    if reinvest:
        active["reinvest_dividends"] = True
    dynamic_pool = _dynamic_pool_enabled(active, dynamic_pool)
    codes, universe = _resolve_universe_codes(codes, dynamic_pool, manifest_path)
    if universe.get("kind") == "manifest":
        from universe_manifest import load_manifest, verify_cache_snapshot
        manifest_file = Path(universe["path"])
        if not manifest_file.is_absolute():
            manifest_file = ROOT / manifest_file
        loaded_manifest = load_manifest(manifest_file)
        cache_source = (loaded_manifest.get("source") or {}).get("path")
        cache_root = Path(cache_source) if cache_source else CACHE_DIR
        if not cache_root.is_absolute():
            cache_root = ROOT / cache_root
        if cache_root.resolve() != CACHE_DIR.resolve():
            raise ValueError("当前回测只支持 manifest 与 data/backtest_cache 同一数据目录")
        verify_cache_snapshot(loaded_manifest, cache_root)
    # manifest 提供的 as_of 是默认截止；显式 through_date 可用于固定池/显式代码
    # 的审计回放，避免缓存刷新后末端执行悄悄越过研究截止日。
    cutoff = str(active.get("through_date") or universe.get("as_of") or "")[:10] or None
    if cutoff:
        try:
            datetime.strptime(cutoff, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("through_date/as_of 必须为 YYYY-MM-DD") from exc
    rebalance_meta = {}
    if rebalance_dates_path:
        dates_path = Path(rebalance_dates_path)
        if not dates_path.is_absolute():
            dates_path = ROOT / dates_path
        dates_payload = json.loads(dates_path.read_text(encoding="utf-8"))
        expected_hash = None
        expected_manifest_hash = None
        if isinstance(dates_payload, dict):
            expected_hash = dates_payload.get("dates_sha256")
            source = dates_payload.get("source") or {}
            if isinstance(source, dict):
                expected_manifest_hash = source.get("manifest_records_sha256")
            raw_dates = dates_payload.get("dates") or dates_payload.get("rebalance_dates") or []
            if cutoff and dates_payload.get("as_of") and str(dates_payload["as_of"])[:10] != cutoff:
                raise ValueError("调仓日期文件 as_of 与 manifest 截止日不一致")
        else:
            raw_dates = dates_payload
        rdates = _normalize_rebalance_dates(raw_dates, cutoff)
        if expected_hash and _rebalance_dates_hash(rdates) != str(expected_hash):
            raise ValueError("调仓日期文件 dates_sha256 校验失败")
        if expected_manifest_hash:
            actual_manifest_hash = universe.get("records_sha256")
            if actual_manifest_hash and str(expected_manifest_hash) != str(actual_manifest_hash):
                raise ValueError("调仓日期文件绑定的 manifest 哈希不一致")
        rebalance_meta = {
            "path": _portable_path(dates_path),
            "sha256": _rebalance_dates_hash(rdates),
        }
    else:
        rdates = _normalize_rebalance_dates(
            rebalance_dates if rebalance_dates is not None else _get_monthly_dates(codes),
            cutoff,
        )
        rebalance_meta = {
            "source": "explicit" if rebalance_dates is not None else "generated_monthly_dates_v3",
            "sha256": _rebalance_dates_hash(rdates),
        }

    klines = {}
    divs = {}
    for i, code in enumerate(codes, 1):
        if verbose:
            print("  [%d/%d] %s" % (i, len(codes), code), end="\r")
        _kl_cached = _load_cache("kl_" + code) is not None
        try:
            klines[code] = fetch_kline(code, cutoff)
            if cutoff:
                klines[code] = {
                    str(day)[:10]: value for day, value in klines[code].items()
                    if str(day)[:10] <= cutoff
                }
        except Exception as e:
            print("\n  %s kline fail: %s" % (code, e))
            klines[code] = {}
        if not _kl_cached:
            time.sleep(0.15)
        _dv_cached = _load_cache("dv_" + code) is not None
        try:
            divs[code] = fetch_dividends(code)
            if cutoff:
                # 年度汇总只作兼容回退；严格路径使用下面的逐笔明细。
                divs[code] = [
                    item for item in divs[code]
                    if _as_int(item.get("year")) is not None
                    and _as_int(item.get("year")) <= int(cutoff[:4])
                ]
        except Exception as e:
            print("\n  %s div fail: %s" % (code, e))
            divs[code] = []
        if not _dv_cached:
            time.sleep(0.5)
    divs_detail = {}
    for code in codes:
        try:
            divs_detail[code] = _fetch_dividends_detail(code)
            if cutoff:
                divs_detail[code] = [
                    item for item in divs_detail[code]
                    if len(str(item.get("ex_date") or "")[:10]) == 10
                    and str(item.get("ex_date"))[:10] <= cutoff
                ]
        except Exception:
            divs_detail[code] = []

    # 动态候选池模式：每个调仓日按逐笔除权日筛选，避免年度汇总前视。
    calendar = _trading_calendar(klines)
    execution_lag = int(active.get("execution_lag_days") or 0)
    if execution_lag < 0:
        raise ValueError("execution_lag_days 不能为负数")
    info_lag = int(active.get("dividend_information_lag_days") or 0)
    if info_lag < 0:
        raise ValueError("dividend_information_lag_days 不能为负数")
    try:
        raw_pool_switch_month = active.get("pool_switch_month", 7)
        pool_switch_month = int(7 if raw_pool_switch_month is None else raw_pool_switch_month)
    except (TypeError, ValueError) as exc:
        raise ValueError("pool_switch_month 必须是 1-12 的整数") from exc
    if not 1 <= pool_switch_month <= 12:
        raise ValueError("pool_switch_month 必须是 1-12 的整数")

    def available_dividend_details(signal_date):
        if info_lag == 0:
            return divs_detail
        cutoff_date = datetime.strptime(signal_date, "%Y-%m-%d") - timedelta(days=info_lag)
        out = {}
        for code, items in divs_detail.items():
            out[code] = [item for item in (items or [])
                         if str(item.get("ex_date") or "")[:10] <= cutoff_date.strftime("%Y-%m-%d")]
        return out

    def signal_rows_for(codes_for_date, signal_date):
        rows_for_date = []
        details = available_dividend_details(signal_date)
        for code in codes_for_date:
            price = _find_price(klines.get(code, {}), signal_date)
            if not price or price <= 0:
                continue
            row = build_snapshot(
                code, price, divs.get(code, []), signal_date,
                details.get(code),
            )
            row["signal_date"] = signal_date
            rows_for_date.append(row)
        return rows_for_date

    def execution_rows(rows_for_signal, execution_date):
        rows_for_execution = []
        for source in rows_for_signal:
            row = dict(source)
            row["execution_date"] = execution_date
            mark_price, mark_date = _find_price_with_date(
                klines.get(str(source.get("code") or "").zfill(6), {}),
                execution_date,
            )
            row["mark_price"] = mark_price
            row["mark_price_date"] = mark_date
            row["execution_price"] = _exact_price(
                klines.get(str(source.get("code") or "").zfill(6), {}),
                execution_date,
            )
            if mark_date:
                try:
                    row["mark_price_age_days"] = (
                        datetime.strptime(execution_date, "%Y-%m-%d")
                        - datetime.strptime(mark_date, "%Y-%m-%d")
                    ).days
                except (TypeError, ValueError):
                    row["mark_price_age_days"] = None
            rows_for_execution.append(row)
        return rows_for_execution

    if verbose:
        print("\n数据就绪，开始回测...")

    nav_series = []
    state = None
    credited = set()
    pool_provenance = []

    def _nav_entry(d, st):
        entry = {"date": d, "nav": st["nav"], "cash": st.get("cash", 0)}
        if track_holdings:
            h = st.get("holdings") or {}
            entry["holdings"] = {
                c: {"shares": v.get("shares", 0), "entry_price": v.get("entry_price", 0)}
                for c, v in h.items()
            }
        return entry

    for rb in rdates:
        if dynamic_pool:
            min_yr = int(active.get("pool_min_consecutive_years") or 8)
            pool_codes = screen_dynamic_pool(
                divs, rb, min_yr, dividend_details_by_code=available_dividend_details(rb),
                pool_switch_month=pool_switch_month,
            )
        else:
            pool_codes = list(codes)
        pool_codes = sorted(set(pool_codes))
        pool_provenance.append({
            "signal_date": rb,
            "pool_count": len(pool_codes),
            "pool_codes_sha256": hashlib.sha256(",".join(pool_codes).encode("utf-8")).hexdigest(),
        })
        # 先记录执行日期，再构建信号行；没有任何可用价格的信号日也必须
        # 留下可审计的执行缺口，不能因提前 continue 而丢失 provenance。
        execution_date = rb if execution_lag == 0 else _next_trading_date(calendar, rb, execution_lag)
        pool_provenance[-1]["execution_date"] = execution_date
        # 已持仓即使暂时离开候选池，也必须进入本期核验和估值，
        # 否则会被错误当作缺失数据而冻结在旧成本价。
        held_codes = set((state or {}).get("holdings", {}))
        active_codes = sorted(set(pool_codes) | held_codes)
        all_signal_rows = signal_rows_for(active_codes, rb)
        if not all_signal_rows:
            continue

        # 动量过滤：只对新入场候选施加，已持仓股票保留
        mm = int(active.get("momentum_months") or 0)
        mp = str(active.get("momentum_periods") or "").strip()
        if mm > 0 or mp:
            _pl = lambda code, date: _find_price(klines.get(code, {}), date)
            signal_rows = momentum_filter(all_signal_rows, held_codes, _pl, rb, rdates, active)
            # 动量退出实验需要为已有持仓附带同一信号日计算的历史比率；
            # 默认未启用时不影响原有仅过滤新入场候选的行为。
            if float(active.get("momentum_exit_threshold") or 0.0) > 0 and held_codes:
                held_momentum = momentum_filter(all_signal_rows, set(), _pl, rb, rdates, active)
                ratios = {str(r.get("code")): r.get("momentum_ratio") for r in held_momentum}
                for row in signal_rows:
                    if str(row.get("code")) in held_codes and row.get("momentum_ratio") is None:
                        row["momentum_ratio"] = ratios.get(str(row.get("code")))
        else:
           signal_rows = all_signal_rows

        # 波动率计算：为每个候选行附加日频收益率标准差（仅用于 yield_vol 排序）
        if str(active.get("rank_by") or "yield") == "yield_vol":
            import statistics as _stat
            vol_days = int(active.get("volatility_lookback_days") or 120)
            for row in signal_rows:
                code = str(row.get("code") or "").zfill(6)
                kl = klines.get(code, {})
                dates_before = sorted(d for d in kl if d <= rb)
                window = dates_before[-(vol_days + 1):] if len(dates_before) > vol_days else dates_before
                if len(window) >= 10:
                    rets = []
                    for j in range(1, len(window)):
                        p0 = float(kl[window[j - 1]])
                        p1 = float(kl[window[j]])
                        if p0 > 0:
                            rets.append(p1 / p0 - 1.0)
                    row["volatility"] = round(_stat.pstdev(rets), 6) if len(rets) >= 2 else 0.0
                else:
                    row["volatility"] = 0.0

        if execution_date is None:
            # 最后一个信号点通常没有下一交易日；留给末端标记逻辑处理。
            continue
        if not signal_rows and state is not None:
            # 本期没有可入场候选时仍要按执行日给现有持仓估值，
            # 不能把上期 NAV 原样复制到本期。
            mark_rows = execution_rows(all_signal_rows, execution_date)
            state = _apply_precise_dividends(state, divs_detail, execution_date, active, credited)
            mark_rbc = {r["code"]: r for r in mark_rows}
            state["nav"] = round(
               float(state.get("cash", 0)) + _market_value(state.get("holdings", {}), mark_rbc), 2,
           )
            nav_series.append(_nav_entry(execution_date, state))
            continue
        rows = execution_rows(signal_rows, execution_date)

        if state is None:
            state = build_initial_ledger(rows, float(active["initial_capital"]), execution_date, active)
        else:
            state = _apply_precise_dividends(state, divs_detail, execution_date, active, credited)
            # rebalance_quarter 在启用分红再投资时会调用 reinvest_cash，
            # 因此这里不再重复执行一次再投资。
            state = rebalance_quarter(state, rows, execution_date, active)
            # 等权再平衡：削减超配并补充低配
            rbc = {r["code"]: r for r in rows}
            rebal_state = {"cash": state.get("cash", 0),
                          "holdings": state.get("holdings", {}),
                          "events": state.get("events", [])}
            rebal_state = rebalance_equally(rebal_state, rbc, execution_date, active)
            state["cash"] = rebal_state.get("cash", state.get("cash", 0))
            state["holdings"] = rebal_state.get("holdings", state.get("holdings", {}))
            state["events"] = rebal_state.get("events", state.get("events", []))
            mv = _market_value(state["holdings"], rbc)
            state["nav"] = round(state["cash"] + mv, 2)

        if verbose:
            _n = len(state.get("holdings", {}))
            nav = state.get("nav", 0)
            acts = state.get("actions", []) if rb != rdates[0] else []
            buys = sum(1 for a in acts if a.get("action") == "buy")
            sells = sum(1 for a in acts if a.get("action") == "sell")
            print("  signal=%s exec=%s: hold=%d NAV=%.0f buy=%d sell=%d" %
                  (rb, execution_date, _n, nav, buys, sells))

        if "nav" not in state:
            state["nav"] = state.get("cash", 0)
        nav_series.append(_nav_entry(execution_date, state))

    # 末端只有信号日、没有下一交易日时，按该日价格做一次只读估值，
    # 不执行新的买卖，确保样本外截止日不会被悄悄截断。
    if state and rdates:
        final_date = rdates[-1]
        if not nav_series or nav_series[-1]["date"] != final_date:
            if dynamic_pool:
                final_pool = screen_dynamic_pool(
                    divs, final_date,
                    int(active.get("pool_min_consecutive_years") or 8),
                    dividend_details_by_code=available_dividend_details(final_date),
                    pool_switch_month=pool_switch_month,
                )
            else:
                final_pool = list(codes)
            final_codes = sorted(set(final_pool) | set(state.get("holdings", {})))
            final_signal_rows = signal_rows_for(final_codes, final_date)
            final_rows = execution_rows(final_signal_rows, final_date)
            state = _apply_precise_dividends(state, divs_detail, final_date, active, credited)
            final_rbc = {r["code"]: r for r in final_rows}
            state["nav"] = round(
                float(state.get("cash", 0)) + _market_value(state.get("holdings", {}), final_rbc), 2,
            )
            nav_series.append(_nav_entry(final_date, state))

    metrics = _compute_metrics(nav_series, float(active["initial_capital"]))
    events = list((state or {}).get("events") or [])
    trade_events = [event for event in events if event.get("side") in ("买入", "卖出", "buy", "sell")]
    metrics.update({
        "observations": len(nav_series),
        "trade_count": len(trade_events),
        "buy_count": sum(1 for event in trade_events if event.get("side") in ("买入", "buy")),
        "sell_count": sum(1 for event in trade_events if event.get("side") in ("卖出", "sell")),
        "dividend_event_count": sum(1 for event in events if event.get("side") == "dividend"),
        "split_event_count": sum(1 for event in events if event.get("side") == "split"),
        "ending_cash": round(float((state or {}).get("cash") or 0.0), 2),
    })
    result = {
        "rules": dict(active),
        "dynamic_pool": bool(dynamic_pool),
        "universe": universe,
        "pool_provenance": pool_provenance,
        "data_cutoff": cutoff,
        "rebalance_dates": {
            "count": len(rdates),
            "first": rdates[0] if rdates else None,
            "last": rdates[-1] if rdates else None,
            **rebalance_meta,
        },
        # 兼容旧字段名，但记录实际账本开关；CLI 的显式开关仍由
        # ``reinvest_cli_flag`` 单独保留，避免输出与实际行为相反。
        "reinvest": bool(active.get("reinvest_dividends")),
        "reinvest_cli_flag": bool(reinvest),
        "metrics": metrics,
        "nav_series": nav_series,
        "final_holdings": [
            {"code": k, "shares": v["shares"], "entry_price": v["entry_price"]}
            for k, v in (state or {}).get("holdings", {}).items()
        ] if state else [],
    }

    if return_events:
        result["_events"] = events
    return result


def _reinvest_cash(state, rows, as_of, rules):
    """分红再投资：保留 3000 元现金，其余按持仓比例补买整数手。"""
    cash = float(state.get("cash", 0))
    if cash < 3000:
        return state
    holdings = state.get("holdings", {})
    if not holdings:
        return state
    rbc = {r["code"]: r for r in rows}
    lot = int(rules.get("lot_size", 100))
    total_mv = sum(
        (rbc[c]["price"] if c in rbc and rbc[c].get("price") else h.get("entry_price", 0)) * h["shares"]
        for c, h in holdings.items()
    ) or 1
    for code, h in list(holdings.items()):
        row = rbc.get(code)
        if not row or not row.get("price"):
            continue
        alloc = cash * 0.95 * (row["price"] * h["shares"] / total_mv)
        shares = int(alloc // (row["price"] * lot)) * lot
        if shares < lot:
            continue
        cost = row["price"] * shares
        fees = transaction_fees(cost, "buy", code, rules)
        total = cost + fees["total"]
        if total > cash:
            shares -= lot
            if shares < lot:
                continue
            cost = row["price"] * shares
            fees = transaction_fees(cost, "buy", code, rules)
            total = cost + fees["total"]
        if total > cash:
            continue
        cash -= total
        h["shares"] += shares
        state.setdefault("events", []).append({
            "date": as_of, "side": "buy", "code": code, "shares": shares,
            "price": row["price"], "reason": "分红现金再投资",
        })
    state["cash"] = round(cash, 2)
    return state


def _compute_metrics(nav_series, initial):
    if not nav_series:
        return {"cagr": 0, "max_drawdown": 0, "volatility": 0, "sharpe": 0, "ending_nav": initial}
    ending = float(nav_series[-1]["nav"])
    n_days = (datetime.strptime(nav_series[-1]["date"], "%Y-%m-%d") -
              datetime.strptime(nav_series[0]["date"], "%Y-%m-%d")).days
    years = max(n_days / 365.25, 0.01)
    cagr = (ending / initial) ** (1 / years) - 1
    returns = []
    for i in range(1, len(nav_series)):
        prev = float(nav_series[i - 1]["nav"])
        curr = float(nav_series[i]["nav"])
        if prev > 0:
            returns.append(curr / prev - 1)
    peak = nav_series[0]["nav"]
    max_dd = 0
    for item in nav_series:
        peak = max(peak, item["nav"])
        dd = (peak - item["nav"]) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    n_returns = len(returns)
    if n_returns > 1:
        ppr = n_returns / years
        mean_r = sum(returns) / n_returns
        var_r = sum((r - mean_r) ** 2 for r in returns) / (n_returns - 1)
        vol_a = math.sqrt(var_r) * math.sqrt(ppr)
        rf_period = (1.02) ** (1 / ppr) - 1
        excess = [r - rf_period for r in returns]
        mean_ex = sum(excess) / n_returns
        std_ex = math.sqrt(sum((r - mean_ex) ** 2 for r in excess) / (n_returns - 1))
        sharpe = (mean_ex / std_ex) * math.sqrt(ppr) if std_ex > 0 else 0
    else:
        vol_a = 0
        sharpe = 0
    return {
        "cagr": round(cagr * 100, 2), "max_drawdown": round(max_dd * 100, 2),
        "volatility": round(vol_a * 100, 2), "sharpe": round(sharpe, 3),
        "ending_nav": round(ending, 2), "total_return": round((ending / initial - 1) * 100, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="quarterly dividend backtest")
    parser.add_argument("--json", type=str)
    parser.add_argument("--param", nargs=2, action="append", default=[])
    parser.add_argument("--reinvest", action="store_true")
    pool_group = parser.add_mutually_exclusive_group()
    pool_group.add_argument("--dynamic-pool", dest="dynamic_pool", action="store_true",
                            help="使用动态候选池；未指定时跟随 strategy 规则")
    pool_group.add_argument("--curated-pool", dest="dynamic_pool", action="store_false",
                            help="强制使用兼容的固定候选池")
    parser.set_defaults(dynamic_pool=None)
    parser.add_argument("--manifest", type=str,
                        help="候选池 manifest 路径；动态池默认使用 data/universe_manifest.json")
    parser.add_argument("--rebalance-dates", type=str,
                        help="月度调仓日期 JSON 文件（列表或 {'dates': [...]}）")
    parser.add_argument("--max-position", type=float)
    args = parser.parse_args()
    rules = {}
    for key, val in args.param:
        try:
            rules[key] = float(val) if "." in val else int(val)
        except ValueError:
            rules[key] = val
    if args.max_position is not None:
        rules["max_position_pct"] = args.max_position
    result = run_backtest(
        rules=rules,
        reinvest=args.reinvest,
        dynamic_pool=args.dynamic_pool,
        manifest_path=args.manifest,
        rebalance_dates_path=args.rebalance_dates,
    )
    if result.get("dynamic_pool"):
        print("动态池来源: %s（%d 只）" % (
            result["universe"].get("kind"), result["universe"].get("count", 0),
        ))
    m = result["metrics"]
    print("\n" + "=" * 50)
    print("Ending NAV: %.0f" % m["ending_nav"])
    print("Total return: %.1f%%" % m["total_return"])
    print("CAGR: %.2f%%" % m["cagr"])
    print("Max drawdown: %.2f%%" % m["max_drawdown"])
    print("Sharpe: %.3f" % m["sharpe"])
    print("=" * 50)
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
