"""以真实未复权场内价格回放条件性下穿买入，不联网、不连接账户。"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import calendar
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
from statistics import mean, median
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARAMETERS = {"initial_capital": 100000, "lot_size": 100, "commission_rate": .0003,
                      "minimum_commission": 5, "slippage_rate": .0005, "entry_lag_sessions": 1,
                      "holding_months": [6, 12, 24], "reinvest_dividends": False}
CONDITIONAL_LABEL = "按事后历史曲线定义的条件性结果，未证明历史发布可知性；不是可实时执行的胜率"
CENT = Decimal("0.01")
ZERO = Decimal(0)


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期必须为YYYY-MM-DD")
    return value


def decimal(value, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError("金额或比例不能是布尔值")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError("数值必须是有限正数")
    return result


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def read_json(path):
    def invalid(value):
        raise ValueError("JSON包含非有限数字：" + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=".crossing-entry-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_parameters(path):
    parameters = read_json(path)
    if not isinstance(parameters, dict) or set(parameters) != set(DEFAULT_PARAMETERS):
        raise ValueError("研究参数字段不完整或含未授权规则")
    for key in ["initial_capital", "commission_rate", "minimum_commission", "slippage_rate"]:
        decimal(parameters[key], allow_zero=key in ["commission_rate", "minimum_commission", "slippage_rate"])
    if parameters["reinvest_dividends"] is not False or parameters["holding_months"] != [6, 12, 24]:
        raise ValueError("本轮固定现金分红不复投和6/12/24个月，不做参数搜索")
    if parameters["lot_size"] != 100 or parameters["entry_lag_sessions"] != 1:
        raise ValueError("本轮固定100份整手和信号后下一交易日进入")
    return parameters


def add_months(value, months):
    initial = date.fromisoformat(value)
    year, month0 = divmod(initial.year * 12 + initial.month - 1 + months, 12)
    return date(year, month0 + 1, min(initial.day, calendar.monthrange(year, month0 + 1)[1])).isoformat()


def prepare(asset, cutoff=None):
    if asset.get("_prepared"):
        return asset
    listing = day(asset["listing_date"])
    prices = {}
    trade_days = []
    for row in asset["prices"]:
        current = day(row["date"])
        if current in prices:
            raise ValueError("价格日期重复")
        close = decimal(row["close"])
        if cutoff is None or current <= cutoff:
            prices[current] = close
            volume = row.get("volume", row.get("volume_shares"))
            if current >= listing and (volume is None or decimal(volume, allow_zero=True) > 0):
                trade_days.append(current)
    if not prices:
        raise ValueError("资产没有截止日内的有效场内价格")
    days = sorted(prices)
    events = defaultdict(list)
    for index, dividend in enumerate(asset.get("dividends", [])):
        record, ex, pay = (day(dividend[key]) for key in ["record_date", "ex_date", "pay_date"])
        amount = decimal(dividend["cash_per_unit"])
        if dividend.get("verified") is False:
            covered = any(interval["start"] <= record and interval["end"] >= pay for interval in asset.get("quality", {}).get("excluded_intervals", []))
            if not record <= ex <= pay or not covered:
                raise ValueError("未核实分红缺少完整排除区间，禁止当作已知权益")
            continue
        if not record < ex <= pay:
            raise ValueError("分红日期必须满足权益登记日早于除息日，且除息不晚于到账")
        for effective, kind in [(record, "record"), (ex, "ex"), (pay, "pay")]:
            events[effective].append({"kind": kind, "id": index, "amount": amount, "record": record, "ex": ex, "pay": pay})
    for split in asset.get("splits", []):
        effective = day(split["date"])
        ratio = decimal(split["ratio"])
        events[effective].append({"kind": "split", "ratio": ratio})
    exclusions = []
    quality = asset.get("quality", {})
    for interval in quality.get("excluded_intervals", []):
        start, end = day(interval["start"]), day(interval["end"])
        if end < start:
            raise ValueError("未核实区间日期倒置")
        exclusions.append((start, end, str(interval["reason"])))
    if quality.get("status") in ["blocked", "failed", "unverified"]:
        exclusions.append((listing, days[-1], "资产数据质量未通过：" + str(quality.get("reason", quality["status"]))))
    return {"_prepared": True, "prices": prices, "days": days, "trade_days": sorted(trade_days),
            "events": events, "event_days": sorted(events), "listing_date": listing,
            "excluded_intervals": exclusions, "as_of": days[-1], "source": asset}


def fee(notional, parameters, multiplier):
    rate = decimal(parameters["commission_rate"], allow_zero=True) * multiplier
    minimum = decimal(parameters["minimum_commission"], allow_zero=True) * multiplier
    return max(money(notional * rate), money(minimum))


def replay(asset, signal_date, parameters, *, extra_delay=0, cost_multiplier=1, audit=True):
    data = prepare(asset)
    day(signal_date)
    months = parameters["holding_months"]
    result = {"signal_date": signal_date, "status": "incomplete", "reason": None,
              "horizons": {str(m): None for m in months}, "horizon_reasons": {}, "horizon_status": {}}
    def unavailable(reason, status):
        result["reason"] = reason
        result["status"] = status
        result["horizon_reasons"] = {str(m): reason for m in months}
        result["horizon_status"] = {str(m): status for m in months}
        return result
    if signal_date < data["listing_date"]:
        return unavailable("信号发生时该产品尚未上市，不能买入或拼接其他产品历史", "excluded")
    if signal_date < data["days"][0]:
        return unavailable("信号早于已核实场内价格覆盖范围", "excluded")
    position = bisect_right(data["trade_days"], signal_date) + parameters["entry_lag_sessions"] - 1 + extra_delay
    if position >= len(data["trade_days"]):
        return unavailable("信号后尚无可用交易日或数据未覆盖成交日", "incomplete")
    entry = data["trade_days"][position]
    multiplier = decimal(cost_multiplier)
    capital = decimal(parameters["initial_capital"])
    slip = decimal(parameters["slippage_rate"], allow_zero=True) * multiplier
    if slip >= 1:
        raise ValueError("滑点必须小于100%")
    raw_entry = data["prices"][entry]
    execution_entry = raw_entry * (1 + slip)
    lot = Decimal(parameters["lot_size"])
    units = (capital / execution_entry / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    while units > 0 and money(execution_entry * units) + fee(money(execution_entry * units), parameters, multiplier) > capital:
        units -= lot
    if units <= 0:
        return unavailable("资金不足以支付一手份额及费用", "excluded")
    entry_notional = money(execution_entry * units)
    entry_fee = fee(entry_notional, parameters, multiplier)
    entry_slip = money((execution_entry - raw_entry) * units)
    cash = capital - entry_notional - entry_fee
    entry_cash = cash
    initial_units = units
    ends = defaultdict(list)
    for horizon in months:
        key = str(horizon)
        target = add_months(entry, horizon)
        exit_index = bisect_left(data["trade_days"], target)
        if exit_index >= len(data["trade_days"]):
            result["horizon_reasons"][key] = f"不足{horizon}个完整日历月，不以较短窗口替代"
            result["horizon_status"][key] = "incomplete"
            continue
        exit_date = data["trade_days"][exit_index]
        blocked = [reason for start, end, reason in data["excluded_intervals"] if start <= exit_date and end >= entry]
        if blocked:
            result["horizon_reasons"][key] = "数据窗口未核实：" + "；".join(blocked)
            result["horizon_status"][key] = "excluded"
            continue
        ends[exit_date].append(key)
    if not ends:
        statuses = result["horizon_status"].values()
        result["status"] = "excluded" if all(s == "excluded" for s in statuses) else "incomplete"
        result["reason"] = "；".join(dict.fromkeys(result["horizon_reasons"].values()))
        return result
    last = max(ends)
    days = [d for d in data["days"] if entry <= d <= last]
    timeline = sorted(set(days) | {d for d in data["event_days"] if entry <= d <= last})
    claims = {}
    received = ZERO
    receivable = ZERO
    peak = capital
    max_drawdown = ZERO
    minimum = capital
    first_break_even = None
    last_close = raw_entry
    executable_days = set(data["trade_days"])
    ledger = [{"kind": "buy", "date": entry, "raw_close": float(raw_entry), "execution_price": float(execution_entry),
               "units": float(units), "notional": float(entry_notional), "commission": float(entry_fee), "slippage": float(entry_slip)}] if audit else []
    for current in timeline:
        actions = data["events"].get(current, [])
        for event in actions:
            if event["kind"] == "split" and current > entry:
                before = units
                units *= event["ratio"]
                if audit:
                    ledger.append({"kind": "split", "date": current, "ratio": float(event["ratio"]), "units_before": float(before), "units_after": float(units)})
        for event in actions:
            if event["kind"] == "ex" and event["id"] in claims:
                value = money(claims[event["id"]] * event["amount"])
                receivable += value
                claims[event["id"]] = value
                if audit:
                    ledger.append({"kind": "dividend_receivable", "date": current, "value": float(value), "pay_date": event["pay"]})
        for event in actions:
            if event["kind"] == "pay" and event["id"] in claims:
                value = claims.pop(event["id"])
                receivable -= value
                received += value
                cash += value
                if audit:
                    ledger.append({"kind": "dividend_payment", "date": current, "value": float(value)})
        if current in data["prices"]:
            last_close = data["prices"][current]
        nav = cash + units * last_close + receivable
        peak = max(peak, nav)
        max_drawdown = min(max_drawdown, nav / peak - 1)
        minimum = min(minimum, nav)
        sell_notional = money(units * last_close * (1 - slip))
        sell_fee = fee(sell_notional, parameters, multiplier)
        liquidation = cash + sell_notional - sell_fee + receivable
        if first_break_even is None and current in executable_days and liquidation >= capital:
            first_break_even = (date.fromisoformat(current) - date.fromisoformat(entry)).days
        for key in ends.get(current, []):
            sell_slip = money(units * last_close * slip)
            ending_events = ledger + [{"kind": "sell", "date": current, "raw_close": float(last_close),
                                      "execution_price": float(last_close * (1 - slip)), "units": float(units),
                                      "notional": float(sell_notional), "commission": float(sell_fee), "slippage": float(sell_slip)}] if audit else []
            result["horizons"][key] = {"status": "complete", "entry_date": entry, "exit_date": current,
                "entry_units": float(initial_units), "exit_units": float(units), "entry_raw_close": float(raw_entry),
                "exit_raw_close": float(last_close), "entry_execution_price": float(execution_entry),
                "exit_execution_price": float(last_close * (1 - slip)), "entry_cash_remaining": float(entry_cash),
                "entry_commission": float(entry_fee), "exit_commission": float(sell_fee), "total_commission": float(entry_fee + sell_fee),
                "entry_slippage": float(entry_slip), "exit_slippage": float(sell_slip), "total_slippage": float(entry_slip + sell_slip),
                "dividend_cash_received": float(received), "dividend_receivable": float(receivable),
                "ending_cash": float(cash + sell_notional - sell_fee), "ending_net_assets": float(liquidation),
                "net_return": float(liquidation / capital - 1), "max_drawdown": float(min(max_drawdown, liquidation / peak - 1)),
                "min_from_capital": float(min(minimum, liquidation) / capital - 1),
                "first_break_even_calendar_days": first_break_even, "events": ending_events}
            result["horizon_status"][key] = "complete"
        # 权益登记按当日交易后的持股；买入除息日者不会取得此前登记权益。
        for event in actions:
            if event["kind"] == "record" and entry <= current:
                claims[event["id"]] = units
                if audit:
                    ledger.append({"kind": "dividend_entitlement", "date": current, "units": float(units), "ex_date": event["ex"]})
    result["status"] = "complete" if all(result["horizons"].values()) else "incomplete"
    if result["horizon_reasons"]:
        result["reason"] = "；".join(dict.fromkeys(result["horizon_reasons"].values()))
    return result


def summarize(rows):
    summary = {}
    for horizon in [6, 12, 24]:
        values = [row["horizons"][str(horizon)] for row in rows if row["horizons"][str(horizon)] is not None]
        returns = [value["net_return"] for value in values]
        waits = [value["first_break_even_calendar_days"] for value in values if value["first_break_even_calendar_days"] is not None]
        summary[str(horizon)] = {"observations": len(values), "positive": sum(value > 0 for value in returns),
            "mean_net_return": mean(returns) if returns else None, "median_net_return": median(returns) if returns else None,
            "worst_net_return": min(returns) if returns else None,
            "worst_drawdown": min(v["max_drawdown"] for v in values) if values else None,
            "median_first_break_even_calendar_days": median(waits) if waits else None,
            "never_break_even": len(values) - len(waits)}
    return summary


def group_summary(rows):
    return {"raw": summarize(rows), "retained": summarize([row for row in rows if row.get("retained_flag")])}


def build(inputs_path, signals_path, parameters_path, *, generated_at=None):
    inputs, signals = read_json(inputs_path), read_json(signals_path)
    from refresh_crossing_assets import verify as verify_asset_inputs
    if verify_asset_inputs(Path(inputs_path).parent) != inputs:
        raise ValueError("指定的资产输入不能从同目录冻结原始响应重建")
    parameters = load_parameters(parameters_path)
    if inputs.get("schema_version") != 1 or signals.get("schema_version") != 1:
        raise ValueError("输入或信号快照版本不受支持")
    requested = day(inputs["requested_as_of"])
    signal_cutoff = day(signals["as_of"])
    raw = [row for row in signals["crossings"] if row["direction"] == "down"]
    if len({row["date"] for row in raw}) != len(raw) or any(day(row["date"]) > signal_cutoff for row in raw) or raw != sorted(raw, key=lambda row: row["date"]):
        raise ValueError("信号日期重复或越过信号截止日")
    for signal in raw:
        index = signal["index"]
        if type(index) is not int or index <= 0 or index >= len(signals["chart"]["dates"]) or signals["chart"]["dates"][index] != signal["date"]:
            raise ValueError("下穿信号与冻结曲线日期不一致")
    retained = {row["date"] for row in signals["signals"]}
    if not retained <= {row["date"] for row in raw}:
        raise ValueError("保留信号不属于原始下穿集合")
    if set(inputs.get("assets", {})) != {"510300", "160706"}:
        raise ValueError("本轮必须分别提供510300 ETF与160706 LOF数据，不拼接、不遗漏")
    result = {"schema_version": 1, "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "as_of": None, "requested_as_of": requested, "signal_as_of": signal_cutoff,
        "methodology": {"conditional": True, "label": CONDITIONAL_LABEL, "parameters": parameters,
                        "drawdown_basis": "持仓未复权收盘市值＋现金＋除息后应收分红；退出日扣实际卖出成本",
                        "break_even_basis": "每日按相同卖出费用与滑点假设清算后的净资产首次不低于初始资金，按自然日计等待",
                        "cash_dividends": "权益登记后取得权利，除息计应收，到账转现金，不复投；无股票红利税"},
        "signal_hash": sha256(signals_path), "input_hash": sha256(inputs_path), "params_hash": sha256(parameters_path),
        "signal_context": {"as_of": signal_cutoff, "parameters_sha256": signals.get("audit", {}).get("parameters_sha256"),
                           "manifest_sha256": signals.get("audit", {}).get("manifest_sha256"),
                           "raw_downcrossing_dates": [row["date"] for row in raw]},
        "assets": {}, "limitations": [CONDITIONAL_LABEL, "信号仍来自存在日期与精度疑点的第三方历史曲线，不证明当时可知。",
            "每个信号单独投入资金，窗口可能重叠，不能把合计次数视为独立周期或可交易未来胜率。",
            "普通交易日对照同样存在重叠；没有参数搜索，也没有独立未见样本。",
            "未核实事件和行情冲突窗口一律排除；这种保守排除仍可能改变样本构成，不能视为无偏选择。",
            "ETF与LOF各自从上市后开始，不拼接不同产品历史；未核实区间排除，不使用复权价格重复加分红。"]}
    for code, source in inputs["assets"].items():
        if code not in ["510300", "160706"]:
            raise ValueError("本轮只允许510300 ETF与160706 LOF独立报告")
        expected_type, listing = {"510300": ("ETF", "2012-05-28"), "160706": ("LOF", "2005-10-17")}[code]
        if source["type"] != expected_type or source["listing_date"] != listing:
            raise ValueError("资产品种或上市日期与已确认产品不一致：" + code)
        if inputs.get("hashes", {}).get(code) != canonical_sha256(source):
            raise ValueError("资产内容与采集指纹不一致：" + code)
        if source.get("_prepared") or source.get("quality", {}).get("status") not in ["usable", "partial", "verified"]:
            raise ValueError("资产数据质量未通过或缺少明确的质量状态：" + code)
        if source.get("quality", {}).get("splits_verified") is not True:
            raise ValueError("尚未核实拆分事件，不能按价格直接回放：" + code)
        if any(day(row["date"]) > requested for row in source["prices"]):
            raise ValueError("资产价格越过研究输入截止日：" + code)
        prepared = prepare(source, requested)
        rows = []
        for signal in raw:
            row = replay(prepared, signal["date"], parameters)
            row["retained_flag"] = signal["date"] in retained
            row["signal_kind"] = "raw_downcrossing"
            row["signal_verification_status"] = signal.get("verification_status", "pending_date_validation")
            row["precision_sensitive"] = signal.get("precision_sensitive")
            rows.append(row)
        comparisons = []
        for current in prepared["trade_days"]:
            row = replay(prepared, current, parameters, audit=False)
            if any(row["horizons"].values()):
                comparisons.append(row)
        stress = {}
        for name, costs, delay in [("double_costs", 2, 0), ("extra_delay_2_sessions", 1, 2)]:
            stress_rows = []
            for signal in raw:
                row = replay(prepared, signal["date"], parameters, cost_multiplier=costs, extra_delay=delay)
                row["retained_flag"] = signal["date"] in retained
                stress_rows.append(row)
            stress[name] = {"summary": group_summary(stress_rows), "rows": stress_rows}
        result["assets"][code] = {"name": source["name"], "type": source["type"], "listing_date": source["listing_date"],
            "as_of": prepared["as_of"], "rows": rows, "summary": group_summary(rows),
            "comparisons": {**summarize(comparisons), "overlap": True}, "stress": stress,
            "quality": source.get("quality", {}), "input_sources": source.get("sources", {})}
    if not result["assets"]:
        raise ValueError("没有可研究资产")
    result["as_of"] = min(row["as_of"] for row in result["assets"].values())
    return result


def summary_markdown(result):
    lines = ["# 股债下穿后的条件性基金买入验证", "", f"实际评估截至：{result['as_of']}。", "", f"**{CONDITIONAL_LABEL}**", ""]
    for code, asset in result["assets"].items():
        lines += [f"## {code} {asset['name']}（{asset['type']}）", "", f"上市日：{asset['listing_date']}；数据截至：{asset['as_of']}。", "",
                  "| 口径 | 持有月数 | 完整次数 | 正收益次数 | 平均净收益 | 最差净收益 | 最差回撤 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for group, summary in asset["summary"].items():
            for horizon, values in summary.items():
                percent = lambda value: "—" if value is None else f"{value:+.2%}"
                lines.append(f"| {group} | {horizon} | {values['observations']} | {values['positive']} | {percent(values['mean_net_return'])} | {percent(values['worst_net_return'])} | {percent(values['worst_drawdown'])} |")
        lines.append("")
    lines += ["## 限制", ""] + ["- " + line for line in result["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=ROOT / "data/crossing-assets/inputs.json")
    parser.add_argument("--signals", type=Path, default=ROOT / "site/stock-bond.json")
    parser.add_argument("--parameters", type=Path, default=ROOT / "config/crossing_entry_research.json")
    parser.add_argument("--output", type=Path, default=ROOT / "site/entry-research.json")
    parser.add_argument("--summary-md", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    previous = read_json(args.output) if args.output.exists() else None
    unchanged = previous is not None and all(previous.get(key) == sha256(path) for key, path in [("input_hash", args.inputs), ("signal_hash", args.signals), ("params_hash", args.parameters)])
    expected = build(args.inputs, args.signals, args.parameters, generated_at=previous["generated_at"] if previous and (args.verify_only or unchanged) else None)
    if args.verify_only:
        if previous != expected:
            raise ValueError("研究产物与冻结输入、信号或参数不一致")
    else:
        write_json(args.output, expected)
        if args.summary_md:
            args.summary_md.parent.mkdir(parents=True, exist_ok=True)
            args.summary_md.write_text(summary_markdown(expected), encoding="utf-8", newline="\n")
    print(f"条件性研究校验通过；截至{expected['as_of']}；产品{','.join(expected['assets'])}；不是实时可执行胜率")


if __name__ == "__main__":
    main()
