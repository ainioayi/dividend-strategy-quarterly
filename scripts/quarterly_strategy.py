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
    "entry_yield": 5.0,
    "entry_pr": 0.85,
    # 持有区间略宽于买入区间，避免季度边界来回打脸。
    "hold_yield": 4.5,
    "hold_pr": 1.05,
    # 高卖：软触发连续两个季度才执行，硬风险立即退出。
    "exit_yield": 4.25,
    "exit_pr": 1.20,
    "exit_confirm_quarters": 2,
    "max_holdings": 10,
    "max_sector": 2,
    "max_banks": 2,
    # 1.0 表示不额外限制；历史现金引擎可用 0.5/0.2 做仓位敏感性分析。
    "max_position_pct": 1.0,
    "lot_size": 100,
    # 当前 A 股保守可解释费用：佣金最低 5 元；卖出另计印花税。
    "buy_commission_rate": 0.0003,
    "sell_commission_rate": 0.0003,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "min_commission": 5.0,
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
    soft = soft_exit_reasons(row, active)
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


def _buy(
    cash: float,
    row: dict[str, Any],
    shares: int,
    date: str,
    rules: dict[str, Any],
) -> tuple[float, dict[str, Any] | None, dict[str, Any] | None]:
    price = _price(row)
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
    price = _price(row) or float(holding.get("entry_price") or 0.0)
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
        price = _price(row or {}) or float(holding.get("entry_price") or 0.0)
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

    cap = float(active.get("max_position_pct") or 1.0)
    if not 0.0 < cap <= 1.0:
        raise ValueError("max_position_pct 必须在 (0, 1] 内")
    target = min(float(initial_capital) / len(candidates), float(initial_capital) * cap)
    lot = int(active["lot_size"])
    # 先按目标等权建仓，确保每个通过约束的候选都有机会进入账本。
    for row in candidates:
        price = _price(row)
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
            price = _price(row)
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
        decision = evaluate_holding(rows_by_code.get(code), int(holding.get("soft_exit_streak") or 0), active)
        holding["soft_exit_streak"] = decision["soft_exit_streak"]
        actions.append({"code": code, "action": decision["action"], "kind": decision["kind"], "reasons": decision["reasons"]})
        if decision["action"] == "sell" and rows_by_code.get(code) is not None:
            current_row = rows_by_code[code]
            if _price(current_row) is None or float(_price(current_row) or 0.0) <= 0:
                actions[-1] = {"code": code, "action": "hold", "kind": "data_gap",
                               "reasons": ["卖出信号已触发，但缺少可执行报价"]}
                continue
            cash, event = _sell(cash, holding, current_row, as_of, "；".join(decision["reasons"]), active)
            events.append(event)
            del holdings[code]

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
        price = _price(row)
        if price is None:
            continue
        cap = float(active.get("max_position_pct") or 1.0)
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


__all__ = [
    "DEFAULT_QUARTERLY_RULES",
    "build_initial_ledger",
    "entry_signal",
    "evaluate_holding",
    "hard_exit_reasons",
    "merged_rules",
    "rebalance_quarter",
    "select_entry_candidates",
    "soft_exit_reasons",
    "transaction_fees",
    "trigger_prices",
]
