"""用独立小样本证明条件性基金回放的交易、权益和时间边界。"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

import crossing_entry_research as engine


def parameters(**changes):
    return {**engine.DEFAULT_PARAMETERS, **changes}


def asset(prices, *, listing="2020-01-01", dividends=None, splits=None, quality=None):
    return {"name": "测试基金", "type": "ETF", "listing_date": listing,
            "prices": [{"date": day, "close": price} for day, price in prices],
            "dividends": dividends or [], "splits": splits or [],
            "quality": quality or {"status": "usable", "excluded_intervals": []}}


def test_next_session_lots_costs_and_no_signal_close_trade():
    data = asset([("2020-01-02", 1), ("2020-01-03", 10), ("2020-07-03", 11), ("2021-01-04", 12), ("2022-01-03", 13)])
    row = engine.replay(data, "2020-01-02", parameters())
    six = row["horizons"]["6"]
    assert six["entry_date"] == "2020-01-03"
    assert six["entry_units"] % 100 == 0
    assert six["entry_units"] == 9900
    assert six["entry_commission"] == pytest.approx(29.71)
    assert six["entry_slippage"] == pytest.approx(49.5)
    assert six["entry_cash_remaining"] == pytest.approx(920.79)
    assert six["exit_date"] == "2020-07-03"
    assert six["net_return"] < .1
    assert six["exit_commission"] >= 5


def test_ex_day_buyer_does_not_receive_earlier_holder_dividend():
    div = {"record_date": "2020-01-02", "ex_date": "2020-01-03", "pay_date": "2020-01-06", "cash_per_unit": .5}
    data = asset([("2020-01-02", 10), ("2020-01-03", 9.5), ("2020-01-06", 9.5), ("2020-07-03", 9.5)], dividends=[div])
    six = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    assert six["dividend_cash_received"] == 0
    assert six["dividend_receivable"] == 0


def test_ex_to_payment_receivable_prevents_false_drawdown_and_is_not_reinvested():
    div = {"record_date": "2020-01-03", "ex_date": "2020-01-06", "pay_date": "2020-01-10", "cash_per_unit": 1}
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-01-06", 9), ("2020-01-10", 9), ("2020-07-03", 9)], dividends=[div])
    six = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    assert six["dividend_cash_received"] == 9900
    assert six["exit_units"] == 9900
    assert six["max_drawdown"] > -.01
    assert six["net_return"] > -.01
    assert [event["kind"] for event in six["events"]] == ["buy", "dividend_entitlement", "dividend_receivable", "dividend_payment", "sell"]


def test_unpaid_dividend_remains_receivable_at_exit():
    div = {"record_date": "2020-07-01", "ex_date": "2020-07-02", "pay_date": "2020-07-06", "cash_per_unit": 1}
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-07-01", 10), ("2020-07-02", 9), ("2020-07-03", 9), ("2020-07-06", 9)], dividends=[div])
    six = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    assert six["dividend_cash_received"] == 0
    assert six["dividend_receivable"] == 9900
    assert six["net_return"] > -.01


def test_split_adjusts_units_once_and_does_not_fake_loss():
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-02-03", 5), ("2020-07-03", 5)], splits=[{"date": "2020-02-03", "ratio": 2}])
    six = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    assert six["entry_units"] == 9900
    assert six["exit_units"] == 19800
    assert six["net_return"] > -.01
    assert len([event for event in six["events"] if event["kind"] == "split"]) == 1


def test_listing_boundary_incomplete_period_and_quality_exclusion():
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-07-03", 10)], listing="2020-01-02")
    assert engine.replay(data, "2020-01-01", parameters())["status"] == "excluded"
    row = engine.replay(data, "2020-01-02", parameters())
    assert row["horizons"]["6"] is not None
    assert row["horizons"]["12"] is None
    data["quality"]["excluded_intervals"] = [{"start": "2020-03-01", "end": "2020-03-31", "reason": "拆分未核实"}]
    row = engine.replay(data, "2020-01-02", parameters())
    assert row["horizons"]["6"] is None
    assert "拆分未核实" in row["horizon_reasons"]["6"]


def test_minimum_fee_cash_limit_and_extra_delay():
    data = asset([("2020-01-02", 1), ("2020-01-03", 1), ("2020-01-06", 2), ("2020-01-07", 3), ("2020-07-07", 4)])
    row = engine.replay(data, "2020-01-02", parameters(initial_capital=1000), extra_delay=2)
    six = row["horizons"]["6"]
    assert six["entry_date"] == "2020-01-07"
    assert six["entry_commission"] == 5
    assert six["entry_units"] == 300
    assert six["entry_cash_remaining"] >= 0


def test_higher_costs_reduce_return_and_first_breakeven_is_after_fees():
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-01-06", 10), ("2020-01-07", 11), ("2020-07-03", 11)])
    one = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    two = engine.replay(data, "2020-01-02", parameters(), cost_multiplier=2)["horizons"]["6"]
    assert two["net_return"] < one["net_return"]
    assert one["first_break_even_calendar_days"] == 4


def test_suspended_quote_cannot_be_first_executable_breakeven():
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-01-06", 11), ("2020-01-07", 11), ("2020-07-03", 11)])
    data["prices"][2]["volume"] = 0
    six = engine.replay(data, "2020-01-02", parameters())["horizons"]["6"]
    assert six["first_break_even_calendar_days"] == 4


def test_unverified_dividend_is_excluded_instead_of_used_as_known_cash():
    div = {"record_date": "2020-01-06", "ex_date": "2020-01-06", "pay_date": "2020-01-10", "cash_per_unit": 1, "verified": False}
    data = asset([("2020-01-02", 10), ("2020-01-03", 10), ("2020-07-03", 11)], dividends=[div])
    with pytest.raises(ValueError, match="未核实分红"):
        engine.replay(data, "2020-01-02", parameters())
    data["quality"]["excluded_intervals"] = [{"start": "2020-01-06", "end": "2020-01-10", "reason": "场内分红日期未核实"}]
    six = engine.replay(data, "2020-01-02", parameters())
    assert six["horizons"]["6"] is None
    assert six["horizon_status"]["6"] == "excluded"


def test_invalid_nonfinite_price_and_wrong_dividend_order_fail_closed():
    data = asset([("2020-01-02", float("nan")), ("2020-01-03", 10)])
    with pytest.raises(ValueError):
        engine.replay(data, "2020-01-02", parameters())
    data = asset([("2020-01-02", 10), ("2020-01-03", 10)], dividends=[{"record_date": "2020-01-03", "ex_date": "2020-01-02", "pay_date": "2020-01-06", "cash_per_unit": 1}])
    with pytest.raises(ValueError):
        engine.replay(data, "2020-01-02", parameters())


def test_frozen_build_binds_signal_version_and_refuses_tampered_asset_hash(tmp_path, monkeypatch):
    import refresh_crossing_assets
    prices = [("2020-01-02", 10), ("2020-01-03", 10), ("2020-07-03", 11), ("2021-01-04", 12), ("2022-01-03", 13)]
    assets = {}
    for code, kind, listing in [("510300", "ETF", "2012-05-28"), ("160706", "LOF", "2005-10-17")]:
        assets[code] = asset(prices, listing=listing, quality={"status": "partial", "excluded_intervals": [], "splits_verified": True})
        assets[code]["type"] = kind
    inputs = {"schema_version": 1, "requested_as_of": "2022-01-03", "assets": assets,
              "hashes": {code: engine.canonical_sha256(value) for code, value in assets.items()}}
    signals = {"schema_version": 1, "as_of": "2020-01-02", "chart": {"dates": ["2020-01-01", "2020-01-02"]},
               "audit": {"parameters_sha256": "params", "manifest_sha256": "manifest"},
               "crossings": [{"date": "2020-01-02", "index": 1, "direction": "down", "verification_status": "pending_date_validation", "precision_sensitive": True}],
               "signals": [{"date": "2020-01-02", "index": 1, "direction": "down"}]}
    input_path, signal_path, parameter_path = [tmp_path / name for name in ["inputs.json", "signals.json", "parameters.json"]]
    for path, payload in [(input_path, inputs), (signal_path, signals), (parameter_path, parameters())]:
        path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(refresh_crossing_assets, "verify", lambda folder: inputs)
    result = engine.build(input_path, signal_path, parameter_path)
    assert result["as_of"] == "2022-01-03"
    assert result["signal_context"]["manifest_sha256"] == "manifest"
    assert result["methodology"]["conditional"] is True
    assert result["assets"]["510300"]["rows"][0]["signal_verification_status"] == "pending_date_validation"
    assert result["assets"]["510300"]["rows"][0]["precision_sensitive"] is True
    assert result["assets"]["510300"]["rows"][0]["horizons"]["24"] is not None
    inputs["assets"]["510300"]["prices"][-1]["close"] = 999
    input_path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="采集指纹"):
        engine.build(input_path, signal_path, parameter_path)


@pytest.mark.parametrize("change,message", [("blocked", "质量未通过"), ("split", "拆分"), ("listing", "上市日期")])
def test_build_rejects_unverified_quality_or_wrong_product(tmp_path, monkeypatch, change, message):
    import refresh_crossing_assets
    assets = {}
    for code, kind, listing in [("510300", "ETF", "2012-05-28"), ("160706", "LOF", "2005-10-17")]:
        assets[code] = asset([("2020-01-02", 10), ("2020-01-03", 10)], listing=listing,
                             quality={"status": "partial", "excluded_intervals": [], "splits_verified": True})
        assets[code]["type"] = kind
    if change == "blocked": assets["510300"]["quality"]["status"] = "blocked"
    elif change == "split": assets["510300"]["quality"]["splits_verified"] = False
    else: assets["510300"]["listing_date"] = "2000-01-01"
    inputs = {"schema_version": 1, "requested_as_of": "2020-01-03", "assets": assets,
              "hashes": {code: engine.canonical_sha256(value) for code, value in assets.items()}}
    signals = {"schema_version": 1, "as_of": "2020-01-03", "chart": {"dates": ["2020-01-02", "2020-01-03"]},
               "crossings": [], "signals": []}
    paths = [tmp_path / name for name in ["inputs.json", "signals.json", "parameters.json"]]
    for path, value in zip(paths, [inputs, signals, parameters()]): path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(refresh_crossing_assets, "verify", lambda folder: inputs)
    with pytest.raises(ValueError, match=message): engine.build(*paths)
