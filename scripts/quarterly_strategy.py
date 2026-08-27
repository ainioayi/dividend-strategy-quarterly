"""季度调仓与低买高卖现金账本。

本模块只接受已经按日期落盘的快照，负责把筛选结果转换成可执行的
100 股整数手交易。它不生成历史价格，也不把当前横截面倒推成十年收益。
历史数据库补齐后，可以复用同一组 ``entry_signal``、``evaluate_holding``
和 ``rebalance_quarter`` 函数做逐季走步回放。
"""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Iterable


DEFAULT_QUARTERLY_RULES: dict[str, float | int | str] = {
    "initial_capital": 100000.0,
    "frequency": "quarterly",
    # 低买：必须先通过可持续性/分红质量硬门槛，再给估值留安全边际。
    "entry_yield": 6.8,
    "entry_pr": 1.0,
    # 持有区间略宽于买入区间，避免季度边界来回打脸。
    "hold_yield": 6.0,
    # 亏损仓使用更低的退出阈值：价格下跌时收益率通常升高，
    # 仅在分红大幅削减导致收益率跌破此线时才卖出亏损仓。
    "loss_hold_yield": 5.5,
    "hold_pr": 1.2,
    # 高卖：软触发连续两个季度才执行，硬风险立即退出。
    "exit_yield": 5.3,
    "exit_pr": 1.5,
    "exit_confirm_quarters": 1,
    "max_holdings": 2,
    "max_sector": 2,
    "max_banks": 2,
    # 1.0 表示不额外限制；历史现金引擎可用 0.5/0.2 做仓位敏感性分析。
    "max_position_pct": 1.0,
    "lot_size": 100,
    # 分红再投资：累计现金超过保留额后按持仓比例补买整数手，月度调仓下复利效果显著。
    "reinvest_dividends": True,
    "reinvest_cash_reserve": 0.0,
    # 等权再平衡：持仓市值超过目标权重 rebalance_threshold 倍时削减并补到低配持仓。
    "rebalance_threshold": 2.0,
    # 止损：持仓浮亏超过此百分比时强制卖出，防止高息陷原股造成过大亏损。
    "stop_loss_pct": 0.0,
    # 当前 A 股保守可解释费用：佣金最低 5 元；卖出另计印花税。
    "buy_commission_rate": 0.0003,
    "sell_commission_rate": 0.0003,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "min_commission": 5.0,
    # 候选池筛选：动态模式下，每个调仓日按截至当时已知的分红历史筛选候选池。
    # pool_mode="dynamic" 启用动态筛选，"curated" 使用固定候选池。
    "pool_mode": "curated",
    "pool_min_consecutive_years": 8,
    # 每年从哪个月份开始把上一年度视为已确认；默认 7 月。
    "pool_switch_month": 7,
    "momentum_months": 0,
    "momentum_threshold": 1.0,
    # 多周期动量平均：逗号分隔的回看月数列表（如 "3,4,5"）。
    # 设为空串则使用单一 momentum_months。非空时计算各周期动量比的几何均值再与 threshold 比较，
    # 平滑单一回看期的参数敏感性。
    "momentum_periods": "",
    # 选股排序依据："yield" 按收益率降序（默认），"momentum" 按动量比率降序。
    "rank_by": "yield",
    # 收益率上限：排除因价格暴跌导致 DPS/Price 虚高的困境股。
    "max_yield": 999.0,
}


def merged_rules(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    active = dict(DEFAULT_QUARTERLY_RULES)
    if rules:
        active.update(rules)
    return active


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sector(row: dict[str, Any]) -> str:
    value = str(row.get("sector") or row.get("industry") or "未知行业").strip()
    return value.split("-", 1)[0] or "未知行业"


def _is_bank(row: dict[str, Any]) -> bool:
    return bool(row.get("bank")) or "银行" in str(row.get("industry") or "")


def _yield(row: dict[str, Any]) -> float | None:
    return _number(row.get("yield", row.get("real_yield")))


def _pr(row: dict[str, Any]) -> float | None:
    return _number(row.get("pr"))


def _price(row: dict[str, Any]) -> float | None:
    return _number(row.get("price"))


def _execution_price(row: dict[str, Any]) -> float | None:
    """返回实际成交价；回测行显式带 ``execution_price=None`` 时不回退。

    实盘快照没有该字段，沿用 ``price``。回测则把信号价与下一交易日
    成交价分开，避免用生成信号的收盘价假设可以同时成交。
    """
    if "execution_price" in row:
        return _number(row.get("execution_price"))
    return _price(row)


def _dps(row: dict[str, Any]) -> float | None:
    return _number(row.get("dps"))


def hard_exit_reasons(row: dict[str, Any], rules: dict[str, Any] | None = None) -> list[str]:
    """返回不应等待确认的风险原因。

    缺少可选财务字段不会被擅自当成风险；缺少报价则由交易层标成数据缺口，
    不能在无法成交时假装卖出。
    """
    active = merged_rules(rules)
    reasons: list[str] = []
    if row.get("sustainability") not in (None, "可持续"):
        reasons.append("可持续性失效")
    payout = _number(row.get("payout_ratio"))
    if payout is not None and not (15.0 <= payout <= 85.0):
        reasons.append("支付率越过保守区间")
    years = row.get("dividend_years")
    if years is not None and _number(years) is not None and float(years) < 8:
        reasons.append("历史分红年份不足")
    coverage = _number(row.get("recent5_coverage"))
    if coverage is not None and coverage < 1.0:
        reasons.append("最近五年分红断档")
    if not _is_bank(row):
        ocf = _number(row.get("ocf_coverage"))
        if ocf is not None and ocf < 1.2:
            reasons.append("经营现金流覆盖不足")
    # 只有明确出现坏值才退出；None 留给数据缺口处理。
    y = _yield(row)
    p = _pr(row)
    if y is not None and y <= 0:
        reasons.append("股息率非正")
    if p is not None and p <= 0:
        reasons.append("PR 非正")
    return reasons


def entry_signal(row: dict[str, Any], rules: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """季度低买信号：硬门槛通过且估值/股息进入买入带。"""
    active = merged_rules(rules)
    reasons = list(row.get("gate_reasons") or [])
    if row.get("eligible") is False and not reasons:
        reasons.append("优化硬门槛未通过")
    if row.get("sustainability") not in (None, "可持续"):
        reasons.append("可持续性不是“可持续”")
    y = _yield(row)
    p = _pr(row)
    if y is None or y < float(active["entry_yield"]):
        reasons.append(f"真实股息率低于买入线 {float(active['entry_yield']):g}%")
    max_y = float(active.get("max_yield") or 999.0)
    if y is not None and max_y < 999.0 and y > max_y:
        reasons.append(f"真实股息率高于上限 {max_y:g}%")
    if p is None or p > float(active["entry_pr"]):
        reasons.append(f"PR 高于买入线 {float(active['entry_pr']):g}")
    if _price(row) is None or _price(row) <= 0:
        reasons.append("缺少可执行报价")
    return not reasons, reasons


def soft_exit_reasons(row: dict[str, Any], rules: dict[str, Any] | None = None) -> list[str]:
    """返回需要连续确认的估值/股息软卖出原因。"""
    active = merged_rules(rules)
    reasons: list[str] = []
    y = _yield(row)
    p = _pr(row)
    if y is not None and y < float(active["hold_yield"]):
        reasons.append(f"股息率低于持有线 {float(active['hold_yield']):g}%")
    if p is not None and p > float(active["hold_pr"]):
        reasons.append(f"PR 高于持有线 {float(active['hold_pr']):g}")
    persistence = _number(row.get("persistence_count"))
    minimum = _number(row.get("min_persistence"))
    if persistence is not None and minimum is not None and persistence < minimum:
        reasons.append("信号持续性不足")
    return reasons


def evaluate_holding(
    row: dict[str, Any] | None,
    soft_exit_streak: int = 0,
    rules: dict[str, Any] | None = None,
    entry_price: float | None = None,
) -> dict[str, Any]:
    """在一个季度检查点给出 hold/sell 结果，不读取未来数据。"""
    active = merged_rules(rules)
    if row is None:
        return {"action": "hold", "kind": "data_gap", "reasons": ["本期没有可核验快照"],
                "soft_exit_streak": soft_exit_streak}
    sustainability = row.get("sustainability")
    if sustainability in (None, "", "未评估", "数据缺失"):
        return {"action": "hold", "kind": "data_gap", "reasons": ["可持续性数据未完成核验"],
                "soft_exit_streak": soft_exit_streak}
    if _price(row) is None or float(_price(row) or 0.0) <= 0:
        return {"action": "hold", "kind": "data_gap", "reasons": ["缺少可执行报价"],
                "soft_exit_streak": soft_exit_streak}
    hard = hard_exit_reasons(row, active)
    if hard:
        return {"action": "sell", "kind": "hard", "reasons": hard,
                "soft_exit_streak": soft_exit_streak}
    # 亏损仓使用更低的退出阈值：避免在价格低谷因分红削减信号过早卖出
    eval_rules = active
    if entry_price is not None and entry_price > 0:
        cp = _price(row) or 0.0
        if cp > 0 and cp < float(entry_price):
            loss_y = active.get("loss_hold_yield")
            if loss_y is not None:
                eval_rules = dict(active)
                eval_rules["hold_yield"] = float(loss_y)
    soft = soft_exit_reasons(row, eval_rules)
    streak = soft_exit_streak + 1 if soft else 0
    if soft and streak >= int(active["exit_confirm_quarters"]):
        return {"action": "sell", "kind": "soft_confirmed", "reasons": soft,
                "soft_exit_streak": streak}
    return {"action": "hold", "kind": "soft_pending" if soft else "normal",
            "reasons": soft, "soft_exit_streak": streak}


def transaction_fees(
    gross: float,
    side: str,
    code: str = "",
    rules: dict[str, Any] | None = None,
) -> dict[str, float]:
    """计算单边交易费用；不把费用隐藏在收益数字里。"""
    active = merged_rules(rules)
    gross = max(float(gross), 0.0)
    rate_key = "buy_commission_rate" if side == "buy" else "sell_commission_rate"
    commission = max(float(active["min_commission"]), gross * float(active[rate_key])) if gross else 0.0
    stamp = gross * float(active["stamp_duty_rate"]) if side == "sell" else 0.0
    transfer = gross * float(active["transfer_fee_rate"])
    return {
        "commission": commission,
        "stamp_duty": stamp,
        "transfer_fee": transfer,
        "total": commission + stamp + transfer,
    }


def screen_dynamic_pool(
    div_history_by_code: dict[str, list[dict[str, Any]]],
    as_of: str,
    min_consecutive_years: int = 8,
    dividend_details_by_code: dict[str, list[dict[str, Any]]] | None = None,
    pool_switch_month: int = 7,
) -> list[str]:
    """用截至 as_of 时已知的分红历史动态筛选候选池（无未来函数）。

    对每只股票，检查截至 as_of 前一年度起的连续分红年数是否达到
    min_consecutive_years。7月后使用 year-1 为最新确认年份，
    1-6月使用 year-2（当年分红尚未实施）。

    ``dividend_details_by_code`` 存在时，必须同时满足逐笔除权日不晚于
    ``as_of``；这比只看年度汇总更严格，可避免“已实施”记录在历史回放中
    把后来才除权的分红带入候选池。旧的年度汇总接口仍保留，便于离线兼容。
    """
    year = int(as_of[:4])
    month = int(as_of[5:7]) if len(as_of) >= 7 else 1
    try:
        switch = int(7 if pool_switch_month is None else pool_switch_month)
    except (TypeError, ValueError) as exc:
        raise ValueError("pool_switch_month 必须是 1-12 的整数") from exc
    if not 1 <= switch <= 12:
        raise ValueError("pool_switch_month 必须是 1-12 的整数")
    end_year = year - 1 if month >= switch else year - 2
    start_year = end_year - min_consecutive_years + 1
    pool: list[str] = []
    details_map = dividend_details_by_code or {}
    for code, dh in div_history_by_code.items():
        years_with_dps: set[int] = set()
        details = details_map.get(code)
        if details is not None:
            # 逐笔事件是严格时间边界；缺少除权日的记录不能默认为已知。
            for item in details:
                try:
                    y = int(item.get("year"))
                except (TypeError, ValueError):
                    continue
                ex_date = str(item.get("ex_date") or item.get("ex_dividend_date") or "")[:10]
                dps = _number(item.get("dps") or item.get("cash_div_per_share"))
                if (dps is not None and dps > 0 and len(ex_date) == 10
                        and ex_date <= as_of and start_year <= y <= end_year):
                    years_with_dps.add(y)
        else:
            for item in dh:
                try:
                    y = int(item.get("year"))
                except (TypeError, ValueError):
                    continue
                dps = item.get("dps", 0)
                try:
                    positive_dps = float(dps) > 0
                except (TypeError, ValueError):
                    positive_dps = False
                if positive_dps and start_year <= y <= end_year:
                    years_with_dps.add(y)
        # “连续 N 年”要求窗口内每一年都有已知正分红，不能只按数量计数。
        required_years = set(range(start_year, end_year + 1))
        if required_years.issubset(years_with_dps):
            pool.append(str(code).zfill(6))
    return sorted(pool)


def select_entry_candidates(
    rows: Iterable[dict[str, Any]],
    rules: dict[str, Any] | None = None,
    excluded: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按真实股息率优先、行业/银行上限选出季度买入池。"""
    active = merged_rules(rules)
    excluded = excluded or set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "").zfill(6)
        ok, _ = entry_signal(row, active)
        if ok and code not in excluded:
            candidates.append(row)
    rank_by = str(active.get("rank_by") or "yield")
    if rank_by == "momentum":
        candidates.sort(key=lambda r: (-float(r.get("momentum_ratio") or 0), -float(_yield(r) or -1)))
    elif rank_by == "yield_vol":
        # 波动率调整排序：收益率/日频波动率，偏好高息低波
        candidates.sort(key=lambda r: (
            -(float(_yield(r) or 0) / max(float(r.get("volatility") or 0.01), 0.01)),
            -float(_yield(r) or -1),
        ))
    else:
        candidates.sort(key=lambda r: (-float(_yield(r) or -1), -float(r.get("quality_score") or 0), float(_pr(r) or 99)))
    selected: list[dict[str, Any]] = []
    sector_count: dict[str, int] = {}
    bank_count = 0
    for row in candidates:
        sector = _sector(row)
        bank = _is_bank(row)
        if sector_count.get(sector, 0) >= int(active["max_sector"]):
            continue
        if bank and bank_count >= int(active["max_banks"]):
            continue
        selected.append(row)
        sector_count[sector] = sector_count.get(sector, 0) + 1
        bank_count += int(bank)
        if len(selected) >= int(active["max_holdings"]):
            break
    return selected


def momentum_filter(
    rows, held_codes, price_lookup, rb_date, rebalance_dates, rules=None,
):
    """对新入场候选应用动量过滤，已持仓股票始终保留（无未来函数）。"""
    active = merged_rules(rules)
    mm = int(active.get("momentum_months") or 0)
    mt = float(active.get("momentum_threshold") or 1.0)
    periods_str = str(active.get("momentum_periods") or "").strip()
    periods = []
    if periods_str:
        periods = [int(x) for x in periods_str.split(",") if x.strip()]
    if mm <= 0 and not periods:
        return list(rows)
    entry_y = float(active["entry_yield"])
    try:
        idx = rebalance_dates.index(rb_date)
    except ValueError:
        return list(rows)
    # 多周期模式必须同时具备最长回看期；只用较短周期会让缺失历史的
    # 股票悄悄通过，导致不同代码之间的信号口径不一致。
    max_period = max(periods) if periods else mm
    if idx < max_period:
        return list(rows)
    past_dates = {}
    if periods:
        for p in periods:
            past_dates[p] = rebalance_dates[idx - p]
    else:
        past_dates[mm] = rebalance_dates[idx - mm]
    filtered = []
    for row in rows:
        code = str(row.get("code") or "").zfill(6)
        y = _yield(row) or 0
        if code in held_codes and float(active.get("momentum_exit_threshold") or 0.0) <= 0:
            filtered.append(row)
            continue
        if y < entry_y and code not in held_codes:
            filtered.append(row)
            continue
        curr_p = _price(row) or 0
        if curr_p <= 0:
            continue
        # 计算各周期动量比的几何均值
        ratios = []
        for p, pd in past_dates.items():
            pp = price_lookup(code, pd) or 0
            if pp > 0:
                ratios.append(curr_p / pp)
        # 任一回看期缺少价格时不生成动量信号，避免用部分历史替代完整规则。
        ratio_ok = len(ratios) == (len(periods) if periods else 1) and ratios
        ratio = math.prod(ratios) ** (1.0 / len(ratios)) if ratio_ok else None
        if code in held_codes and float(active.get("momentum_exit_threshold") or 0.0) > 0:
            row = dict(row)
            row["momentum_ratio"] = ratio
            filtered.append(row)
        elif ratio_ok and ratio >= mt:
            row = dict(row)
            row["momentum_ratio"] = ratio
            filtered.append(row)
    return filtered


def _buy(
    cash: float,
    row: dict[str, Any],
    shares: int,
    date: str,
    rules: dict[str, Any],
) -> tuple[float, dict[str, Any] | None, dict[str, Any] | None]:
    price = _execution_price(row)
    lot = int(rules["lot_size"])
    if price is None or price <= 0 or shares < lot or shares % lot:
        return cash, None, {"reason": "报价或整数手无效", "code": row.get("code")}
    gross = price * shares
    fees = transaction_fees(gross, "buy", str(row.get("code") or ""), rules)
    total = gross + fees["total"]
    if total > cash + 1e-8:
        return cash, None, {"reason": "现金不足", "code": row.get("code"), "required": total}
    code = str(row.get("code") or "").zfill(6)
    holding = {
        "code": code,
        "name": row.get("name") or code,
        "shares": int(shares),
        "entry_price": price,
        "entry_date": date,
        "entry_pr": _pr(row),
        "entry_yield": _yield(row),
        "soft_exit_streak": 0,
        "sector": _sector(row),
        "bank": _is_bank(row),
        "dps": _dps(row),
    }
    event = {
        "date": date,
        "side": "买入",
        "code": code,
        "name": holding["name"],
        "shares": int(shares),
        "price": price,
        "gross": gross,
        "fees": fees,
        "net_cash": total,
        "reason": "低买：可持续 + 股息率/PR 进入买入带",
    }
    return cash - total, holding, event


def _sell(
    cash: float,
    holding: dict[str, Any],
    row: dict[str, Any],
    date: str,
    reason: str,
    rules: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    price = _execution_price(row)
    if price is None:
        price = float(holding.get("entry_price") or 0.0)
    shares = int(holding.get("shares") or 0)
    gross = max(price * shares, 0.0)
    fees = transaction_fees(gross, "sell", str(holding.get("code") or ""), rules)
    proceeds = gross - fees["total"]
    event = {
        "date": date,
        "side": "卖出",
        "code": holding.get("code"),
        "name": holding.get("name"),
        "shares": shares,
        "price": price,
        "gross": gross,
        "fees": fees,
        "net_cash": proceeds,
        "reason": "高卖/风控：" + reason,
    }
    return cash + proceeds, event


def _market_value(holdings: dict[str, dict[str, Any]], rows_by_code: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for code, holding in holdings.items():
        row = rows_by_code.get(code)
        # 交易必须使用当日精确成交价；若停牌无法成交，估值仍可使用
        # 截止日之前最近的标记价，最后才回退到入场价并由上层审计。
        price = _execution_price(row or {})
        if price is None:
            price = _number((row or {}).get("mark_price"))
        price = price or float(holding.get("entry_price") or 0.0)
        total += price * int(holding.get("shares") or 0)
    return total


def build_initial_ledger(
    rows: Iterable[dict[str, Any]],
    initial_capital: float = 100000.0,
    as_of: str = "",
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用当前快照做一次真实整数手建仓，返回现金账本而非虚构未来收益。"""
    active = merged_rules(rules)
    active["initial_capital"] = float(initial_capital)
    source_rows = list(rows)
    candidates = select_entry_candidates(source_rows, active)
    cash = float(initial_capital)
    holdings: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not candidates:
        return {"as_of": as_of, "initial_capital": initial_capital, "cash": cash,
                "holdings": {}, "events": [], "skipped": [], "candidates": []}

    cap_value = active.get("max_position_pct")
    cap = float(1.0 if cap_value is None else cap_value)
    if not 0.0 < cap <= 1.0:
        raise ValueError("max_position_pct 必须在 (0, 1] 内")
    target = min(float(initial_capital) / len(candidates), float(initial_capital) * cap)
    lot = int(active["lot_size"])
    # 先按目标等权建仓，确保每个通过约束的候选都有机会进入账本。
    for row in candidates:
        price = _execution_price(row)
        if price is None or price <= 0:
            skipped.append({"code": row.get("code"), "name": row.get("name"), "reason": "缺少报价"})
            continue
        shares = max(lot, int(target // (price * lot)) * lot)
        while shares >= lot:
            next_cash, holding, event_or_error = _buy(cash, row, shares, as_of, active)
            if holding is not None:
                cash = next_cash
                holdings[holding["code"]] = holding
                events.append(event_or_error)
                break
            shares -= lot
        else:
            skipped.append({"code": row.get("code"), "name": row.get("name"), "reason": "现金不足"})

    # 用剩余现金给最轻仓且不超过目标 110% 的持仓补一手，减少整数手偏差。
    while True:
        possible = []
        for row in candidates:
            code = str(row.get("code") or "").zfill(6)
            holding = holdings.get(code)
            if not holding:
                continue
            price = _execution_price(row)
            if price is None:
                continue
            value = price * int(holding["shares"])
            if value + price * lot <= target * 1.10:
                possible.append((value, row))
        possible.sort(key=lambda x: x[0])
        added = False
        for _, row in possible:
            code = str(row.get("code") or "").zfill(6)
            next_cash, holding, event_or_error = _buy(cash, row, lot, as_of, active)
            if holding is not None:
                cash = next_cash
                old = holdings[code]
                old["shares"] += holding["shares"]
                events.append(event_or_error)
                added = True
                break
        if not added:
            break

    rows_by_code = {str(r.get("code") or "").zfill(6): r for r in source_rows}
    market_value = _market_value(holdings, rows_by_code)
    fees = sum(float((e.get("fees") or {}).get("total") or 0.0) for e in events)
    return {
        "as_of": as_of,
        "initial_capital": float(initial_capital),
        "cash": round(cash, 2),
        "holdings": holdings,
        "events": events,
        "skipped": skipped,
        "candidates": [str(r.get("code") or "").zfill(6) for r in candidates],
        "market_value": round(market_value, 2),
        "nav": round(cash + market_value, 2),
        "fees": round(fees, 2),
        "rows_by_code": rows_by_code,
    }


def reinvest_cash(
    state: dict[str, Any],
    rows_by_code: dict[str, dict[str, Any]],
    date: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """用超过现金保留额的资金按持仓比例追加整数手。"""
    active = merged_rules(rules)
    if not active.get("reinvest_dividends"):
        return state
    reserve = float(active.get("reinvest_cash_reserve") or 0.0)
    if reserve < 0:
        raise ValueError("reinvest_cash_reserve 不能为负数")
    cash = float(state.get("cash") or 0.0)
    if cash <= reserve:
        return state
    holdings = state.get("holdings") or {}
    if not holdings:
        return state
    lot = int(active["lot_size"])
    available = cash - reserve
    values = {}
    total_mv = 0.0
    for code, holding in holdings.items():
        row = rows_by_code.get(code) or {}
        price = _execution_price(row) or float(holding.get("entry_price") or 0.0)
        mv = price * int(holding.get("shares") or 0)
        values[code] = mv
        total_mv += mv
    if total_mv <= 0:
        return state
    events = list(state.get("events") or [])
    cap_value = active.get("max_position_pct")
    cap = float(1.0 if cap_value is None else cap_value)
    if not 0.0 < cap <= 1.0:
        raise ValueError("max_position_pct 必须在 (0, 1] 内")
    # 仓位上限按本期已知组合净值计算；若按初始本金封顶，组合上涨后
    # 分红现金会长期闲置，且 ``1.0`` 会错误地变成固定金额上限。
    portfolio_nav = cash + total_mv
    for code, holding in list(holdings.items()):
        row = rows_by_code.get(code) or {}
        price = _execution_price(row)
        if not price or price <= 0:
            continue
        current_value = price * int(holding.get("shares") or 0)
        max_value = portfolio_nav * cap
        if current_value >= max_value:
            continue
        alloc = min(
            available * (values.get(code, 0) / total_mv),
            max_value - current_value,
        )
        shares = int(alloc // (price * lot)) * lot
        if shares < lot:
            continue
        gross = price * shares
        fees = transaction_fees(gross, "buy", code, active)
        total_cost = gross + fees["total"]
        # 保留额是交易完成后的硬下限，手续费也必须计入预算。
        budget = max(cash - reserve, 0.0)
        while shares >= lot and total_cost > budget + 1e-8:
            shares -= lot
            if shares < lot:
                break
            gross = price * shares
            fees = transaction_fees(gross, "buy", code, active)
            total_cost = gross + fees["total"]
        if total_cost > budget + 1e-8 or shares < lot:
            continue
        cash -= total_cost
        holding["shares"] = int(holding.get("shares") or 0) + shares
        events.append({
            "date": date, "side": "买入", "code": code,
            "shares": shares, "price": price, "gross": gross,
            "fees": fees, "net_cash": total_cost, "reason": "分红现金再投资",
        })
    state["cash"] = round(cash, 2)
    state["events"] = events
    return state


def rebalance_equally(
    state: dict[str, Any],
    rows_by_code: dict[str, dict[str, Any]],
    as_of: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """按等权目标削减超配持仓，并向低配持仓补仓。"""
    active = merged_rules(rules)
    threshold = float(active.get("rebalance_threshold") or 999.0)
    if threshold >= 999:
        return state
    holdings = state.get("holdings") or {}
    if len(holdings) < 2:
        return state
    cash = float(state.get("cash") or 0.0)
    reserve = float(active.get("reinvest_cash_reserve") or 0.0)
    if reserve < 0:
        raise ValueError("reinvest_cash_reserve 不能为负数")
    events = list(state.get("events") or [])
    lot = int(active.get("lot_size") or 100)
    max_h = int(active.get("max_holdings") or 1)
    total_mv = _market_value(holdings, rows_by_code) + cash
    target_per = total_mv / max_h
    for code, h in list(holdings.items()):
        row = rows_by_code.get(code)
        if not row:
            continue
        price = _execution_price(row)
        if not price or price <= 0:
            continue
        cv = price * int(h.get("shares") or 0)
        if cv > target_per * threshold:
            excess = cv - target_per
            sts = int(excess // (price * lot)) * lot
            if sts >= lot:
                gross = price * sts
                fees = transaction_fees(gross, "sell", code, active)
                cash += gross - fees["total"]
                h["shares"] = int(h.get("shares") or 0) - sts
                events.append({
                    "date": as_of, "side": "卖出", "code": code,
                    "shares": sts, "price": price, "gross": gross,
                    "fees": fees, "net_cash": gross - fees["total"],
                    "reason": "等权再平衡减仓",
                })
    state["cash"] = round(cash, 2)
    state["events"] = events
    if cash > reserve and holdings:
        for code, h in list(holdings.items()):
            row = rows_by_code.get(code)
            if not row:
                continue
            price = _execution_price(row)
            if not price or price <= 0:
                continue
            cv = price * int(h.get("shares") or 0)
            if cv < target_per * 0.8:
                shortfall = target_per - cv
                shares = int(shortfall // (price * lot)) * lot
                if shares < lot:
                    continue
                gross = price * shares
                fees = transaction_fees(gross, "buy", code, active)
                total_cost = gross + fees["total"]
                budget = max(cash - reserve, 0.0)
                if total_cost > budget:
                    shares -= lot
                    if shares < lot:
                        continue
                    gross = price * shares
                    fees = transaction_fees(gross, "buy", code, active)
                    total_cost = gross + fees["total"]
                if total_cost > max(cash - reserve, 0.0) or shares < lot:
                    continue
                cash -= total_cost
                h["shares"] = int(h.get("shares") or 0) + shares
                events.append({
                    "date": as_of, "side": "买入", "code": code,
                    "shares": shares, "price": price, "gross": gross,
                    "fees": fees, "net_cash": total_cost,
                    "reason": "等权再平衡补仓",
                })
        state["cash"] = round(cash, 2)
    return state


def rebalance_quarter(
    state: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    as_of: str,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在一个季度检查点处理卖出确认、候选补位和现金更新。"""
    active = merged_rules(rules)
    rows_by_code = {str(r.get("code") or "").zfill(6): r for r in rows}
    holdings = deepcopy(state.get("holdings") or {})
    cash = float(state.get("cash") or 0.0)
    events = list(state.get("events") or [])
    actions: list[dict[str, Any]] = []

    for code in list(holdings):
        holding = holdings[code]
        # 可选的历史动量退出：momentum_ratio 必须由信号日回看价格预先写入快照。
        mom_exit = float(active.get("momentum_exit_threshold") or 0.0)
        if mom_exit > 0 and rows_by_code.get(code) is not None:
            mr = rows_by_code[code].get("momentum_ratio")
            streak = int(holding.get("momentum_exit_streak") or 0)
            streak = streak + 1 if mr is not None and float(mr) < mom_exit else 0
            holding["momentum_exit_streak"] = streak
            confirm = max(1, int(active.get("momentum_exit_confirm_count") or 1))
            if streak >= confirm:
                current_row = rows_by_code[code]
                if _execution_price(current_row) is not None and float(_execution_price(current_row) or 0) > 0:
                    cash, event = _sell(cash, holding, current_row, as_of,
                                        "动量跌破退出线 %.3f" % mom_exit, active)
                    events.append(event)
                    actions.append({"code": code, "action": "sell", "kind": "momentum_exit",
                                    "reasons": ["动量比率 %.4f < %.4f" % (float(mr), mom_exit)]})
                    del holdings[code]
                    continue
        decision = evaluate_holding(
            rows_by_code.get(code),
            int(holding.get("soft_exit_streak") or 0),
            active,
            entry_price=float(holding.get("entry_price") or 0),
        )

        # 检查可选止损线。
        sl_pct = float(active.get("stop_loss_pct") or 0.0)
        if (sl_pct > 0 and rows_by_code.get(code) is not None
                and _execution_price(rows_by_code[code]) is not None):
            ep = float(holding.get("entry_price") or 0.0)
            cp = _price(rows_by_code[code]) or 0.0
            if ep > 0 and cp > 0:
                loss = (ep - cp) / ep * 100.0
                if loss >= sl_pct:
                    cash, ev = _sell(cash, holding, rows_by_code[code], as_of, "止损线 %.1f%%" % loss, active)
                    events.append(ev)
                    actions.append({"code": code, "action": "sell", "kind": "stop_loss", "reasons": ["亏损 %.1f%%" % loss]})
                    del holdings[code]
                    continue

        holding["soft_exit_streak"] = decision["soft_exit_streak"]
        actions.append({"code": code, "action": decision["action"], "kind": decision["kind"], "reasons": decision["reasons"]})
        if decision["action"] == "sell" and rows_by_code.get(code) is not None:
            current_row = rows_by_code[code]
            if _execution_price(current_row) is None or float(_execution_price(current_row) or 0.0) <= 0:
                actions[-1] = {"code": code, "action": "hold", "kind": "data_gap",
                               "reasons": ["卖出信号已触发，但缺少可执行报价"]}
                continue
            cash, event = _sell(cash, holding, current_row, as_of, "；".join(decision["reasons"]), active)
            events.append(event)
            del holdings[code]

    # 买入新标的前，先把超过保留额的分红现金投入已有持仓。
    ri_state = {"cash": cash, "holdings": holdings, "events": events,
                "initial_capital": float(state.get("initial_capital") or active["initial_capital"])}
    ri_state = reinvest_cash(ri_state, rows_by_code, as_of, active)
    cash = float(ri_state.get("cash") or cash)
    holdings = ri_state.get("holdings") or holdings
    events = ri_state.get("events") or events

    # 卖出后按同样的行业上限补位；既有持仓不会因短期排名变化被强制换手。
    candidates = select_entry_candidates(rows_by_code.values(), active, excluded=set(holdings))
    sector_count: dict[str, int] = {}
    bank_count = 0
    for holding in holdings.values():
        sector_count[holding.get("sector") or "未知行业"] = sector_count.get(holding.get("sector") or "未知行业", 0) + 1
        bank_count += int(bool(holding.get("bank")))
    for row in candidates:
        if len(holdings) >= int(active["max_holdings"]):
            break
        sector = _sector(row)
        bank = _is_bank(row)
        if sector_count.get(sector, 0) >= int(active["max_sector"]):
            continue
        if bank and bank_count >= int(active["max_banks"]):
            continue
        price = _execution_price(row)
        if price is None:
            continue
        cap_value = active.get("max_position_pct")
        cap = float(1.0 if cap_value is None else cap_value)
        if not 0.0 < cap <= 1.0:
            raise ValueError("max_position_pct 必须在 (0, 1] 内")
        target = max(
            min(float(state.get("initial_capital") or 0.0) / int(active["max_holdings"]),
                float(state.get("initial_capital") or 0.0) * cap),
            price * int(active["lot_size"]),
        )
        shares = max(int(active["lot_size"]), int(target // (price * int(active["lot_size"]))) * int(active["lot_size"]))
        while shares >= int(active["lot_size"]):
            next_cash, holding, event_or_error = _buy(cash, row, shares, as_of, active)
            if holding is not None:
                cash = next_cash
                holdings[holding["code"]] = holding
                events.append(event_or_error)
                sector_count[sector] = sector_count.get(sector, 0) + 1
                bank_count += int(bank)
                actions.append({"code": holding["code"], "action": "buy", "kind": "replacement", "reasons": ["季度调仓补位"]})
                break
            shares -= int(active["lot_size"])

    market_value = _market_value(holdings, rows_by_code)
    next_state = {
        "as_of": as_of,
        "initial_capital": float(state.get("initial_capital") or active["initial_capital"]),
        "cash": round(cash, 2),
        "holdings": holdings,
        "events": events,
        "actions": actions,
        "market_value": round(market_value, 2),
        "nav": round(cash + market_value, 2),
        "rows_by_code": rows_by_code,
    }
    return next_state


def trigger_prices(row: dict[str, Any], rules: dict[str, Any] | None = None) -> dict[str, float | None]:
    """按当前 PR/分红不变推导理论触发价，仅用于审计，不是价格预测。"""
    active = merged_rules(rules)
    price = _price(row)
    pr = _pr(row)
    dps = _dps(row)
    entry_pr_price = price * float(active["entry_pr"]) / pr if price and pr and pr > 0 else None
    exit_pr_price = price * float(active["exit_pr"]) / pr if price and pr and pr > 0 else None
    entry_yield_price = dps / (float(active["entry_yield"]) / 100.0) if dps and dps > 0 else None
    exit_yield_price = dps / (float(active["exit_yield"]) / 100.0) if dps and dps > 0 else None
    exits = [v for v in (exit_pr_price, exit_yield_price) if v is not None]
    return {
        "entry_pr_price": entry_pr_price,
        "entry_yield_price": entry_yield_price,
        "exit_pr_price": exit_pr_price,
        "exit_yield_price": exit_yield_price,
        "first_exit_price": min(exits) if exits else None,
    }


def rebalance_rotation(
    state: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    as_of: str,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """持有股息率排名前 N 的标的，并主动轮换掉低排名持仓。"""
    active = merged_rules(rules)
    rows_by_code = {str(r.get("code") or "").zfill(6): r for r in rows}
    holdings = deepcopy(state.get("holdings") or {})
    cash = float(state.get("cash") or 0.0)
    events = list(state.get("events") or [])
    actions = []
    lot = int(active["lot_size"])
    max_h = int(active["max_holdings"])
    entry_y = float(active["entry_yield"])
    rotate_y = float(active.get("rotate_yield") or entry_y)

    candidates = sorted(
        [r for r in rows if _yield(r) is not None and _yield(r) >= entry_y and _price(r) is not None],
        key=lambda r: -float(_yield(r) or 0),
    )
    top_codes = set(str(r.get("code") or "").zfill(6) for r in candidates[:max_h])

    for code in list(holdings.keys()):
        holding = holdings[code]
        row = rows_by_code.get(code)
        if row is None:
            continue
        yld = _yield(row)
        in_top = code in top_codes
        if not in_top and (yld is None or yld < rotate_y):
            price = _execution_price(row)
            if price is None or price <= 0:
                actions.append({"code": code, "action": "hold", "kind": "data_gap", "reasons": ["缺少报价"]})
                continue
            cash, event = _sell(cash, holding, row, as_of, "轮换卖出", active)
            events.append(event)
            del holdings[code]
            actions.append({"code": code, "action": "sell", "kind": "rotation", "reasons": ["股息率 %.2f%%" % (yld or 0)]})

    initial = float(state.get("initial_capital") or active["initial_capital"])
    target_per = initial / max_h
    for row in candidates[:max_h]:
        code = str(row.get("code") or "").zfill(6)
        if code in holdings or len(holdings) >= max_h:
            continue
        price = _execution_price(row)
        if price is None or price <= 0:
            continue
        shares = max(lot, int(target_per // (price * lot)) * lot)
        while shares >= lot:
            next_cash, holding, _ = _buy(cash, row, shares, as_of, active)
            if holding is not None:
                cash = next_cash
                holdings[code] = holding
                actions.append({"code": code, "action": "buy", "kind": "rotation", "reasons": ["股息率排名靠前"]})
                break
            shares -= lot

    ri_state = {"cash": cash, "holdings": holdings, "events": events, "initial_capital": initial}
    ri_state = reinvest_cash(ri_state, rows_by_code, as_of, active)
    cash = float(ri_state.get("cash") or cash)
    holdings = ri_state.get("holdings") or holdings
    events = ri_state.get("events") or events

    market_value = _market_value(holdings, rows_by_code)
    return {
        "as_of": as_of, "initial_capital": initial,
        "cash": round(cash, 2), "holdings": holdings,
        "events": events, "actions": actions,
        "market_value": round(market_value, 2),
        "nav": round(cash + market_value, 2),
        "rows_by_code": rows_by_code,
    }


__all__ = [
    "DEFAULT_QUARTERLY_RULES",
    "build_initial_ledger",
    "entry_signal",
    "evaluate_holding",
    "hard_exit_reasons",
    "merged_rules",
    "momentum_filter",
    "rebalance_equally",
    "rebalance_quarter",
    "reinvest_cash",
    "select_entry_candidates",
    "screen_dynamic_pool",
    "soft_exit_reasons",
    "transaction_fees",
    "rebalance_rotation",
    "trigger_prices",
]
