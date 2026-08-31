"""多资产风险预算 V2.2 的纯规则、前向成交和冻结回测。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import date
from pathlib import Path
from typing import Any, Sequence


MA_V22_RULES: dict[str, Any] = {
    "initial_capital": 100000.0,
    "frequency": "monthly",
    "execution_timing": "next_trading_day_open",
    "risk_assets": ["csi", "gold", "nq"],
    "momentum_lookback_days": 126,
    "volatility_window_days": 63,
    "target_volatility": 0.10,
    "max_holdings": 4,
    "lot_size": 100,
    "transaction_cost_rate": 0.001,
    "risk_free_rate": 0.02,
}

MA_V22_ASSETS: dict[str, dict[str, str]] = {
    "csi": {"code": "510300", "symbol": "sh510300", "name": "沪深300 ETF"},
    "gold": {"code": "518880", "symbol": "sh518880", "name": "黄金 ETF"},
    "nq": {"code": "513100", "symbol": "sh513100", "name": "纳指100 ETF"},
    "bond": {"code": "511010", "symbol": "sh511010", "name": "国债 ETF"},
}

MA_V22_ATTACHMENT_SHA256 = {
    "config.py": "afe2bb3d55ef1b2378849c974a5853b27976a423f2b9b3d8ec75e1c18620eb03",
    "strategy.py": "1d5b2987466622f87ae9549a1d8d3d57c5a9b00b7382ec0244644721653ef733",
    "fetcher.py": "d4b8c8e212553bb112bb398a05f3a37285f17c1d15c0187b3699286260f352c7",
    "monitor.py": "f34d8f8ec379f8f29005669f598ba2b78c388026d4aca507d3b88e1ad1bd93ff",
    "notify.py": "04cfd326fb72ef0830eb8267fb51676dd220b2309acfcb71e9eb974b345d3105",
    "selfcheck.py": "6fa2dc851a69138f7ea93676cdfde6a1c53b3d8c52c56fd01c17743efc5f4c4d",
    "requirements.txt": "809ecb105b8e60b9f697c628a9a72c3e587fb4747bf9f1836cc72117dbd45f8b",
    "reference_chart.png": "a329852eeab6975a9cfbdbcbd06757860cedd517d093c31adbb10e9c82f77945",
}

MIN_SIGNAL_DATE = "2014-02-01"


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def daily_returns(values: Sequence[float]) -> list[float]:
    prices = [float(value) for value in values]
    if any(not math.isfinite(value) or value <= 0 for value in prices):
        raise ValueError("价格必须是有限正数")
    return [prices[index] / prices[index - 1] - 1 for index in range(1, len(prices))]


def sample_covariance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("协方差序列必须等长且至少两个观测")
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    return sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right)) / (len(left) - 1)


def period_ends(days: Sequence[str]) -> list[str]:
    ends: dict[str, str] = {}
    for day in sorted(str(value)[:10] for value in days):
        ends[day[:7]] = day
    return list(ends.values())


def _asset_series(rows: Sequence[dict[str, Any]], field: str) -> dict[str, list[float]]:
    return {
        asset: [float(row["assets"][asset][field]) for row in rows]
        for asset in MA_V22_ASSETS
    }


def compute_target(
    price_rows: Sequence[dict[str, Any]], signal_date: str,
    rules: dict[str, Any] = MA_V22_RULES,
) -> tuple[dict[str, float], dict[str, dict[str, Any]], dict[str, Any]]:
    """按信号日收盘计算动量门控、逆波动率和 10% 波动目标。"""
    rows = sorted(
        (row for row in price_rows if str(row.get("date", "")) <= signal_date),
        key=lambda row: row["date"],
    )
    if not rows or rows[-1]["date"] != signal_date:
        raise ValueError("V2.2 输入没有信号日收盘价")
    lookback = int(rules["momentum_lookback_days"])
    window = int(rules["volatility_window_days"])
    if len(rows) <= max(lookback, window):
        raise ValueError("V2.2 输入不足完整动量或波动窗口")

    closes = _asset_series(rows, "hfq_close")
    returns = {asset: daily_returns(values) for asset, values in closes.items()}
    momentum = {
        asset: closes[asset][-1] / closes[asset][-lookback - 1] - 1
        for asset in rules["risk_assets"]
    }
    volatility = {
        asset: statistics.stdev(returns[asset][-window:]) * math.sqrt(252)
        for asset in MA_V22_ASSETS
    }
    active = [
        asset for asset in rules["risk_assets"]
        if momentum[asset] > 0 and volatility[asset] > 0
    ]
    if not active:
        target = {"bond": 1.0}
        portfolio_volatility = None
        scale = 1.0
    else:
        inverse = {asset: 1 / volatility[asset] for asset in active}
        total = sum(inverse.values())
        base = {asset: inverse[asset] / total for asset in active}
        variance = 0.0
        for left in active:
            for right in active:
                covariance = sample_covariance(
                    returns[left][-window:], returns[right][-window:]
                ) * 252
                variance += base[left] * base[right] * covariance
        portfolio_volatility = math.sqrt(max(variance, 0.0))
        scale = (
            min(1.0, float(rules["target_volatility"]) / portfolio_volatility)
            if portfolio_volatility > 0 else 1.0
        )
        target = {asset: scale * base[asset] for asset in active}
        target["bond"] = 1.0 - scale

    detail = {}
    for asset in rules["risk_assets"]:
        detail[asset] = {
            "momentum": momentum[asset],
            "volatility": volatility[asset],
            "active": asset in active,
            "weight": target.get(asset, 0.0),
        }
    detail["bond"] = {
        "momentum": None,
        "volatility": volatility["bond"],
        "active": True,
        "weight": target.get("bond", 0.0),
    }
    info = {
        "active_assets": active,
        "scale": scale,
        "portfolio_volatility": portfolio_volatility,
    }
    if not math.isclose(sum(target.values()), 1.0, abs_tol=1e-10):
        raise RuntimeError("V2.2 目标权重合计不为 100%")
    return target, detail, info


def load_inputs(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("strategy") != "ma_v22" or payload.get("price_format") != "tencent_hfq_signal_raw_execution":
        raise ValueError("V2.2 输入策略或价格口径不正确")
    if payload.get("assets") != MA_V22_ASSETS:
        raise ValueError("V2.2 输入资产清单不正确")
    inputs = payload.get("inputs") or {}
    for name in ("prices", "dividends"):
        rows = inputs.get(name)
        if not isinstance(rows, list) or (payload.get("hashes") or {}).get(name) != canonical_sha256(rows):
            raise ValueError(f"V2.2 输入 {name} 缺失或哈希校验失败")
    prices = inputs["prices"]
    expected_assets = set(MA_V22_ASSETS)
    if not prices or any(set((row.get("assets") or {}).keys()) != expected_assets for row in prices):
        raise ValueError("V2.2 每个价格行必须包含四项冻结资产")
    if prices[-1].get("date") != payload.get("as_of"):
        raise ValueError("V2.2 输入最后日期必须等于 as_of")
    attachments = {row.get("name"): row.get("sha256") for row in payload.get("attachments") or []}
    if attachments != MA_V22_ATTACHMENT_SHA256:
        raise ValueError("V2.2 参考附件指纹不一致")
    content = dict(payload)
    expected = content.pop("content_sha256", None)
    if expected != canonical_sha256(content):
        raise ValueError("V2.2 输入 content_sha256 校验失败")
    return payload


def build_forward_signal(
    signal_date: str,
    dates_path: Path,
    journal_rows: Sequence[dict[str, Any]],
    input_path: Path,
) -> dict[str, Any]:
    date.fromisoformat(signal_date)
    dates_payload = json.loads(Path(dates_path).read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    if signal_date not in dates:
        raise ValueError("V2.2 信号日不在版本化月末日期中")
    snapshot = load_inputs(input_path)
    if snapshot.get("as_of") != signal_date:
        raise ValueError("V2.2 输入截止日必须等于信号日")
    prices = snapshot["inputs"]["prices"]
    target, detail, info = compute_target(prices, signal_date)
    previous = next((row for row in reversed(journal_rows) if row.get("event_type") == "execution"), None)
    target_codes = [MA_V22_ASSETS[asset]["code"] for asset, weight in target.items() if weight > 0]
    signal_prices = [row for row in prices if row["date"] <= signal_date]
    return {
        "schema_version": 1,
        "event_type": "signal",
        "strategy_id": "ma_v22",
        "strategy_version": "V2.2",
        "shadow": True,
        "period": signal_date[:7],
        "signal_date": signal_date,
        "target_codes": target_codes,
        "target_weights": target,
        "candidate_pool": {"count": 4, "codes": [row["code"] for row in MA_V22_ASSETS.values()]},
        "decision_snapshot": {
            "held_codes": sorted(str(row.get("code")) for row in (previous or {}).get("holdings", [])),
            "eligible_entry_codes": target_codes,
            "details": detail,
            **info,
        },
        "signal_prices_sha256": canonical_sha256(signal_prices),
        "ma_v22_input_sha256": snapshot["content_sha256"],
        "status": "等待下一真实交易日开盘模拟执行",
    }


def transaction_fees(gross: float, multiplier: float = 1.0) -> dict[str, float]:
    total = max(float(gross), 0.0) * float(MA_V22_RULES["transaction_cost_rate"]) * multiplier
    return {"model_cost": total, "total": total}


def round_lot_shares(value: float, price: float, lot_size: int = 100) -> int:
    if price <= 0 or lot_size <= 0:
        raise ValueError("价格和整手数量必须为正数")
    return max(0, math.floor(max(float(value), 0.0) / price / lot_size) * lot_size)


def _entry_dates(events: Sequence[dict[str, Any]]) -> dict[str, str]:
    entries: dict[str, str] = {}
    shares: dict[str, int] = {}
    for event in events:
        code = str(event.get("code") or "")
        side = event.get("side")
        amount = int(event.get("shares") or 0)
        if side == "买入" and code:
            if shares.get(code, 0) == 0:
                entries[code] = str(event.get("date") or "")[:10]
            shares[code] = shares.get(code, 0) + amount
        elif side == "卖出" and code:
            shares[code] = max(shares.get(code, 0) - amount, 0)
            if shares[code] == 0:
                entries.pop(code, None)
    return entries


def build_forward_execution(
    period: str,
    journal_rows: Sequence[dict[str, Any]],
    input_path: Path,
) -> dict[str, Any]:
    snapshot = load_inputs(input_path)
    signals = [
        row for row in journal_rows
        if row.get("event_type") == "signal" and row.get("strategy_id") == "ma_v22" and row.get("period") == period
    ]
    if len(signals) != 1:
        raise ValueError("执行期必须恰有一个 V2.2 信号")
    signal = signals[0]
    prices = snapshot["inputs"]["prices"]
    historical = [row for row in prices if row["date"] <= signal["signal_date"]]
    if canonical_sha256(historical) != signal.get("signal_prices_sha256"):
        raise ValueError("V2.2 执行输入改变了信号日前价格")
    future = [row for row in prices if row["date"] > signal["signal_date"]]
    if not future:
        raise ValueError("V2.2 尚无信号日后的下一交易日开盘价")
    execution_row = min(future, key=lambda row: row["date"])
    execution_date = execution_row["date"]
    previous = next((row for row in reversed(journal_rows) if row.get("event_type") == "execution"), None)
    holdings = {str(row["code"]): dict(row) for row in (previous or {}).get("holdings", [])}
    cash = float((previous or {}).get("cash", MA_V22_RULES["initial_capital"]))
    operations: list[dict[str, Any]] = []
    previous_date = str((previous or {}).get("execution_date") or signal["signal_date"])
    entries = _entry_dates((previous or {}).get("cumulative_events", []))
    credited = {
        (str(op.get("code")), str(op.get("pay_date")))
        for row in journal_rows for op in row.get("operations", []) if op.get("side") == "分红"
    }
    for item in snapshot["inputs"]["dividends"]:
        code = str(item.get("code") or "")
        pay_date = str(item.get("pay_date") or "")[:10]
        record_date = str(item.get("record_date") or "")[:10]
        if code not in holdings or not (previous_date < pay_date <= execution_date):
            continue
        if entries.get(code, previous_date) > record_date or (code, pay_date) in credited:
            continue
        shares = int(holdings[code]["shares"])
        gross = shares * float(item.get("cash_per_unit") or 0)
        cash += gross
        operations.append({
            "date": pay_date, "record_date": record_date, "ex_date": item.get("ex_date"),
            "pay_date": pay_date, "side": "分红", "code": code, "shares": shares,
            "gross": gross, "net_cash": gross, "fees": {"total": 0.0}, "reason": "ETF 现金分红",
        })

    raw_open = {MA_V22_ASSETS[asset]["code"]: float(value["raw_open"])
                for asset, value in execution_row["assets"].items()}
    raw_close = {MA_V22_ASSETS[asset]["code"]: float(value["raw_close"])
                 for asset, value in execution_row["assets"].items()}
    nav_at_open = cash + sum(int(row["shares"]) * raw_open[code] for code, row in holdings.items())
    target_by_code = {
        MA_V22_ASSETS[asset]["code"]: float(weight)
        for asset, weight in signal["target_weights"].items()
    }
    lot = int(MA_V22_RULES["lot_size"])
    desired = {
        code: round_lot_shares(nav_at_open * target_by_code.get(code, 0.0), price, lot)
        for code, price in raw_open.items()
    }

    for code in sorted(holdings):
        shares = max(int(holdings[code]["shares"]) - desired.get(code, 0), 0)
        if not shares:
            continue
        gross = shares * raw_open[code]
        fees = transaction_fees(gross)
        cash += gross - fees["total"]
        holdings[code]["shares"] -= shares
        operations.append({
            "date": execution_date, "side": "卖出", "code": code,
            "name": next(row["name"] for row in MA_V22_ASSETS.values() if row["code"] == code),
            "shares": shares, "price": raw_open[code], "gross": gross,
            "net_cash": gross - fees["total"], "fees": fees, "reason": "V2.2 月度目标权重",
        })
        if holdings[code]["shares"] == 0:
            holdings.pop(code)

    for asset in ("csi", "gold", "nq", "bond"):
        code = MA_V22_ASSETS[asset]["code"]
        shares = max(desired[code] - int(holdings.get(code, {}).get("shares", 0)), 0)
        while shares:
            gross = shares * raw_open[code]
            fees = transaction_fees(gross)
            if gross + fees["total"] <= cash + 1e-9:
                break
            shares -= lot
        if not shares:
            continue
        gross = shares * raw_open[code]
        fees = transaction_fees(gross)
        cash -= gross + fees["total"]
        old_shares = int(holdings.get(code, {}).get("shares", 0))
        old_cost = float(holdings.get(code, {}).get("entry_price", raw_open[code])) * old_shares
        holdings[code] = {
            "code": code,
            "shares": old_shares + shares,
            "entry_price": (old_cost + gross) / (old_shares + shares),
        }
        operations.append({
            "date": execution_date, "side": "买入", "code": code, "name": MA_V22_ASSETS[asset]["name"],
            "shares": shares, "price": raw_open[code], "gross": gross,
            "net_cash": gross + fees["total"], "fees": fees, "reason": "V2.2 月度目标权重",
        })

    nav = cash + sum(int(row["shares"]) * raw_close[code] for code, row in holdings.items())
    cumulative = list((previous or {}).get("cumulative_events", [])) + operations
    event = {
        "schema_version": 1,
        "event_type": "execution",
        "strategy_id": "ma_v22",
        "strategy_version": "V2.2",
        "shadow": True,
        "period": period,
        "signal_date": signal["signal_date"],
        "execution_date": execution_date,
        "execution_timing": "open",
        "target_weights": signal["target_weights"],
        "execution_prices": raw_open,
        "closing_prices": raw_close,
        "operations": operations,
        "cumulative_events": cumulative,
        "holdings": list(holdings.values()),
        "cash": round(cash, 6),
        "fees": round(sum(float((op.get("fees") or {}).get("total", 0)) for op in operations), 6),
        "nav": round(nav, 6),
        "ma_v22_input_sha256": snapshot["content_sha256"],
        "status": "已按下一真实交易日开盘价完成模型执行，并按当日收盘价估值",
    }
    event["content_sha256"] = canonical_sha256(event)
    return event


def backtest_metrics(nav_rows: Sequence[dict[str, Any]], trade_count: int) -> dict[str, Any]:
    values = [float(row["nav"]) for row in nav_rows]
    if len(values) < 2:
        raise ValueError("V2.2 NAV 序列不足")
    years = (date.fromisoformat(nav_rows[-1]["date"]) - date.fromisoformat(nav_rows[0]["date"])).days / 365.25
    returns = daily_returns(values)
    volatility = statistics.stdev(returns) * math.sqrt(252)
    cagr = (values[-1] / values[0]) ** (1 / years) - 1
    sharpe = ((statistics.mean(returns) - MA_V22_RULES["risk_free_rate"] / 252) * 252 / volatility
              if volatility > 0 else 0.0)
    peak, max_drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1 - value / peak)
    monthly = {}
    for row in nav_rows:
        monthly[row["date"][:7]] = row
    months = list(monthly.values())

    def rolling_worst(window: int) -> float | None:
        results = []
        for index in range(window, len(months)):
            start, end = months[index - window], months[index]
            if ((date.fromisoformat(end["date"]).year * 12 + date.fromisoformat(end["date"]).month)
                    - (date.fromisoformat(start["date"]).year * 12 + date.fromisoformat(start["date"]).month)) != window:
                continue
            results.append((float(end["nav"]) / float(start["nav"])) ** (12 / window) - 1)
        return min(results) if results else None

    oos = {}
    for year in (2021, 2023):
        rows = [row for row in nav_rows if row["date"] >= f"{year}-01-01"]
        if len(rows) > 1:
            elapsed = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days / 365.25
            oos[str(year)] = (float(rows[-1]["nav"]) / float(rows[0]["nav"])) ** (1 / elapsed) - 1
    return {
        "years": years, "cagr": cagr, "volatility": volatility, "sharpe": sharpe,
        "max_drawdown": max_drawdown, "trade_count": trade_count,
        "rolling_36m_worst_cagr": rolling_worst(36),
        "rolling_48m_worst_cagr": rolling_worst(48),
        "continuous_oos_cagr": oos,
    }


def _run_weight_backtest(price_rows: Sequence[dict[str, Any]], cost: float) -> dict[str, Any]:
    rows = sorted(price_rows, key=lambda row: row["date"])
    days = [row["date"] for row in rows]
    opens = _asset_series(rows, "hfq_open")
    index_of = {day: index for index, day in enumerate(days)}
    targets = {}
    for signal_date in period_ends(days):
        if signal_date < MIN_SIGNAL_DATE:
            continue
        index = index_of[signal_date]
        if index + 1 < len(days):
            targets[days[index + 1]] = compute_target(rows[:index + 1], signal_date)[0]

    nav = 1.0
    weights: dict[str, float] | None = None
    first_execution = None
    nav_rows = []
    weight_rows = []
    trade_count = 0
    turns = []
    for index, day in enumerate(days):
        if index > 0 and weights is not None:
            asset_returns = {
                asset: opens[asset][index] / opens[asset][index - 1] - 1
                for asset in MA_V22_ASSETS
            }
            portfolio_return = sum(weight * asset_returns[asset] for asset, weight in weights.items())
            nav *= 1 + portfolio_return
            weights = {
                asset: weight * (1 + asset_returns[asset]) / (1 + portfolio_return)
                for asset, weight in weights.items()
            }
        if day in targets:
            target = targets[day]
            turn = sum(abs(target.get(asset, 0.0) - (weights or {}).get(asset, 0.0))
                       for asset in set(target) | set(weights or {}))
            trade_count += sum(abs(target.get(asset, 0.0) - (weights or {}).get(asset, 0.0)) > 1e-12
                               for asset in set(target) | set(weights or {}))
            nav *= 1 - cost * turn
            weights = dict(target)
            first_execution = first_execution or day
            turns.append({"date": day, "turnover": turn})
        if first_execution is not None:
            nav_rows.append({"date": day, "nav": nav})
            weight_rows.append({"date": day, **{asset: (weights or {}).get(asset, 0.0) for asset in MA_V22_ASSETS}})
    return {
        "nav_series": nav_rows,
        "weights": weight_rows,
        "turnovers": turns,
        "trade_count": trade_count,
        "first_execution_date": first_execution,
        "last_execution_date": max(targets) if targets else None,
    }


def run_frozen_backtest(input_path: Path, initial_capital: float = 1_000_000.0) -> dict[str, Any]:
    snapshot = load_inputs(input_path)
    normal = _run_weight_backtest(snapshot["inputs"]["prices"], MA_V22_RULES["transaction_cost_rate"])
    high = _run_weight_backtest(snapshot["inputs"]["prices"], MA_V22_RULES["transaction_cost_rate"] * 3)
    normal_nav = [{"date": row["date"], "nav": row["nav"] * initial_capital} for row in normal["nav_series"]]
    high_nav = [{"date": row["date"], "nav": row["nav"] * initial_capital} for row in high["nav_series"]]
    average_weights = {
        asset: statistics.mean(float(row[asset]) for row in normal["weights"])
        for asset in MA_V22_ASSETS
    }
    result = {
        "schema_version": 1,
        "strategy": "ma_v22",
        "display_name": "多资产风险预算 V2.2（全球版影子）",
        "initial_capital": initial_capital,
        "inputs": {"ma_v22_content_sha256": snapshot["content_sha256"]},
        "metrics": backtest_metrics(normal_nav, normal["trade_count"]),
        "high_cost_metrics": backtest_metrics(high_nav, high["trade_count"]),
        "average_weights": average_weights,
        "nav_series": normal_nav,
        "turnovers": normal["turnovers"],
        "first_execution_date": normal["first_execution_date"],
        "last_execution_date": normal["last_execution_date"],
        "reference_claim": {"cagr": 0.131, "sharpe": 1.04, "max_drawdown": 0.136},
        "limitations": [
            "参考包未附 weights_multi.csv 和 nav_multi.csv，无法执行其逐点自检。",
            "当前结果使用刷新时冻结的腾讯后复权行情；在线源修订历史会改变输入指纹。",
            "历史回测不代表未来收益，也不是买卖建议。",
        ],
    }
    result["content_sha256"] = canonical_sha256(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="多资产风险预算 V2.2 冻结输入回测")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    args = parser.parse_args()
    result = run_frozen_backtest(args.input, args.initial_capital)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
