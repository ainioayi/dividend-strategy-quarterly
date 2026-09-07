"""公开业绩发布前的账本、行情覆盖和指纹检查，不修改输入。"""
from __future__ import annotations

import math
import re
from datetime import date

from monthly_forward import FORWARD_STRATEGIES, _hash, build_metadata, strategy_profile


def required_price_dates(journal: list[dict], calendar: list[str]) -> dict[str, set[str]]:
    """只要求实际持仓期间的行情，卖出后的停牌或缺价不影响历史估值。"""
    by_strategy: dict[str, list[dict]] = {}
    for row in journal:
        if row.get("event_type") == "execution":
            by_strategy.setdefault(row.get("strategy_id") or "v1", []).append(row)
    required: dict[str, set[str]] = {}
    for executions in by_strategy.values():
        executions.sort(key=lambda row: row["execution_date"])
        for index, row in enumerate(executions):
            end = executions[index + 1]["execution_date"] if index + 1 < len(executions) else "9999-12-31"
            days = {day for day in calendar if row["execution_date"] <= day < end}
            for holding in row.get("holdings") or []:
                if float(holding.get("shares") or 0) > 0:
                    required.setdefault(str(holding["code"]).zfill(6), set()).update(days)
    return required


def _date(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"日期格式错误：{value}")
    return value


def _fingerprint(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} 缺少有效指纹")


def _verify_journal(key: str, rows: list[dict], as_of: str, previous: dict) -> None:
    profile = strategy_profile(key)
    expected_hash = (previous.get("audit", {}).get("strategy_journals") or {}).get(key)
    if expected_hash and not any(_hash(rows[:length]) == expected_hash for length in range(len(rows) + 1)):
        raise ValueError(f"{key} 账本与上次发布的历史前缀不一致")
    seen = set()
    signals = {}
    cumulative: list[dict] = []
    last_day = ""
    for row in rows:
        kind, period = row.get("event_type"), row.get("period")
        if kind not in ("signal", "execution") or (kind, period) in seen:
            raise ValueError(f"{key} 账本事件类型错误或重复")
        seen.add((kind, period))
        if row.get("strategy_id") != key or row.get("strategy_version") != profile["version"] or row.get("shadow") != profile["shadow"]:
            raise ValueError(f"{key} 账本策略身份不匹配")
        signal_day = _date(row.get("signal_date"))
        event_day = _date(row.get("execution_date") if kind == "execution" else signal_day)
        if signal_day[:7] != period or event_day > as_of or event_day < last_day:
            raise ValueError(f"{key} 账本日期越界或顺序错误")
        last_day = event_day
        if not profile.get("independent_rules") and row.get("rules_sha256") != _hash(profile["rules"]):
            raise ValueError(f"{key} 账本规则指纹漂移")
        content_hash = row.get("content_sha256")
        # 已有独立策略信号没有逐事件指纹，仍受输入指纹及已发布账本前缀约束。
        if content_hash is not None or kind == "execution" or not profile.get("independent_rules"):
            if content_hash != _hash({k: v for k, v in row.items() if k not in ("recorded_at", "content_sha256")}):
                raise ValueError(f"{key} 账本内容指纹不匹配")
        if profile.get("independent_rules"):
            _fingerprint(row.get(f"{key}_input_sha256"), f"{key} 策略输入")
        if kind == "signal":
            signals[period] = row
        else:
            if period not in signals or signals[period]["signal_date"] != signal_day or event_day <= signal_day:
                raise ValueError(f"{key} 执行缺少对应的先前信号")
            cumulative += row.get("operations") or []
            if row.get("cumulative_events") != cumulative:
                raise ValueError(f"{key} 累计交易与追加事件不一致")
            for field in ("cash", "nav"):
                value = float(row.get(field, float("nan")))
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{key} 账本 {field} 无效")
            if not profile.get("independent_rules"):
                if row.get("signal_input") != signals[period].get("input") or row.get("execution_input", {}).get("data_cutoff") != event_day:
                    raise ValueError(f"{key} 执行输入与信号或日期不一致")


def history_changes(previous: dict, current: dict) -> list[dict]:
    """只比较已公开的净值，页面展示字段或观察计划升级不算历史收益变更。"""
    by_date = {row["date"]: row for row in current["series"]}
    changes = []
    for old in previous.get("series") or []:
        new = by_date.get(old["date"], {})
        for field in ["benchmark_nav", *(f"{key}_nav" for key in FORWARD_STRATEGIES)]:
            if old.get(field) != new.get(field):
                changes.append({"date": old["date"], "field": field, "before": old.get(field), "after": new.get(field)})
    return changes


def validate_publication(metadatas: dict, journals: dict, market: dict, performance: dict, previous: dict | None = None, *, correction_reason: str | None = None) -> dict:
    """失败抛异常；仅有明确停牌证据的缺价允许沿用旧价，并留下警示。"""
    previous = previous or {}
    as_of = _date(market.get("as_of"))
    if set(metadatas) != set(FORWARD_STRATEGIES) or set(journals) != set(FORWARD_STRATEGIES):
        raise ValueError("发布必须包含五套策略")
    if performance.get("as_of") != as_of or previous.get("as_of", "") > as_of:
        raise ValueError("公开截止日不一致或倒退")
    if market.get("price_format") != "unadjusted_close":
        raise ValueError("公开行情必须是不复权收盘价")
    content = {k: v for k, v in market.items() if k != "retrieved_at"}
    content["hashes"] = {}
    if market.get("hashes", {}).get("content_sha256") != _hash(content):
        raise ValueError("行情快照指纹不匹配")
    calendar = [row["date"] for row in market["benchmark"]["prices"]]
    if not calendar or calendar != sorted(set(calendar)) or calendar[-1] != as_of:
        raise ValueError("基准日期缺失、重复或乱序")
    verified_calendar = market.get("trading_calendar") or {}
    if verified_calendar.get("provider") != "baostock_trade_dates" or verified_calendar.get("dates") != calendar:
        raise ValueError("基准行情与核验交易日历不一致")
    old_days = {row["date"] for row in previous.get("series", [])}
    if not old_days <= set(calendar):
        raise ValueError("公开历史日期被截断")
    if history_changes(previous, performance) and not (correction_reason or "").strip():
        raise ValueError("历史公开净值发生变化，必须显式提供修复原因后才能发布")
    for security in [market["benchmark"], *market.get("securities", {}).values()]:
        rows = security.get("prices") or []
        days = [_date(row["date"]) for row in rows]
        if not days or days != sorted(set(days)) or days[-1] > as_of:
            raise ValueError("证券行情日期缺失、重复、乱序或包含未来数据")
        if any(not math.isfinite(float(row["close"])) or float(row["close"]) <= 0 for row in rows):
            raise ValueError("证券价格必须为有限正数")
        for field in ("prices", "dividends"):
            if security.get("hashes", {}).get(f"{field}_sha256") != _hash(security.get(field) or []):
                raise ValueError(f"证券 {field} 指纹不匹配")
    report = {"status": "passed", "as_of": as_of, "strategies": {}, "warnings": []}
    for key in FORWARD_STRATEGIES:
        metadata, rows = metadatas[key], journals[key]
        if metadata != build_metadata(key):
            raise ValueError(f"{key} 元数据不符合冻结合同")
        _verify_journal(key, rows, as_of, previous)
        result = performance["strategies"][key]
        if [row["date"] for row in result["series"]] != calendar:
            raise ValueError(f"{key} 净值日期与共同基准不一致")
        if result["audit"]["journal_sha256"] != _hash(rows) or result["audit"]["rules_sha256"] != metadata["rules_sha256"]:
            raise ValueError(f"{key} 公开数据与账本或规则指纹不一致")
        if result.get("health", {}).get("outcome") == "failure":
            raise ValueError(f"{key} 最近账本动作失败，停止发布")
        for code, days in required_price_dates(rows, calendar).items():
            security = market.get("securities", {}).get(code) or {}
            missing = days - {row["date"] for row in security.get("prices", [])}
            evidence = security.get("suspension_evidence") or {}
            confirmed = set(evidence.get("dates") or []) if evidence.get("provider") == "baostock_tradestatus" else set()
            if missing - confirmed:
                raise ValueError(f"{key} {code} 行情缺失且停牌未确认：{sorted(missing - confirmed)}")
            if missing:
                report["warnings"].append(f"{key} {code} 有 {len(missing)} 个持仓日经确认停牌，沿用此前价格估值")
        signals = [row for row in rows if row["event_type"] == "signal"]
        executions = [row for row in rows if row["event_type"] == "execution"]
        for row in signals:
            month_days = [day for day in calendar if day[:7] == row["period"]]
            if not month_days or row["signal_date"] != month_days[-1] or row["signal_date"] < metadata["first_signal_date"]:
                raise ValueError(f"{key} 信号不是已核验的月末交易日")
        for row in executions:
            next_day = next((day for day in calendar if day > row["signal_date"]), None)
            if row["execution_date"] != next_day:
                raise ValueError(f"{key} 执行不是信号后的下一真实交易日")
        completed_months = {day[:7] for day in calendar if day >= metadata["first_signal_date"] and day[:7] < as_of[:7]}
        if not completed_months <= {row["period"] for row in signals}:
            raise ValueError(f"{key} 缺少已结束月份的信号")
        executed = {row["period"] for row in executions}
        pending = [row for row in signals if row["period"] not in executed]
        if len(pending) > 1 or any(any(day > row["signal_date"] for day in calendar) for row in pending):
            raise ValueError(f"{key} 有逾期待执行信号，停止发布")
        holdings = []
        for holding in result["holdings"]:
            stale = holding["price_date"] != as_of
            security = market["securities"][holding["code"]]
            holdings.append({"code": holding["code"], "price_date": holding["price_date"],
                             "status": "confirmed_suspension" if stale else "current",
                             "source": (security.get("suspension_evidence") or {}).get("provider") if stale else security.get("selected_provider")})
        summary = result["summary"]
        if not all(math.isfinite(float(summary[field])) for field in ("total_assets", "cash", "cumulative_return_pct", "fees")):
            raise ValueError(f"{key} 公开指标包含非有限数值")
        if not math.isclose(summary["total_assets"], summary["cash"] + sum(row["market_value"] for row in result["holdings"]), abs_tol=0.02):
            raise ValueError(f"{key} 现金加持仓市值与总资产不一致")
        report["strategies"][key] = {
            "as_of": as_of, "last_signal_date": signals[-1]["signal_date"] if signals else None,
            "last_execution_date": executions[-1]["execution_date"] if executions else None,
            "pending_signal_date": pending[0]["signal_date"] if pending else None,
            "price_status": "confirmed_suspension" if any(row["status"] != "current" for row in holdings) else "current",
            "holdings": holdings, "journal_sha256": _hash(rows), "rules_sha256": metadata["rules_sha256"],
        }
    return report
