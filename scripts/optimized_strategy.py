"""可解释的高股息策略优化规则。

本模块只处理已经落盘的快照，不在排序阶段发起网络请求。它把「可持续」
作为硬门槛，把市赚率作为低估带和排序信息，并用分红连续性、现金覆盖、
信号持续性和行业上限减少股息陷阱。历史收益仍由公开回测报告单独提供。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RULES = {
    "yield_floor": 5.0,
    "pr_ceiling": 1.2,
    "strict_pr_ceiling": 0.5,
    "min_dividend_years": 8,
    "min_recent5_coverage": 1.0,
    "payout_min": 15.0,
    "payout_max": 85.0,
    "min_ocf_coverage": 1.2,
    "persistence_window": 5,
    "min_persistence": 3,
    "max_holdings": 10,
    "max_sector": 2,
    "max_banks": 2,
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _dps_years(row: dict[str, Any]) -> dict[int, float]:
    raw = ((row.get("dividend") or {}).get("years") or {})
    years: dict[int, float] = {}
    for key, value in raw.items():
        try:
            year = int(str(key)[:4])
        except (TypeError, ValueError):
            continue
        dps = _positive(value)
        if dps is not None:
            years[year] = dps
    return years


def _consecutive_years(years: dict[int, float], latest: int | None) -> int:
    if latest is None:
        return 0
    count = 0
    year = latest
    while year in years:
        count += 1
        year -= 1
    return count


def _recent_coverage(years: dict[int, float], latest: int | None, window: int = 5) -> float:
    if latest is None or window <= 0:
        return 0.0
    return sum(1 for year in range(latest - window + 1, latest + 1) if year in years) / window


def _cagr(first: float | None, last: float | None, periods: int) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or periods <= 0:
        return None
    return (last / first) ** (1.0 / periods) - 1.0


def _persistence(
    snapshots: dict[str, list[dict[str, Any]]],
    code: str,
    rules: dict[str, float | int],
) -> tuple[int, int]:
    """统计最近快照中同时满足三项主信号的次数。

    快照只有十二个交易日，不能当成长期回测；这里仅用来识别一天价格
    波动造成的短暂入选，并在页面中明确标注为稳定性证据。
    """
    rows = snapshots.get(code, [])
    window = int(rules["persistence_window"])
    recent = rows[-window:]
    passed = 0
    for row in recent:
        yield_value = _number(row.get("真实股息率%"))
        pr = _number(row.get("市赚率PR"))
        if (
            row.get("可持续性") == "可持续"
            and yield_value is not None
            and yield_value >= float(rules["yield_floor"])
            and pr is not None
            and pr <= float(rules["pr_ceiling"])
        ):
            passed += 1
    return passed, len(recent)


def _sector(industry: str | None) -> str:
    value = (industry or "未知行业").strip()
    return value.split("-", 1)[0] or "未知行业"


def _is_bank(industry: str | None) -> bool:
    return "银行" in (industry or "")


def _quality_score(item: dict[str, Any], rules: dict[str, float | int]) -> float:
    """固定尺度的质量分，仅作同股息率下的次级排序。

    真实股息率始终是第一排序键；质量分不能把低股息率股票硬抬到高股息率
    股票之上。各分项均有上限，避免一个异常字段主导结果。
    """
    floor = float(rules["yield_floor"])
    ceiling = float(rules["pr_ceiling"])
    yield_part = min(max((float(item.get("yield") or 0) - floor) / 3.0, 0.0), 1.0) * 35.0
    streak_part = min(float(item.get("streak") or 0) / 15.0, 1.0) * 20.0
    coverage_part = min(max(float(item.get("recent5_coverage") or 0), 0.0), 1.0) * 10.0

    if item.get("bank"):
        # 银行现金流量表受存贷款变动影响，不能与实体企业直接横比。
        cash_part = 15.0
    else:
        coverage = item.get("ocf_coverage")
        cash_part = min(max((float(coverage or 0) - 1.0) / 3.0, 0.0), 1.0) * 15.0

    payout = item.get("payout_ratio")
    if payout is None:
        payout_part = 0.0
    else:
        distance = abs(float(payout) - 50.0)
        payout_part = max(0.0, 1.0 - distance / 50.0) * 10.0

    pr = item.get("pr")
    pr_part = max(0.0, 1.0 - float(pr or ceiling) / ceiling) * 10.0
    return round(yield_part + streak_part + coverage_part + cash_part + payout_part + pr_part, 2)


def _gate(item: dict[str, Any], rules: dict[str, float | int]) -> list[str]:
    reasons: list[str] = []
    yield_value = item.get("yield")
    pr = item.get("pr")
    if item.get("sustainability") != "可持续":
        reasons.append("可持续性不是“可持续”")
    if yield_value is None or float(yield_value) < float(rules["yield_floor"]):
        reasons.append(f"真实股息率低于 {float(rules['yield_floor']):g}%")
    if pr is None or float(pr) > float(rules["pr_ceiling"]):
        reasons.append(f"PR 高于 {float(rules['pr_ceiling']):g}")
    if int(item.get("dividend_years") or 0) < int(rules["min_dividend_years"]):
        reasons.append(f"历史分红年份少于 {int(rules['min_dividend_years'])} 年")
    if float(item.get("recent5_coverage") or 0) < float(rules["min_recent5_coverage"]):
        reasons.append("最近五个完整财年分红不连续")
    payout = item.get("payout_ratio")
    if payout is None:
        reasons.append("支付率无法复算")
    elif not (float(rules["payout_min"]) <= float(payout) <= float(rules["payout_max"])):
        reasons.append("支付率落在保守区间外")
    if not item.get("bank"):
        coverage = item.get("ocf_coverage")
        if coverage is None or float(coverage) < float(rules["min_ocf_coverage"]):
            reasons.append(f"经营现金流覆盖低于 {float(rules['min_ocf_coverage']):g} 倍")
    passed, window = int(item.get("persistence_count") or 0), int(item.get("persistence_window") or 0)
    if window and passed < int(rules["min_persistence"]):
        reasons.append(f"最近 {window} 个快照信号只出现 {passed} 次")
    return reasons


def enrich_rows(
    verified: dict[str, Any],
    screener_rows: Iterable[dict[str, Any]],
    snapshots: dict[str, list[dict[str, Any]]] | None = None,
    rules: dict[str, float | int] | None = None,
) -> list[dict[str, Any]]:
    """合并当前独立核验数据、页面行业字段和短期快照稳定性。"""
    active = dict(DEFAULT_RULES)
    if rules:
        active.update(rules)
    page_by_code = {str(row.get("代码") or "").zfill(6): row for row in screener_rows}
    snapshots = snapshots or {}
    result: list[dict[str, Any]] = []
    for source in verified.get("rows") or []:
        code = str(source.get("code") or "").zfill(6)
        page = page_by_code.get(code, {})
        quote = source.get("quote") or {}
        dividend = source.get("dividend") or {}
        financial = source.get("financial") or {}
        years = _dps_years(source)
        latest = max(years) if years else None
        dps = _positive(dividend.get("dps_per_share"))
        shares = _positive(quote.get("total_shares"))
        net_profit = _positive(financial.get("net_profit"))
        operating_cf = _number(financial.get("operating_cf"))
        dividend_cash = dps * shares if dps is not None and shares is not None else None
        payout = (dividend_cash / net_profit * 100.0 if dividend_cash and net_profit else None)
        ocf_coverage = (operating_cf / dividend_cash if operating_cf is not None and dividend_cash else None)
        recent_years = [years.get(year) for year in range((latest or 0) - 4, (latest or 0) + 1)] if latest else []
        available = [value for value in recent_years if value is not None]
        dps_cagr = _cagr(available[0], available[-1], len(available) - 1) if len(available) >= 2 else None
        persistence_count, persistence_window = _persistence(snapshots, code, active)
        industry = str(page.get("行业") or source.get("industry") or "未知行业")
        item: dict[str, Any] = {
            "code": code,
            "name": source.get("name") or page.get("名称") or code,
            "yield": _number(source.get("page_real_yield")),
            "ttm_yield": _number(page.get("TTM股息率%")),
            "pr": _number(source.get("page_pr")),
            "zone": source.get("page_zone") or page.get("估值区间") or "未分区",
            "sustainability": source.get("page_sustainability") or page.get("可持续性") or "未评估",
            "industry": industry,
            "sector": _sector(industry),
            "bank": _is_bank(industry),
            "roe": _number(financial.get("roe")),
            "price": _number(quote.get("price")),
            "pe_ttm": _number(quote.get("pe_ttm")),
            "market_cap_yi": _number(quote.get("market_cap_yi")),
            "dps": dps,
            "latest_complete_year": dividend.get("latest_complete_year"),
            "dividend_years": len(years),
            "streak": _consecutive_years(years, latest),
            "recent5_coverage": _recent_coverage(years, latest),
            "dps_cagr5": (dps_cagr * 100.0 if dps_cagr is not None else None),
            "dps_min5": min(available) if available else None,
            "payout_ratio": payout,
            "ocf_coverage": ocf_coverage,
            "operating_cf": operating_cf,
            "persistence_count": persistence_count,
            "persistence_window": persistence_window,
            "yield_delta": _number(source.get("delta")),
            "data_date": source.get("page_updated") or verified.get("as_of"),
        }
        item["quality_score"] = _quality_score(item, active)
        item["gate_reasons"] = _gate(item, active)
        item["eligible"] = not item["gate_reasons"]
        item["strict_low"] = bool(item["eligible"] and item.get("pr") is not None and item["pr"] <= float(active["strict_pr_ceiling"]))
        result.append(item)
    return result


def select_portfolio(
    rows: Iterable[dict[str, Any]],
    rules: dict[str, float | int] | None = None,
) -> list[dict[str, Any]]:
    """按真实股息率降序选取组合，并限制行业和银行集中度。"""
    active = dict(DEFAULT_RULES)
    if rules:
        active.update(rules)
    candidates = [row for row in rows if row.get("eligible")]
    candidates.sort(key=lambda row: (-float(row.get("yield") or -1), -float(row.get("quality_score") or 0), float(row.get("pr") or 99)))
    selected: list[dict[str, Any]] = []
    sector_count: dict[str, int] = {}
    bank_count = 0
    for row in candidates:
        sector = str(row.get("sector") or "未知行业")
        if sector_count.get(sector, 0) >= int(active["max_sector"]):
            continue
        if row.get("bank") and bank_count >= int(active["max_banks"]):
            continue
        selected.append(row)
        sector_count[sector] = sector_count.get(sector, 0) + 1
        if row.get("bank"):
            bank_count += 1
        if len(selected) >= int(active["max_holdings"]):
            break
    for index, row in enumerate(selected, start=1):
        row["portfolio_rank"] = index
    return selected


def load_dataset(base: Path, rules: dict[str, float | int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取交付目录中的核验 JSON、当前页面和最近快照。"""
    verified_path = base / "verified_current.json"
    screener_path = base / "dividend-calculator" / "dividend-calculator" / "site" / "screener" / "screener_2026-08-24.json"
    snapshot_dir = screener_path.parent
    verified = json.loads(verified_path.read_text(encoding="utf-8"))
    screener_rows = json.loads(screener_path.read_text(encoding="utf-8"))
    # 先按日期保存整批数据，再给缺席某日的股票补空行；否则“出现次数”会
    # 被误当成“最近窗口内的通过次数”，缺席快照不会受到应有的惩罚。
    snapshot_batches: list[dict[str, dict[str, Any]]] = []
    all_codes: set[str] = set()
    for path in sorted(snapshot_dir.glob("screener_2026-08-*.json")):
        batch: dict[str, dict[str, Any]] = {}
        for row in json.loads(path.read_text(encoding="utf-8")):
            code = str(row.get("代码") or "").zfill(6)
            batch[code] = row
            all_codes.add(code)
        snapshot_batches.append(batch)
    snapshots = {
        code: [batch.get(code, {}) for batch in snapshot_batches]
        for code in all_codes
    }
    rows = enrich_rows(verified, screener_rows, snapshots=snapshots, rules=rules)
    return rows, select_portfolio(rows, rules=rules)


__all__ = ["DEFAULT_RULES", "enrich_rows", "load_dataset", "select_portfolio"]
