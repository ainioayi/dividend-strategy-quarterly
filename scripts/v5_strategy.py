"""V5 高息动量策略的纯规则函数。

调用方必须传入截至信号日可见的新浪前复权因子、冻结不复权缓存和财务记录。
本模块不联网，也不把缺失窗口缩短后继续计算。
"""
from __future__ import annotations

import math
import statistics
import hashlib
import json
import argparse
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Sequence


V5_RULES: dict[str, float | int] = {
    "initial_capital": 100000.0,
    "frequency": "monthly",
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "volatility_window": 60,
    "max_volatility": 0.50,
    "downside_window": 50,
    "risk_target": 0.10,
    "risk_floor": 0.40,
    "max_holdings": 6,
    "max_industry": 1,
    "rebalance_band": 0.20,
    "tight_rebalance_band": 0.05,
    "lot_size": 100,
    "commission_rate": 0.00025,
    "min_commission": 5.0,
    "transfer_fee_rate": 0.00001,
}

V5_ATTACHMENT_SHA256 = {
    "report_pdf": "fbc49e2500e7158f735a5968cac9361db5f0c141a1eefd5c557bf70b7cd79609",
    "appendix_xlsx": "a54496c7ab96f0d9b72f6c3272a0ad081940b67cd187b8e6afa251f4cdf845aa",
}


def daily_returns(prices: Sequence[float]) -> list[float]:
    """由价格生成简单日收益；坏值直接拒绝，避免风险阀门失真。"""
    values = [float(value) for value in prices]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("价格必须是有限正数")
    return [values[index] / values[index - 1] - 1 for index in range(1, len(values))]


def downside_semideviation(returns: Sequence[float], window: int = 50) -> float | None:
    """年化下行半偏差：sqrt(252 * mean(min(r, 0)^2))。"""
    if window <= 0:
        raise ValueError("window 必须为正整数")
    values = [float(value) for value in returns]
    if len(values) < window:
        return None
    values = values[-window:]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("收益率必须为有限数")
    return math.sqrt(252 * sum(min(value, 0.0) ** 2 for value in values) / window)


def annualized_volatility(prices: Sequence[float], window: int = 60) -> float | None:
    """最近 ``window`` 个前复权日收益的样本标准差年化值。"""
    returns = daily_returns(prices)
    if len(returns) < window:
        return None
    return statistics.stdev(returns[-window:]) * math.sqrt(252)


def momentum_ratio(prices: Sequence[float], lookback_days: int) -> float | None:
    """前复权价格动量比；不足完整窗口时返回 ``None``。"""
    values = [float(value) for value in prices]
    if len(values) <= lookback_days:
        return None
    daily_returns(values)  # 统一执行价格校验。
    return values[-1] / values[-lookback_days - 1]


def four_month_momentum(rows: Sequence[dict[str, Any]], signal_date: str) -> float | None:
    """按日历四个月取信号日前最后可用前复权收盘价。"""
    signal = date.fromisoformat(signal_date)
    month_index = signal.year * 12 + signal.month - 1 - 4
    target_year, target_month0 = divmod(month_index, 12)
    target_month = target_month0 + 1
    target_day = min(signal.day, 28)
    target = date(target_year, target_month, target_day).isoformat()
    available = sorted((row for row in rows if str(row["date"]) <= signal_date),
                       key=lambda row: row["date"])
    old = [row for row in available if str(row["date"]) <= target]
    if not old or not available:
        return None
    return float(available[-1]["close"]) / float(old[-1]["close"])


def risk_multiplier(strategy_returns: Sequence[float], index_returns: Sequence[float]) -> float | None:
    """计算 V5 双下行风险阀门；任一 50 日窗口不足便不生成目标。"""
    strategy_dvol = downside_semideviation(strategy_returns)
    index_dvol = downside_semideviation(index_returns)
    if strategy_dvol is None or index_dvol is None:
        return None
    strategy_limit = math.inf if strategy_dvol == 0 else 0.10 / strategy_dvol
    index_limit = math.inf if index_dvol == 0 else 0.10 / index_dvol
    return max(0.40, min(1.0, strategy_limit, index_limit))


def rebalance_band(previous_multiplier: float | None, multiplier: float) -> float:
    """阀门较上月严格下降超过 2 个百分点时使用 ±5%，否则 ±20%。"""
    decline = 0.0 if previous_multiplier is None else previous_multiplier - multiplier
    return 0.05 if decline > 0.02 and not math.isclose(decline, 0.02, abs_tol=1e-12) else 0.20


def new_buy_budget_multiplier(index_prices: Sequence[float], lookback_days: int = 240) -> float | None:
    """H00922 低于 240 个交易日前时，新建仓预算减半。"""
    ratio = momentum_ratio(index_prices, lookback_days)
    return None if ratio is None else (0.5 if ratio < 1 else 1.0)


def payout_covered(eps: Any, dps: Any, published_date: str, signal_date: str) -> bool:
    """财报在信号日前可见、EPS 为正且每股现金分红不超过 EPS。"""
    if not published_date or published_date > signal_date:
        return False
    try:
        earnings, dividend = float(eps), float(dps)
    except (TypeError, ValueError):
        return False
    return math.isfinite(earnings) and math.isfinite(dividend) and earnings > 0 and 0 <= dividend <= earnings


def dividend_cut_exit(previous_dps: Any, current_dps: Any, signal_date: str) -> bool:
    """年度分红削减超过 30% 时退出；7–8 月暂停检查。"""
    month = date.fromisoformat(signal_date).month
    if month in (7, 8):
        return False
    try:
        previous, current = float(previous_dps), float(current_dps)
    except (TypeError, ValueError):
        return False
    return previous > 0 and current >= 0 and current / previous < 0.70


def transaction_fees(gross: float, side: str, trade_date: str,
                     multiplier: float = 1.0) -> dict[str, float]:
    """V5 单边费用；2023-08-28 起卖出印花税降为万五。"""
    if side not in {"buy", "sell"}:
        raise ValueError("side 必须是 buy 或 sell")
    gross = max(float(gross), 0.0)
    commission = max(5.0, gross * 0.00025) if gross else 0.0
    stamp_rate = 0.0005 if trade_date >= "2023-08-28" else 0.001
    stamp = gross * stamp_rate if side == "sell" else 0.0
    transfer = gross * 0.00001
    values = {"commission": commission, "stamp_duty": stamp, "transfer_fee": transfer}
    values = {key: value * multiplier for key, value in values.items()}
    return {**values, "total": sum(values.values())}


def cash_interest(cash: float, year: int, trading_days: int = 1) -> float:
    """按附件区间下限和 252 个交易日计算闲置现金利息。"""
    if trading_days < 0:
        raise ValueError("trading_days 不能为负")
    if 2016 <= year <= 2018:
        rate = 0.026
    elif 2019 <= year <= 2021:
        rate = 0.021
    elif 2022 <= year <= 2024:
        rate = 0.019
    elif 2025 <= year <= 2026:
        rate = 0.014
    else:
        raise ValueError("附件未给出该年度货基利率")
    return max(float(cash), 0.0) * rate * trading_days / 252


def round_lot_shares(budget: float, price: float, lot_size: int = 100) -> int:
    """按预算向下取 100 股整数手；费用由成交层另行扣除。"""
    if price <= 0 or lot_size <= 0:
        raise ValueError("price 和 lot_size 必须为正数")
    return max(0, math.floor(float(budget) / price / lot_size) * lot_size)


def select_candidates(rows: Sequence[dict[str, Any]], max_holdings: int = 6) -> list[dict[str, Any]]:
    """按股息率降序选择，每个证监会行业大类最多一只。

    行记录需由调用方先填入 ``payout_covered``、``momentum``、``volatility``；
    波动率超过 50% 的股票仅在合格池不足六只时按低波动顺序回补。
    """
    eligible = [row for row in rows if row.get("payout_covered") is True
                and float(row.get("yield", 0)) >= 7.5
                and float(row.get("momentum", 0)) >= 0.85
                and row.get("industry")]
    normal = sorted((row for row in eligible if row.get("volatility") is not None
                     and float(row["volatility"]) <= 0.50),
                    key=lambda row: (-float(row["yield"]), str(row.get("code", ""))))
    high_vol = sorted((row for row in eligible if row.get("volatility") is not None
                       and float(row["volatility"]) > 0.50),
                      key=lambda row: (float(row["volatility"]), -float(row["yield"]),
                                       str(row.get("code", ""))))
    selected: list[dict[str, Any]] = []
    industries: set[str] = set()
    for row in normal + high_vol:
        industry = str(row["industry"]).strip()
        if industry in industries:
            continue
        selected.append(dict(row))
        industries.add(industry)
        if len(selected) == max_holdings:
            break
    return selected


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _adjusted_price_rows(code: str, factors: Sequence[dict[str, Any]], cache_dir: Path,
                         as_of: str) -> list[dict[str, Any]]:
    """用新浪阶梯因子和冻结不复权缓存重建前复权序列。"""
    path = Path(cache_dir) / f"kl_{code}.json"
    if not path.exists():
        raise FileNotFoundError(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    ordered = sorted((row for row in factors if str(row.get("code")) == code
                      and str(row.get("date", "")) <= as_of), key=lambda row: row["date"])
    result, index, current = [], 0, None
    for day, close in sorted(raw.items()):
        if day > as_of:
            break
        while index < len(ordered) and ordered[index]["date"] <= day:
            current = ordered[index]
            index += 1
        if current is None or float(current.get("factor", 0)) <= 0:
            raise ValueError(f"{code} 的 {day} 不在新浪前复权因子覆盖内")
        raw_close = float(close)
        result.append({"code": code, "date": day, "unadjusted_close": raw_close,
                       "close": raw_close / float(current["factor"])})
    return result


def _load_v5_inputs(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (payload.get("strategy") != "v5"
            or payload.get("price_format") != "sina_qfq_factors_with_unadjusted_cache"):
        raise ValueError("V5 输入策略或价格口径不正确")
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("V5 输入缺少 inputs")
    for name in ("adjustment_factors", "fundamentals", "industries", "h00922"):
        rows = inputs.get(name)
        if not isinstance(rows, list) or payload.get("hashes", {}).get(name) != _canonical_sha256(rows):
            raise ValueError(f"V5 输入 {name} 缺失或哈希校验失败")
    nav_rows = inputs.get("strategy_nav")
    if not isinstance(nav_rows, list) or payload.get("hashes", {}).get("strategy_nav") != _canonical_sha256(nav_rows):
        raise ValueError("V5 输入 strategy_nav 缺失或哈希校验失败")
    content = dict(payload)
    expected = content.pop("content_sha256", None)
    if expected != _canonical_sha256(content):
        raise ValueError("V5 输入 content_sha256 校验失败")
    return payload


def build_forward_signal(signal_date: str, manifest_path: Path, dates_path: Path,
                         cache_dir: Path, journal_rows: Sequence[dict[str, Any]],
                         v5_input_path: Path) -> dict[str, Any]:
    """生成可追加的 V5 signal 事件，不写账本；输入不完整时失败关闭。"""
    date.fromisoformat(signal_date)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    codes = set(manifest.get("codes") or [row.get("code") for row in manifest.get("records", [])])
    if not codes or str(manifest.get("as_of", ""))[:10] != signal_date:
        raise ValueError("manifest 必须包含股票且截止日等于信号日")
    dates_payload = json.loads(Path(dates_path).read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    if signal_date not in dates:
        raise ValueError("信号日不在冻结月末日期中")
    snapshot = _load_v5_inputs(Path(v5_input_path))
    if snapshot.get("as_of") != signal_date:
        raise ValueError("V5 输入截止日必须等于信号日")
    inputs = snapshot["inputs"]
    price_rows = {
        code: _adjusted_price_rows(code, inputs["adjustment_factors"], cache_dir, signal_date)
        for code in sorted(str(code) for code in codes)
    }
    fundamentals_by_year: dict[str, dict[int, dict[str, Any]]] = {}
    for row in inputs["fundamentals"]:
        code = str(row.get("code", ""))
        if code in codes and str(row.get("published_date", "")) <= signal_date:
            year = int(row["year"])
            current = fundamentals_by_year.setdefault(code, {}).get(year)
            if current is None or str(row["published_date"]) > str(current["published_date"]):
                fundamentals_by_year[code][year] = row
    industries: dict[str, str] = {}
    industry_dates: dict[str, str] = {}
    for row in inputs["industries"]:
        code, published = str(row.get("code", "")), str(row.get("published_date", ""))
        if code in codes and published <= signal_date and published >= industry_dates.get(code, ""):
            industries[code], industry_dates[code] = str(row.get("industry", "")), published
    previous = next((row for row in reversed(journal_rows)
                     if row.get("event_type") == "execution"), None)
    held_codes = {str(row.get("code")) for row in (previous or {}).get("holdings", [])}
    signal_month = date.fromisoformat(signal_date).month
    end_year = date.fromisoformat(signal_date).year - (1 if signal_month >= 7 else 2)
    candidates = []
    for code, rows in price_rows.items():
        rows.sort(key=lambda row: row["date"])
        prices = [float(row["close"]) for row in rows]
        annual = fundamentals_by_year.get(code, {})
        required = [annual.get(year) for year in range(end_year - 2, end_year + 1)]
        if any(row is None or float(row.get("dps") or 0) <= 0 for row in required) or len(prices) < 61:
            continue
        financial = annual[end_year]
        dps = financial.get("dps")
        covered = payout_covered(financial.get("eps"), dps,
                                 str(financial.get("published_date", "")), signal_date)
        momentum = four_month_momentum(rows, signal_date)
        cut = dividend_cut_exit(annual.get(end_year - 1, {}).get("dps"), dps, signal_date)
        candidates.append({
            "code": code, "industry": industries.get(code, ""),
            "yield": float(dps) / float(rows[-1]["unadjusted_close"]) * 100 if dps is not None else 0,
            "momentum": momentum, "volatility": annualized_volatility(prices),
            "payout_covered": covered, "dividend_cut_exit": cut,
        })
    entries = select_candidates([row for row in candidates if not row["dividend_cut_exit"]])
    retained = sorted((row for row in candidates if row["code"] in held_codes
                       and row["yield"] >= 5.5 and not row["dividend_cut_exit"]),
                      key=lambda row: (-float(row["yield"]), row["code"]))
    selected, used_industries = [], set()
    for row in retained + entries:
        if row["industry"] in used_industries or any(old["code"] == row["code"] for old in selected):
            continue
        selected.append(row)
        used_industries.add(row["industry"])
        if len(selected) == 6:
            break
    index_rows = sorted((row for row in inputs["h00922"] if row["date"] <= signal_date),
                        key=lambda row: row["date"])
    index_prices = [float(row["close"]) for row in index_rows]
    buy_gate = new_buy_budget_multiplier(index_prices)
    if buy_gate is None:
        raise ValueError("H00922 不足 240 个交易日，不能生成 V5 信号")
    nav_values = [float(row["nav"]) for row in inputs["strategy_nav"]
                  if str(row.get("date", "")) <= signal_date]
    if len(nav_values) < 51:
        raise ValueError("V5 可回放日频 NAV 不足 51 点，不能计算策略下行半偏差")
    strategy_returns = daily_returns(nav_values)
    index_returns = daily_returns(index_prices)
    multiplier = risk_multiplier(strategy_returns, index_returns)
    if multiplier is None:
        raise ValueError("策略或 H00922 不足 50 日下行风险窗口")
    previous_multiplier = (previous or {}).get("risk_multiplier")
    target_codes = [row["code"] for row in selected]
    return {
        "schema_version": 1, "event_type": "signal", "strategy_id": "v5",
        "strategy_version": "V5", "shadow": True, "period": signal_date[:7],
        "signal_date": signal_date, "target_codes": target_codes,
        "candidate_pool": {"count": len(candidates), "codes": sorted(row["code"] for row in candidates)},
        "decision_snapshot": {"held_codes": sorted(held_codes),
                              "eligible_entry_codes": target_codes},
        "candidates": selected, "new_buy_budget_multiplier": buy_gate,
        "risk_multiplier": multiplier,
        "rebalance_band": rebalance_band(previous_multiplier, multiplier),
        "previous_nav": previous.get("nav") if previous else 100000.0,
        "v5_input_sha256": snapshot["content_sha256"],
    }


def build_forward_execution(period: str, execution_date: str, cache_dir: Path,
                            journal_rows: Sequence[dict[str, Any]],
                            v5_input_path: Path) -> dict[str, Any]:
    """按指定日期精确收盘价生成 execution 事件，不写账本。"""
    date.fromisoformat(execution_date)
    snapshot = _load_v5_inputs(Path(v5_input_path))
    signals = [row for row in journal_rows if row.get("event_type") == "signal"
               and row.get("strategy_id") == "v5" and row.get("period") == period]
    if len(signals) != 1:
        raise ValueError("执行期必须恰有一个 V5 信号")
    signal = signals[0]
    if execution_date <= signal["signal_date"]:
        raise ValueError("执行日必须晚于信号日")
    previous = next((row for row in reversed(journal_rows)
                     if row.get("event_type") == "execution"
                     and str(row.get("execution_date", "")) < execution_date), None)
    holdings = {str(row["code"]): dict(row) for row in (previous or {}).get("holdings", [])}
    cash = float((previous or {}).get("cash", V5_RULES["initial_capital"]))
    target_codes = list(signal.get("target_codes", []))
    fee_multiplier = float(signal.get("fee_multiplier", 1.0))
    all_codes = sorted(set(target_codes) | set(holdings))
    prices: dict[str, float] = {}
    valuation_prices: dict[str, float] = {}
    for code in all_codes:
        path = Path(cache_dir) / f"kl_{code}.json"
        rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        value = rows.get(execution_date) if isinstance(rows, dict) else None
        if value is not None and float(value) > 0:
            prices[code] = float(value)
        visible = [float(close) for day, close in rows.items()
                   if day <= execution_date and float(close) > 0]
        if visible:
            valuation_prices[code] = visible[-1]
        elif code in holdings:
            raise ValueError(f"{code} 缺少执行日前估值价格")
    operations: list[dict[str, Any]] = []
    previous_date = str((previous or {}).get("execution_date") or signal["signal_date"])
    trading_dates = sorted({day for code in all_codes
                            for day in (json.loads((Path(cache_dir) / f"kl_{code}.json").read_text(encoding="utf-8"))
                                        if (Path(cache_dir) / f"kl_{code}.json").exists() else {})
                            if previous_date < day <= execution_date})
    # 按真实交易日日序处理：期初现金先计息，再把当日除权现金分红入账。
    credited = {(str(op.get("code")), str(op.get("ex_date"))) for row in journal_rows
                for op in row.get("operations", []) if op.get("side") == "分红"}
    dividends_by_date: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for code, holding in holdings.items():
        dividend_path = Path(cache_dir) / f"dvd_{code}.json"
        dividends = json.loads(dividend_path.read_text(encoding="utf-8")) if dividend_path.exists() else []
        for row in dividends:
            ex_date = str(row.get("ex_date", ""))[:10]
            if previous_date < ex_date <= execution_date and (code, ex_date) not in credited:
                dividends_by_date.setdefault(ex_date, []).append((code, row))
    interest = 0.0
    for trading_day in trading_dates:
        daily_interest = cash_interest(cash, date.fromisoformat(trading_day).year)
        cash += daily_interest
        interest += daily_interest
        for code, row in dividends_by_date.get(trading_day, []):
            holding = holdings[code]
            shares_before = int(holding["shares"])
            gross = shares_before * float(row.get("dps") or 0)
            cash += gross
            operations.append({"date": execution_date, "ex_date": trading_day, "side": "分红",
                               "code": code, "shares": shares_before, "gross": gross,
                               "net_cash": gross, "fees": {"total": 0.0}, "reason": "现金分红"})
            ratio = float(row.get("bonus_ratio") or 0) + float(row.get("transfer_ratio") or 0)
            if ratio > 0:
                new_shares = shares_before + int(shares_before * ratio / 10)
                holding["shares"] = new_shares
                operations.append({"date": execution_date, "ex_date": trading_day, "side": "送转",
                                   "code": code, "shares": new_shares, "gross": 0.0,
                                   "net_cash": 0.0, "fees": {"total": 0.0},
                                   "reason": "送转股本调整"})
    if interest:
        operations.insert(0, {"date": execution_date, "side": "利息", "gross": interest,
                              "net_cash": interest, "reason": "闲置现金按真实交易日逐日计息"})
    # 先卖出退出标的。
    for code in sorted(set(holdings) - set(target_codes)):
        if code not in prices:
            continue
        shares, price = int(holdings[code]["shares"]), prices[code]
        gross = shares * price
        fees = transaction_fees(gross, "sell", execution_date, fee_multiplier)
        cash += gross - fees["total"]
        operations.append({"date": execution_date, "side": "卖出", "code": code,
                           "shares": shares, "price": price, "gross": gross,
                           "net_cash": gross - fees["total"], "fees": fees, "reason": "V5 退出或轮换"})
        holdings.pop(code)
    nav_before = cash + sum(int(row["shares"]) * valuation_prices[code]
                            for code, row in holdings.items())
    target_value = nav_before * float(signal["risk_multiplier"]) / max(len(target_codes), 1)
    band = float(signal["rebalance_band"])
    # 卖出超配，再买入低配；只有新建仓应用 H00922 半预算。
    for code in target_codes:
        if code not in holdings or code not in prices:
            continue
        value = int(holdings[code]["shares"]) * prices[code]
        if value > target_value * (1 + band):
            shares = round_lot_shares(value - target_value, prices[code])
            if shares:
                gross, fees = shares * prices[code], transaction_fees(shares * prices[code], "sell", execution_date, fee_multiplier)
                cash += gross - fees["total"]
                holdings[code]["shares"] -= shares
                operations.append({"date": execution_date, "side": "卖出", "code": code,
                                   "shares": shares, "price": prices[code], "gross": gross,
                                   "net_cash": gross - fees["total"], "fees": fees, "reason": "超过再平衡上带"})
    for code in target_codes:
        if code not in prices:
            continue
        existing_value = int(holdings.get(code, {}).get("shares", 0)) * prices[code]
        if code in holdings and existing_value >= target_value * (1 - band):
            continue
        desired = target_value - existing_value
        if code not in holdings:
            desired *= float(signal["new_buy_budget_multiplier"])
        shares = round_lot_shares(min(cash, max(desired, 0)), prices[code])
        while shares:
            gross, fees = shares * prices[code], transaction_fees(shares * prices[code], "buy", execution_date, fee_multiplier)
            if gross + fees["total"] <= cash + 1e-9:
                break
            shares -= int(V5_RULES["lot_size"])
        if not shares:
            continue
        cash -= gross + fees["total"]
        old_shares = int(holdings.get(code, {}).get("shares", 0))
        old_cost = float(holdings.get(code, {}).get("entry_price", prices[code])) * old_shares
        holdings[code] = {"code": code, "shares": old_shares + shares,
                          "entry_price": (old_cost + gross) / (old_shares + shares)}
        operations.append({"date": execution_date, "side": "买入", "code": code,
                           "shares": shares, "price": prices[code], "gross": gross,
                           "net_cash": gross + fees["total"], "fees": fees, "reason": "V5 目标仓位"})
    nav = cash + sum(int(row["shares"]) * valuation_prices[code]
                     for code, row in holdings.items())
    fees_total = sum(float((op.get("fees") or {}).get("total", 0)) for op in operations)
    cumulative = list((previous or {}).get("cumulative_events", [])) + operations
    event = {
        "schema_version": 1, "event_type": "execution", "strategy_id": "v5",
        "strategy_version": "V5", "shadow": True, "period": period,
        "signal_date": signal["signal_date"], "execution_date": execution_date,
        "target_codes": target_codes, "execution_prices": prices, "operations": operations,
        "cumulative_events": cumulative, "holdings": list(holdings.values()),
        "cash": round(cash, 6), "fees": round(fees_total, 6), "nav": round(nav, 6),
        "risk_multiplier": signal["risk_multiplier"], "rebalance_band": signal["rebalance_band"],
        "v5_input_sha256": snapshot["content_sha256"],
    }
    event["content_sha256"] = _canonical_sha256(event)
    return event


def backtest_metrics(nav_series: Sequence[dict[str, Any]], initial_capital: float,
                     trade_count: int) -> dict[str, Any]:
    """汇总 V5 计划要求的成本后指标。"""
    if len(nav_series) < 2:
        raise ValueError("NAV 序列至少需要两个观测")
    values = [float(row["nav"]) for row in nav_series]
    elapsed = (date.fromisoformat(nav_series[-1]["date"]) -
               date.fromisoformat(nav_series[0]["date"])).days / 365.25
    cagr = (values[-1] / initial_capital) ** (1 / elapsed) - 1 if elapsed > 0 else 0.0
    peak, max_drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1 - value / peak)
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    sharpe = (statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
              if len(returns) > 1 and statistics.stdev(returns) > 0 else 0.0)

    monthly = {}
    for row in nav_series:
        monthly[str(row["date"])[:7]] = row
    months = list(monthly.values())
    def rolling_worst(window: int) -> float | None:
        results = []
        for index in range(window, len(months)):
            start_month = date.fromisoformat(str(months[index - window]["date"])[:10])
            end_month = date.fromisoformat(str(months[index]["date"])[:10])
            if (end_month.year * 12 + end_month.month -
                    (start_month.year * 12 + start_month.month)) != window:
                continue
            start, end = float(months[index - window]["nav"]), float(months[index]["nav"])
            results.append((end / start) ** (12 / window) - 1)
        return min(results) if results else None
    oos = {}
    for start_year in (2021, 2023):
        subset = [row for row in nav_series if row["date"] >= f"{start_year}-01-01"]
        if len(subset) > 1:
            years = (date.fromisoformat(subset[-1]["date"]) - date.fromisoformat(subset[0]["date"])).days / 365.25
            oos[str(start_year)] = (float(subset[-1]["nav"]) / float(subset[0]["nav"])) ** (1 / years) - 1
    return {"cagr": cagr, "max_drawdown": max_drawdown, "sharpe": sharpe,
            "trade_count": trade_count, "rolling_36m_worst_cagr": rolling_worst(36),
            "rolling_48m_worst_cagr": rolling_worst(48), "continuous_oos_cagr": oos}


def run_frozen_backtest(v5_input_path: Path, rebalance_dates: Sequence[str],
                        cache_dir: Path, initial_capital: float = 1_000_000.0,
                        fee_multiplier: float = 1.0) -> dict[str, Any]:
    """用前向同一引擎逐月执行，并按缓存交易日生成日频 NAV。"""
    source = _load_v5_inputs(Path(v5_input_path))
    inputs = source["inputs"]
    codes = sorted({str(row["code"]) for row in inputs["adjustment_factors"]})
    ordered_dates = sorted(set(rebalance_dates))
    prior_index_days = [row["date"] for row in inputs["h00922"]
                        if row["date"] <= ordered_dates[0]]
    if len(prior_index_days) < 51:
        raise ValueError("首个信号日前 H00922 不足 51 个交易日，无法启动风险序列")
    seed_date = prior_index_days[-51]
    seed = {"event_type": "execution", "strategy_id": "v5", "period": "seed",
            "execution_date": seed_date, "holdings": [], "cash": initial_capital,
            "nav": initial_capital, "risk_multiplier": None, "cumulative_events": []}
    journal: list[dict[str, Any]] = [seed]
    nav_series: list[dict[str, Any]] = [{"date": seed_date, "nav": initial_capital}]
    with tempfile.TemporaryDirectory(prefix="v5_backtest_") as temp_name:
        temp = Path(temp_name)
        # 成交价仍读取现有不复权缓存，避免以前复权价假装成交。
        for code in codes:
            source_path = Path(cache_dir) / f"kl_{code}.json"
            if source_path.exists():
                (temp / source_path.name).write_bytes(source_path.read_bytes())
            dividend_path = Path(cache_dir) / f"dvd_{code}.json"
            if dividend_path.exists():
                (temp / dividend_path.name).write_bytes(dividend_path.read_bytes())
        index_calendar = sorted({row["date"] for row in inputs["h00922"]})
        for signal_date in ordered_dates:
            advanced_state = None
            accrued_operations: list[dict[str, Any]] = []
            previous_execution = next((row for row in reversed(journal)
                                       if row.get("event_type") == "execution"), None)
            if previous_execution:
                last_day = nav_series[-1]["date"] if nav_series else previous_execution["execution_date"]
                holdings = {row["code"]: row for row in previous_execution["holdings"]}
                daily_cash = float(previous_execution["cash"])
                calendar = [day for day in index_calendar if last_day < day <= signal_date]
                for day in calendar:
                    # 首个信号前只建立平坦 NAV 风险窗口；附件没有给出 2015 年现金利率。
                    interest = (0.0 if day < ordered_dates[0]
                                else cash_interest(daily_cash, date.fromisoformat(day).year))
                    daily_cash += interest
                    if interest:
                        accrued_operations.append({"date": day, "side": "利息", "gross": interest,
                                                   "net_cash": interest, "reason": "回测逐交易日现金计息"})
                    for code, holding in list(holdings.items()):
                        dividend_path = temp / f"dvd_{code}.json"
                        details = json.loads(dividend_path.read_text(encoding="utf-8")) if dividend_path.exists() else []
                        for item in details:
                            if str(item.get("ex_date", ""))[:10] != day:
                                continue
                            shares_before = int(holding["shares"])
                            gross = shares_before * float(item.get("dps") or 0)
                            if gross:
                                daily_cash += gross
                                accrued_operations.append({"date": day, "ex_date": day, "side": "分红",
                                                           "code": code, "shares": shares_before,
                                                           "gross": gross, "net_cash": gross,
                                                           "fees": {"total": 0.0}, "reason": "现金分红"})
                            ratio = float(item.get("bonus_ratio") or 0) + float(item.get("transfer_ratio") or 0)
                            if ratio > 0:
                                new_shares = shares_before + int(shares_before * ratio / 10)
                                holding["shares"] = new_shares
                                accrued_operations.append({"date": day, "ex_date": day, "side": "送转",
                                                           "code": code, "shares": new_shares,
                                                           "gross": 0.0, "net_cash": 0.0,
                                                           "fees": {"total": 0.0}, "reason": "送转股本调整"})
                    market_value = 0.0
                    for code, holding in holdings.items():
                        prices = json.loads((temp / f"kl_{code}.json").read_text(encoding="utf-8"))
                        available = [value for price_day, value in prices.items() if price_day <= day]
                        if not available:
                            raise ValueError(f"{code} 在 {day} 前没有估值价格")
                        market_value += int(holding["shares"]) * float(available[-1])
                    nav_series.append({"date": day, "nav": daily_cash + market_value})
                advanced_state = {**previous_execution, "execution_date": signal_date,
                                  "cash": daily_cash, "holdings": list(holdings.values()),
                                  "nav": nav_series[-1]["nav"],
                                  "cumulative_events": list(previous_execution.get("cumulative_events", []))
                                                       + accrued_operations}
            sliced = {name: [dict(row) for row in rows
                             if str(row.get("published_date") or row.get("date") or "")[:10] <= signal_date]
                      for name, rows in inputs.items()}
            sliced["strategy_nav"] = [dict(row) for row in nav_series if row["date"] <= signal_date]
            artifact = {"schema_version": 1, "strategy": "v5", "as_of": signal_date,
                        "price_format": "sina_qfq_factors_with_unadjusted_cache",
                        "attachments": source.get("attachments", []),
                        "inputs": sliced,
                        "hashes": {name: _canonical_sha256(rows) for name, rows in sliced.items()}}
            artifact["content_sha256"] = _canonical_sha256(artifact)
            input_path, manifest_path, dates_path = temp / "input.json", temp / "manifest.json", temp / "dates.json"
            input_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text(json.dumps({"as_of": signal_date, "codes": codes}), encoding="utf-8")
            dates_path.write_text(json.dumps({"dates": [signal_date]}), encoding="utf-8")
            signal = build_forward_signal(signal_date, manifest_path, dates_path, temp, journal, input_path)
            signal["fee_multiplier"] = fee_multiplier
            journal.append(signal)
            future_dates = sorted({day for code in codes for day in
                                   (json.loads((temp / f"kl_{code}.json").read_text(encoding="utf-8"))
                                    if (temp / f"kl_{code}.json").exists() else {}) if day > signal_date})
            if not future_dates:
                journal.pop()
                break
            execution_journal = list(journal)
            if advanced_state is not None:
                execution_journal = [row for row in execution_journal if row is not previous_execution]
                execution_journal.insert(-1, advanced_state)
            execution = build_forward_execution(signal_date[:7], future_dates[0], temp,
                                                execution_journal, input_path)
            if accrued_operations:
                execution["operations"] = accrued_operations + execution["operations"]
                execution["cumulative_events"] = list(previous_execution.get("cumulative_events", [])) \
                    + execution["operations"]
            journal.append(execution)
            nav_series.append({"date": execution["execution_date"], "nav": execution["nav"]})
    trade_count = sum(op.get("side") in {"买入", "卖出"} for row in journal
                      for op in row.get("operations", []))
    result = {"schema_version": 1, "strategy": "v5", "initial_capital": initial_capital,
            "inputs": {"v5_content_sha256": source["content_sha256"]},
            "metrics": backtest_metrics(nav_series, initial_capital, trade_count),
            "nav_series": nav_series, "events": journal[1:],
            "limitations": ["冻结候选集合可能缺少退市股票，存在生存者偏差"]}
    if fee_multiplier == 1.0:
        result["high_cost_metrics"] = run_frozen_backtest(
            v5_input_path, rebalance_dates, cache_dir, initial_capital, 3.0
        )["metrics"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="V5 冻结输入回测")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/backtest_cache"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    args = parser.parse_args()
    dates_payload = json.loads(args.dates.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload)
    result = run_frozen_backtest(args.input, dates, args.cache_dir, args.initial_capital)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
