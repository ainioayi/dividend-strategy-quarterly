"""高息动量 V1/V2/V3/V5 与多资产 V2.2 前向观察账本。

账本使用只追加 JSONL 事件流。信号与执行分成两个命令；相同期已有不同内容时
立即拒绝，绝不覆盖。高息动量 V1 是正式前向模拟，其余策略使用独立影子账本。
脚本只读取冻结缓存，不连接券商，也不会自动下单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest
from quarterly_strategy import momentum_filter, screen_dynamic_pool, select_entry_candidates
from universe_manifest import load_manifest, verify_cache_snapshot

FORWARD_DIR = ROOT / "data" / "forward"
METADATA_PATH = FORWARD_DIR / "v1_metadata.json"
JOURNAL_PATH = FORWARD_DIR / "monthly_v1.jsonl"
SHADOW_DIR = FORWARD_DIR / "shadow"
FORWARD_CACHE_DIR = FORWARD_DIR / "cache"
FORWARD_INPUT_DIR = FORWARD_DIR / "inputs"

V1_COMMIT = "c7d128ff0bc1b4b21c60bc7c6e2894dabf513fae"
V1_START_DATE = "2026-08-25"
V1_FIRST_SIGNAL_DATE = "2026-08-31"
V1_MANIFEST_SHA256 = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"
V1_DATES_SHA256 = "f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e"
V1_DATA_CUTOFF = "2026-08-25"

V1_CAPITAL_POLICY: dict[str, Any] = {
    "target_allocation_pct": 100,
    "cash_reserve": 0,
    "residual_cash_rule": "仅保留整数手和交易费用约束下无法继续买入的现金",
}
V1_OBSERVATION_POLICY: dict[str, Any] = {
    "minimum_months": 6,
    "target_months": 12,
    "parameter_changes_allowed": False,
    "v2_mode": "shadow_only",
    "v2_output_root": "data/forward/shadow",
    "v2_can_write_v1_journal": False,
    "shadow_versions": ["V2", "V3", "V5", "V2.2"],
    "shadow_can_write_v1_journal": False,
}

# 这些值来自 c7d128f 的历史回测口径；PR 上限 999 表示前向观察沿用纯股息率层。
V1_RULES: dict[str, Any] = {
    "initial_capital": 100000.0,
    "frequency": "monthly",
    "entry_yield": 7.5,
    "entry_pr": 999.0,
    "hold_yield": 5.5,
    "loss_hold_yield": 5.5,
    "hold_pr": 999.0,
    "exit_yield": 5.3,
    "exit_pr": 999.0,
    "exit_confirm_quarters": 1,
    "max_holdings": 2,
    "max_sector": 2,
    "max_banks": 2,
    "max_position_pct": 1.0,
    "lot_size": 100,
    "reinvest_dividends": True,
    "reinvest_cash_reserve": 0,
    "rebalance_threshold": 2.0,
    "stop_loss_pct": 0.0,
    "buy_commission_rate": 0.0003,
    "sell_commission_rate": 0.0003,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "min_commission": 5.0,
    "pool_mode": "dynamic",
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "momentum_periods": "",
    "rank_by": "yield",
    "max_yield": 999.0,
    "execution_lag_days": 1,
    "dividend_information_lag_days": 0,
}

FORWARD_STRATEGIES: dict[str, dict[str, Any]] = {
    "v1": {
        "version": "V1",
        "name": "高息动量 V1（2只正式）",
        "short_name": "高息动量 V1（2只正式）",
        "shadow": False,
        "max_holdings": 2,
        "metadata_path": METADATA_PATH,
        "journal_path": JOURNAL_PATH,
    },
    "v2": {
        "version": "V2",
        "name": "高息动量 V2（3只影子）",
        "short_name": "高息动量 V2（3只影子）",
        "shadow": True,
        "max_holdings": 3,
        "metadata_path": SHADOW_DIR / "v2_metadata.json",
        "journal_path": SHADOW_DIR / "monthly_v2.jsonl",
    },
    "v3": {
        "version": "V3",
        "name": "高息动量 V3（4只影子）",
        "short_name": "高息动量 V3（4只影子）",
        "shadow": True,
        "max_holdings": 4,
        "metadata_path": SHADOW_DIR / "v3_metadata.json",
        "journal_path": SHADOW_DIR / "monthly_v3.jsonl",
    },
    "v5": {
        "version": "V5",
        "name": "高息动量 V5（附件规则影子）",
        "short_name": "高息动量 V5（附件规则影子）",
        "shadow": True,
        "independent_rules": True,
        "engine": "v5",
        "max_holdings": 6,
        "metadata_path": SHADOW_DIR / "v5_metadata.json",
        "journal_path": SHADOW_DIR / "monthly_v5.jsonl",
        "input_path": ROOT / "data" / "v5_inputs.json",
    },
    "ma_v22": {
        "version": "V2.2",
        "name": "多资产风险预算 V2.2（全球版影子）",
        "short_name": "多资产风险预算 V2.2（全球版影子）",
        "shadow": True,
        "independent_rules": True,
        "engine": "ma_v22",
        "max_holdings": 4,
        "metadata_path": SHADOW_DIR / "ma_v22_metadata.json",
        "journal_path": SHADOW_DIR / "monthly_ma_v22.jsonl",
        "input_path": ROOT / "data" / "ma_v22_inputs.json",
    },
}


def strategy_profile(strategy_id: str = "v1") -> dict[str, Any]:
    """返回不可共享修改的策略档案。V5 使用独立完整规则合同。"""
    key = str(strategy_id).lower()
    if key not in FORWARD_STRATEGIES:
        raise ValueError(f"未知前向策略: {strategy_id}")
    profile = dict(FORWARD_STRATEGIES[key])
    profile["strategy_id"] = key
    if profile.get("engine") == "v5":
        from v5_strategy import V5_RULES
        profile["rules"] = dict(V5_RULES)
    elif profile.get("engine") == "ma_v22":
        from ma_v22_strategy import MA_V22_RULES
        profile["rules"] = dict(MA_V22_RULES)
    else:
        profile["rules"] = {**V1_RULES, "max_holdings": profile["max_holdings"]}
    return profile


def _require_journal_boundary(
    profile: dict[str, Any], journal_path: Path, *, allow_isolated_journal: bool = False,
) -> None:
    if not profile["shadow"]:
        return
    resolved = journal_path.resolve()
    if resolved == JOURNAL_PATH.resolve():
        raise ValueError(f"{profile['version']} 禁止写入 V1 正式账本")
    if not allow_isolated_journal and not resolved.is_relative_to(SHADOW_DIR.resolve()):
        raise ValueError(f"{profile['version']} 只能写入 data/forward/shadow")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_dates(path: Path) -> tuple[list[str], str]:
    payload = _read_json(path)
    dates = payload.get("dates") if isinstance(payload, dict) else payload
    if not isinstance(dates, list):
        raise ValueError("月度日期文件缺少 dates")
    normalized = sorted({str(value)[:10] for value in dates})
    actual = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected = payload.get("dates_sha256") if isinstance(payload, dict) else None
    if expected and expected != actual:
        raise ValueError("月度日期文件 dates_sha256 校验失败")
    return normalized, actual


def _load_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"前向账本第 {number} 行不是有效 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"前向账本第 {number} 行不是对象")
        rows.append(row)
    return rows


def _append_once(path: Path, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按 event_type + period 保证幂等，并拒绝内容漂移。"""
    identity = (event["event_type"], event["period"])
    comparable = dict(event)
    comparable.pop("recorded_at", None)
    for old in _load_journal(path):
        if (old.get("event_type"), old.get("period")) != identity:
            continue
        old_comparable = dict(old)
        old_comparable.pop("recorded_at", None)
        if old_comparable == comparable:
            return old, False
        raise ValueError(f"{identity[1]} 已存在不同的 {identity[0]} 记录，拒绝覆盖")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    return event, True


def _cache_payload(cache_dir: Path, prefix: str, code: str, default: Any) -> Any:
    path = cache_dir / f"{prefix}_{code}.json"
    return _read_json(path) if path.exists() else default


def _calendar(cache_dir: Path, codes: list[str]) -> list[str]:
    dates = {
        str(day)[:10]
        for code in codes
        for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
        if price and len(str(day)) >= 10
    }
    return sorted(dates)


def _input_state(manifest_path: Path, dates_path: Path) -> tuple[dict[str, Any], list[str]]:
    manifest = load_manifest(manifest_path)
    dates, dates_hash = _load_dates(dates_path)
    state = {
        "manifest_records_sha256": manifest["records_sha256"],
        "dates_sha256": dates_hash,
        "data_cutoff": manifest["as_of"],
        "price_format": (manifest.get("source") or {}).get("price_format"),
    }
    return {"manifest": manifest, "input": state}, dates


def _pool(
    cache_dir: Path, codes: list[str], signal_date: str,
    rules: dict[str, Any] = V1_RULES,
) -> list[str]:
    summaries = {code: _cache_payload(cache_dir, "dv", code, []) for code in codes}
    details = {code: _cache_payload(cache_dir, "dvd", code, []) for code in codes}
    return screen_dynamic_pool(
        summaries,
        signal_date,
        int(rules["pool_min_consecutive_years"]),
        dividend_details_by_code=details,
        pool_switch_month=int(rules["pool_switch_month"]),
    )


def _previous_holdings(journal_path: Path, period: str) -> set[str]:
    executions = [
        row for row in _load_journal(journal_path)
        if row.get("event_type") == "execution" and str(row.get("period") or "") < period
    ]
    if not executions:
        return set()
    latest = max(executions, key=lambda row: str(row.get("period") or ""))
    return {str(row.get("code") or "").zfill(6) for row in latest.get("holdings", [])}


def _require_complete_cache(cache_dir: Path, codes: list[str]) -> None:
    missing = {
        prefix: [code for code in codes if not (cache_dir / f"{prefix}_{code}.json").exists()]
        for prefix in ("kl", "dv", "dvd")
    }
    missing = {prefix: values for prefix, values in missing.items() if values}
    if missing:
        raise ValueError(f"manifest 代码存在缺失缓存，拒绝生成信号: {missing}")


def _historical_input_snapshot(cache_dir: Path, codes: list[str], signal_date: str) -> dict[str, Any]:
    """冻结所有代码截至信号日的价格和已实施分红指纹。"""
    records = []
    for code in codes:
        prices = {
            str(day)[:10]: price
            for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
            if str(day)[:10] <= signal_date
        }
        details = [
            row for row in (_cache_payload(cache_dir, "dvd", code, []) or [])
            if str(row.get("ex_date") or "")[:10] <= signal_date
        ]
        records.append({
            "code": code,
            "prices_sha256": _hash(prices),
            "dividend_details_sha256": _hash(details),
        })
    return {"records": records, "records_sha256": _hash(records)}


def _decision_snapshot(
    cache_dir: Path,
    codes: list[str],
    signal_date: str,
    rebalance_dates: list[str],
    held_codes: set[str],
    rules: dict[str, Any] = V1_RULES,
) -> dict[str, Any]:
    """只用 signal_date 及以前数据重建策略的完整信号决策。"""
    klines = {
        code: {
            str(day)[:10]: price
            for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
            if str(day)[:10] <= signal_date
        }
        for code in codes
    }
    summaries = {code: _cache_payload(cache_dir, "dv", code, []) for code in codes}
    details = {
        code: [
            row for row in (_cache_payload(cache_dir, "dvd", code, []) or [])
            if str(row.get("ex_date") or "")[:10] <= signal_date
        ]
        for code in codes
    }
    pool_codes = screen_dynamic_pool(
        summaries, signal_date, int(rules["pool_min_consecutive_years"]),
        dividend_details_by_code=details,
        pool_switch_month=int(rules["pool_switch_month"]),
    )
    active_codes = sorted(set(pool_codes) | held_codes)
    rows = []
    for code in active_codes:
        price, price_date = backtest._find_price_with_date(klines.get(code, {}), signal_date)
        if not price or price <= 0:
            raise ValueError(f"候选或已有持仓在信号日前没有可用收盘价: {code}")
        row = backtest.build_snapshot(
            code, price, summaries.get(code, []), signal_date, details.get(code, [])
        )
        row["signal_date"] = signal_date
        row["signal_price_date"] = price_date
        row["signal_price_age_days"] = (
            datetime.strptime(signal_date, "%Y-%m-%d")
            - datetime.strptime(price_date, "%Y-%m-%d")
        ).days
        rows.append(row)
    applicable_dates = [date for date in rebalance_dates if date <= signal_date]
    price_lookup = lambda code, date: backtest._find_price(klines.get(code, {}), date)
    filtered = momentum_filter(
        rows, held_codes, price_lookup, signal_date, applicable_dates, rules
    )
    projection_fields = (
        "code", "price", "yield", "real_yield", "pr", "dps", "sustainability",
        "industry", "sector", "bank", "momentum_ratio", "signal_price_date",
        "signal_price_age_days",
    )
    all_projection = [
        {key: row.get(key) for key in projection_fields}
        for row in sorted(rows, key=lambda item: str(item.get("code") or ""))
    ]
    projection = [
        {key: row.get(key) for key in projection_fields}
        for row in sorted(filtered, key=lambda item: str(item.get("code") or ""))
    ]
    eligible = select_entry_candidates(filtered, rules, excluded=held_codes)
    pool_hash = hashlib.sha256(",".join(pool_codes).encode("utf-8")).hexdigest()
    decision = {
        "pool_codes": pool_codes,
        "pool_codes_sha256": pool_hash,
        "held_codes": sorted(held_codes),
        "all_rows": all_projection,
        "all_rows_sha256": _hash(all_projection),
        "rows": projection,
        "rows_sha256": _hash(projection),
        "eligible_entry_codes": [str(row.get("code") or "").zfill(6) for row in eligible],
    }
    decision["decision_sha256"] = _hash(decision)
    return decision


def build_metadata(strategy_id: str = "v1") -> dict[str, Any]:
    profile = strategy_profile(strategy_id)
    rules = profile["rules"]
    engine = profile.get("engine")
    metadata = {
        "schema_version": 1,
        "strategy": profile["name"],
        "mode": (
            "只追加模型账本，不连接券商、不自动下单"
            if not profile["shadow"]
            else "只追加影子模型账本，不连接券商、不自动下单"
        ),
        "v1_commit": V1_COMMIT,
        "forward_start_date": V1_START_DATE,
        "first_signal_date": V1_FIRST_SIGNAL_DATE,
        "first_execution_rule": (
            "首期信号日之后的下一真实交易日开盘模拟成交，并按当日收盘估值；预期 2026-09-01"
            if engine == "ma_v22"
            else "首期信号日之后的下一可用缓存交易日收盘；预期 2026-09-01"
        ),
        "frozen_backtest_input": {
            "manifest_records_sha256": V1_MANIFEST_SHA256,
            "dates_sha256": V1_DATES_SHA256,
            "data_cutoff": V1_DATA_CUTOFF,
            "price_format": "unadjusted_close",
        },
        "rules": rules,
        "rules_sha256": _hash(rules),
        "capital_policy": (
            {
                "initial_capital": 100000.0,
                "target_exposure_range_pct": [40, 100],
                "idle_cash_interest": True,
                "cash_rule": "风险阀门未投入部分保留现金并按附件利率逐交易日计息",
            }
            if engine == "v5" else dict(V1_CAPITAL_POLICY)
        ),
        "observation_policy": dict(V1_OBSERVATION_POLICY),
        "status": "等待 2026-08-31 收盘后的完整冻结输入，尚未生成信号",
    }
    if profile["shadow"]:
        metadata.update({
            "strategy_id": profile["strategy_id"],
            "version": profile["version"],
            "shadow": True,
            "base_strategy": None if profile.get("independent_rules") else "V1",
            "journal": str(profile["journal_path"].relative_to(ROOT)).replace("\\", "/"),
        })
        if engine == "v5":
            from v5_strategy import V5_ATTACHMENT_SHA256
            metadata["frozen_backtest_input"]["price_format"] = "sina_qfq_factors_with_unadjusted_cache"
            metadata["attachment_sha256"] = dict(V5_ATTACHMENT_SHA256)
            try:
                v5_input = profile["input_path"].relative_to(ROOT)
            except ValueError:
                v5_input = profile["input_path"]
            metadata["v5_input"] = str(v5_input).replace("\\", "/")
        elif engine == "ma_v22":
            from ma_v22_strategy import MA_V22_ATTACHMENT_SHA256
            metadata["frozen_backtest_input"] = {
                "price_format": "tencent_hfq_signal_raw_execution",
                "assets": ["510300", "518880", "513100", "511010"],
            }
            metadata["attachment_sha256"] = dict(MA_V22_ATTACHMENT_SHA256)
            try:
                input_path = profile["input_path"].relative_to(ROOT)
            except ValueError:
                input_path = profile["input_path"]
            metadata["ma_v22_input"] = str(input_path).replace("\\", "/")
        else:
            metadata["only_rule_change"] = {
                "field": "max_holdings",
                "from": V1_RULES["max_holdings"],
                "to": rules["max_holdings"],
            }
    return metadata


def verify_forward_contract(
    metadata_path: Path | None = None,
    freeze_path: Path = ROOT / "data" / "v1_freeze.json",
    strategy_id: str = "v1",
) -> dict[str, Any]:
    """校验前向参数、观察期和全量资金规则，任何漂移都失败关闭。"""
    profile = strategy_profile(strategy_id)
    metadata_path = metadata_path or profile["metadata_path"]
    if not metadata_path.exists():
        raise ValueError(f"{profile['version']} 前向元数据不存在")
    metadata = _read_json(metadata_path)
    expected = build_metadata(strategy_id)
    if metadata != expected:
        raise ValueError(f"{profile['version']} 前向元数据与冻结合同不一致")

    rules = metadata.get("rules") or {}
    engine = profile.get("engine")
    if engine == "v5":
        from v5_strategy import V5_ATTACHMENT_SHA256, V5_RULES
        if rules != V5_RULES or metadata.get("attachment_sha256") != V5_ATTACHMENT_SHA256:
            raise ValueError("V5 完整规则或附件指纹与冻结合同不一致")
        input_path = profile["input_path"]
        if not input_path.exists():
            raise ValueError("V5 冻结输入不存在")
        v5_input = _read_json(input_path)
        content = dict(v5_input)
        expected_content_hash = content.pop("content_sha256", None)
        if (
            v5_input.get("strategy") != "v5"
            or v5_input.get("price_format") != "sina_qfq_factors_with_unadjusted_cache"
            or expected_content_hash != _hash(content)
        ):
            raise ValueError("V5 冻结输入文件哈希校验失败")
        attachments = {row.get("name"): row.get("sha256")
                       for row in v5_input.get("attachments") or []}
        if attachments != V5_ATTACHMENT_SHA256:
            raise ValueError("V5 冻结输入附件指纹校验失败")
        for name in ("adjustment_factors", "fundamentals", "industries", "h00922", "strategy_nav"):
            rows = (v5_input.get("inputs") or {}).get(name)
            if not isinstance(rows, list) or (v5_input.get("hashes") or {}).get(name) != _hash(rows):
                raise ValueError(f"V5 冻结输入 {name} 哈希校验失败")
    elif engine == "ma_v22":
        from ma_v22_strategy import MA_V22_ATTACHMENT_SHA256, MA_V22_RULES, load_inputs
        if rules != MA_V22_RULES or metadata.get("attachment_sha256") != MA_V22_ATTACHMENT_SHA256:
            raise ValueError("多资产风险预算 V2.2 完整规则或参考附件指纹与冻结合同不一致")
        if not profile["input_path"].exists():
            raise ValueError("多资产风险预算 V2.2 输入不存在")
        load_inputs(profile["input_path"])
    else:
        frozen = _read_json(freeze_path)
        if frozen.get("version") != "V1" or frozen.get("rules") != V1_RULES:
            raise ValueError("V1 前向参数与历史冻结规则不一致")
        if metadata.get("v1_commit") != frozen.get("git", {}).get("commit"):
            raise ValueError("V1 前向提交与历史冻结提交不一致")
    if profile["shadow"] and not profile.get("independent_rules"):
        changed = {
            key for key in set(V1_RULES) | set(rules)
            if V1_RULES.get(key) != rules.get(key)
        }
        if changed != {"max_holdings"} or rules.get("max_holdings") != profile["max_holdings"]:
            raise ValueError(f"{profile['version']} 只能修改 max_holdings")
        if metadata_path.resolve().parent != SHADOW_DIR.resolve():
            raise ValueError(f"{profile['version']} 元数据必须位于影子目录")

    capital = metadata.get("capital_policy") or {}
    if engine == "v5":
        if (
            capital.get("initial_capital") != 100000.0
            or capital.get("target_exposure_range_pct") != [40, 100]
            or capital.get("idle_cash_interest") is not True
        ):
            raise ValueError("高息动量 V5 必须使用十万元独立账本和 40%-100% 风险敞口")
    elif engine == "ma_v22":
        if capital.get("target_allocation_pct") != 100 or capital.get("cash_reserve") != 0:
            raise ValueError("多资产风险预算 V2.2 必须使用十万元独立账户并按 100% 目标配置")
    else:
        if capital.get("target_allocation_pct") != 100 or capital.get("cash_reserve") != 0:
            raise ValueError("V1 必须按 100% 目标投入且不设置额外现金保留")
        if rules.get("reinvest_cash_reserve") != 0 or rules.get("max_position_pct") != 1.0:
            raise ValueError(f"{profile['version']} 资金参数不符合全量投入合同")

    observation = metadata.get("observation_policy") or {}
    if observation.get("minimum_months") != 6 or observation.get("target_months") != 12:
        raise ValueError("V1 必须冻结观察至少 6 个月、目标 12 个月")
    if observation.get("parameter_changes_allowed") is not False:
        raise ValueError("V1 观察期禁止修改参数")
    if (
        observation.get("v2_mode") != "shadow_only"
        or observation.get("v2_output_root") != "data/forward/shadow"
        or observation.get("v2_can_write_v1_journal") is not False
        or observation.get("shadow_versions") != ["V2", "V3", "V5", "V2.2"]
        or observation.get("shadow_can_write_v1_journal") is not False
    ):
        raise ValueError("影子策略必须保持隔离模式且不能写入 V1 账本")
    return {
        "version": profile["version"],
        "shadow": profile["shadow"],
        "rules_sha256": metadata["rules_sha256"],
        "target_allocation_pct": capital.get("target_allocation_pct"),
        "cash_reserve": capital.get("cash_reserve"),
        "target_exposure_range_pct": capital.get("target_exposure_range_pct"),
        "observation_months": [observation["minimum_months"], observation["target_months"]],
        "v2_mode": observation["v2_mode"],
        "status": "通过",
    }


def initialize(
    metadata_path: Path | None = None,
    journal_path: Path | None = None,
    strategy_id: str = "v1",
) -> None:
    profile = strategy_profile(strategy_id)
    metadata_path = metadata_path or profile["metadata_path"]
    journal_path = journal_path or profile["journal_path"]
    expected = build_metadata(strategy_id)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    if metadata_path.exists() and _read_json(metadata_path) != expected:
        raise ValueError(f"{profile['version']} 元数据已存在但内容不同，拒绝覆盖")
    if not metadata_path.exists():
        metadata_path.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    if not journal_path.exists():
        journal_path.touch()


def record_signal(
    signal_date: str,
    *,
    manifest_path: Path,
    dates_path: Path,
    cache_dir: Path,
    journal_path: Path | None = None,
    metadata_path: Path | None = None,
    strategy_id: str = "v1",
    allow_isolated_journal: bool = False,
    v5_input_path: Path | None = None,
    strategy_input_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    profile = strategy_profile(strategy_id)
    journal_path = journal_path or profile["journal_path"]
    _require_journal_boundary(
        profile, journal_path, allow_isolated_journal=allow_isolated_journal
    )
    rules = profile["rules"]
    verify_forward_contract(metadata_path=metadata_path, strategy_id=strategy_id)
    engine = profile.get("engine")
    input_path = strategy_input_path or v5_input_path or profile.get("input_path")
    if engine == "v5":
        from v5_strategy import build_forward_signal
        event = build_forward_signal(
            signal_date=signal_date,
            manifest_path=manifest_path,
            dates_path=dates_path,
            cache_dir=cache_dir,
            journal_rows=_load_journal(journal_path),
            v5_input_path=input_path,
        )
        return _append_once(journal_path, event)
    if engine == "ma_v22":
        from ma_v22_strategy import build_forward_signal
        event = build_forward_signal(
            signal_date=signal_date,
            dates_path=dates_path,
            journal_rows=_load_journal(journal_path),
            input_path=input_path,
        )
        return _append_once(journal_path, event)
    datetime.strptime(signal_date, "%Y-%m-%d")
    if signal_date < V1_FIRST_SIGNAL_DATE:
        raise ValueError(f"前向信号不得早于 {V1_FIRST_SIGNAL_DATE}")
    loaded, dates = _input_state(manifest_path, dates_path)
    manifest = loaded["manifest"]
    dates_payload = _read_json(dates_path)
    if not isinstance(dates_payload, dict) or str(dates_payload.get("as_of") or "")[:10] != signal_date:
        raise ValueError("信号日期文件截止日必须恰好等于信号日")
    dates_manifest_hash = (dates_payload.get("source") or {}).get("manifest_records_sha256")
    if dates_manifest_hash != manifest["records_sha256"]:
        raise ValueError("信号日期文件没有绑定当前 manifest 哈希")
    if signal_date not in dates:
        raise ValueError("信号日不在版本化月度日期文件中，不能自行猜测月末")
    if manifest["as_of"] != signal_date:
        raise ValueError("信号输入截止日必须恰好等于信号日，不能包含未来数据")
    codes = list(manifest["codes"])
    _require_complete_cache(cache_dir, codes)
    verify_cache_snapshot(manifest, cache_dir)
    calendar = _calendar(cache_dir, codes)
    if signal_date not in calendar:
        raise ValueError("缓存中没有信号日真实收盘价")
    held_codes = _previous_holdings(journal_path, signal_date[:7])
    decision = _decision_snapshot(cache_dir, codes, signal_date, dates, held_codes, rules)
    historical_input = _historical_input_snapshot(cache_dir, codes, signal_date)
    pool_codes = decision["pool_codes"]
    missing = [code for code in pool_codes if signal_date not in _cache_payload(cache_dir, "kl", code, {})]
    event = {
        "schema_version": 1,
        "event_type": "signal",
        "period": signal_date[:7],
        "signal_date": signal_date,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "strategy_id": profile["strategy_id"],
        "strategy_version": profile["version"],
        "shadow": profile["shadow"],
        "v1_commit": V1_COMMIT,
        "rules_sha256": _hash(rules),
        "rules": rules,
        "input": loaded["input"],
        "candidate_pool": {
            "codes": pool_codes,
            "count": len(pool_codes),
            "codes_sha256": decision["pool_codes_sha256"],
        },
        "decision_snapshot": decision,
        "historical_input_snapshot": historical_input,
        "data_gaps": {
            "candidate_codes_missing_signal_close": missing,
            "manifest_codes_missing_cache_files": {},
        },
        "status": "等待下一可用交易日收盘执行",
    }
    event["content_sha256"] = _hash({k: v for k, v in event.items() if k != "recorded_at"})
    return _append_once(journal_path, event)


@contextmanager
def _using_cache(cache_dir: Path) -> Iterator[None]:
    original = backtest.CACHE_DIR
    backtest.CACHE_DIR = cache_dir
    try:
        yield
    finally:
        backtest.CACHE_DIR = original


def record_execution(
    period: str,
    *,
    manifest_path: Path,
    dates_path: Path,
    cache_dir: Path,
    journal_path: Path | None = None,
    metadata_path: Path | None = None,
    strategy_id: str = "v1",
    allow_isolated_journal: bool = False,
    v5_input_path: Path | None = None,
    strategy_input_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    profile = strategy_profile(strategy_id)
    journal_path = journal_path or profile["journal_path"]
    _require_journal_boundary(
        profile, journal_path, allow_isolated_journal=allow_isolated_journal
    )
    profile_rules = profile["rules"]
    verify_forward_contract(metadata_path=metadata_path, strategy_id=strategy_id)
    engine = profile.get("engine")
    input_path = strategy_input_path or v5_input_path or profile.get("input_path")
    if engine == "ma_v22":
        from ma_v22_strategy import build_forward_execution
        event = build_forward_execution(
            period=period,
            journal_rows=_load_journal(journal_path),
            input_path=input_path,
        )
        return _append_once(journal_path, event)
    if engine == "v5":
        rows = _load_journal(journal_path)
        current = next(
            (row for row in rows if row.get("event_type") == "signal" and row.get("period") == period),
            None,
        )
        if current is None:
            raise ValueError(f"{period} 尚无不可回写信号，不能执行")
        loaded, _ = _input_state(manifest_path, dates_path)
        codes = list(loaded["manifest"]["codes"])
        execution_date = backtest._next_trading_date(
            _calendar(cache_dir, codes), current["signal_date"], 1
        )
        if execution_date is None:
            raise ValueError("尚无信号日后的下一可用交易日收盘数据")
        from v5_strategy import build_forward_execution
        event = build_forward_execution(
            period=period,
            execution_date=execution_date,
            cache_dir=cache_dir,
            journal_rows=rows,
            v5_input_path=input_path,
        )
        return _append_once(journal_path, event)
    loaded, dates = _input_state(manifest_path, dates_path)
    signals = [row for row in _load_journal(journal_path) if row.get("event_type") == "signal"]
    signals.sort(key=lambda row: row["signal_date"])
    current = next((row for row in signals if row.get("period") == period), None)
    if current is None:
        raise ValueError(f"{period} 尚无不可回写信号，不能执行")
    if loaded["manifest"]["as_of"] <= current["signal_date"]:
        raise ValueError("执行快照尚未覆盖信号日后的交易日")
    applicable = [row for row in signals if row["signal_date"] <= current["signal_date"]]
    codes = list(loaded["manifest"]["codes"])
    _require_complete_cache(cache_dir, codes)
    verify_cache_snapshot(loaded["manifest"], cache_dir)
    replay_historical_input = _historical_input_snapshot(cache_dir, codes, current["signal_date"])
    if replay_historical_input != current.get("historical_input_snapshot"):
        raise ValueError("扩展执行快照改变了信号日以前的价格或分红，拒绝执行")
    held_codes = set(current.get("decision_snapshot", {}).get("held_codes", []))
    replay_decision = _decision_snapshot(
        cache_dir, codes, current["signal_date"], dates, held_codes, profile_rules
    )
    if replay_decision != current.get("decision_snapshot"):
        raise ValueError("扩展执行快照改变了已冻结信号决策，拒绝执行")
    calendar = _calendar(cache_dir, codes)
    execution_date = backtest._next_trading_date(calendar, current["signal_date"], 1)
    if execution_date is None:
        raise ValueError("尚无信号日后的下一可用交易日收盘数据")
    if execution_date <= current["signal_date"]:
        raise ValueError("执行日必须严格晚于信号日")

    rules = dict(profile_rules)
    rules["through_date"] = loaded["manifest"]["as_of"]
    with _using_cache(cache_dir):
        result = backtest.run_backtest(
            rules=rules,
            codes=codes,
            rebalance_dates=[row["signal_date"] for row in applicable],
            momentum_dates=dates,
            dynamic_pool=True,
            verbose=False,
            track_holdings=True,
            return_events=True,
        )
    provenance = next(
        (row for row in result["pool_provenance"] if row["signal_date"] == current["signal_date"]), None
    )
    if not provenance or provenance.get("execution_date") != execution_date:
        raise ValueError("回放执行日与缓存交易日门禁不一致")
    previous_events = []
    previous_executions = [
        row for row in _load_journal(journal_path)
        if row.get("event_type") == "execution" and row.get("period") < period
    ]
    if previous_executions:
        previous_events = previous_executions[-1].get("cumulative_events", [])
    events = result.get("_events", [])
    if events[:len(previous_events)] != previous_events:
        raise ValueError("历史回放事件发生漂移，拒绝追加执行")
    operations = events[len(previous_events):]
    allowed_buys = held_codes | set(
        current.get("decision_snapshot", {}).get("eligible_entry_codes", [])
    )
    unexpected_buys = sorted({
        str(row.get("code") or "").zfill(6)
        for row in operations
        if row.get("side") in ("买入", "buy")
        and str(row.get("code") or "").zfill(6) not in allowed_buys
    })
    if unexpected_buys:
        raise ValueError(f"执行回放买入了冻结信号未放行的股票: {unexpected_buys}")
    fees = round(sum(float((row.get("fees") or {}).get("total") or 0) for row in operations), 6)
    nav_row = next((row for row in reversed(result["nav_series"]) if row["date"] == execution_date), None)
    if nav_row is None:
        raise ValueError("执行日没有 NAV 记录")
    pool_codes = current["candidate_pool"]["codes"]
    missing_close = [code for code in pool_codes if execution_date not in _cache_payload(cache_dir, "kl", code, {})]
    event = {
        "schema_version": 1,
        "event_type": "execution",
        "period": period,
        "signal_date": current["signal_date"],
        "execution_date": execution_date,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "strategy_id": profile["strategy_id"],
        "strategy_version": profile["version"],
        "shadow": profile["shadow"],
        "v1_commit": V1_COMMIT,
        "rules_sha256": _hash(profile_rules),
        "signal_input": current["input"],
        "execution_input": loaded["input"],
        "candidate_pool": current["candidate_pool"],
        "operations": operations,
        "cumulative_events": events,
        "holdings": result["final_holdings"],
        "cash": nav_row["cash"],
        "fees": fees,
        "nav": nav_row["nav"],
        "data_gaps": {"candidate_codes_missing_execution_close": missing_close},
        "status": "已按缓存真实交易日收盘完成模型执行",
    }
    event["content_sha256"] = _hash({k: v for k, v in event.items() if k != "recorded_at"})
    return _append_once(journal_path, event)


def main() -> int:
    parser = argparse.ArgumentParser(description="五策略只追加前向观察账本")
    parser.add_argument("--strategy", choices=tuple(FORWARD_STRATEGIES), default="v1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化冻结元数据与空账本")
    sub.add_parser("verify", help="校验 V1 冻结、观察期与全量资金合同")
    signal = sub.add_parser("signal", help="追加月末收盘信号")
    signal.add_argument("--date", required=True, help="版本化月末信号日 YYYY-MM-DD")
    execute = sub.add_parser("execute", help="追加下一交易日模拟执行（V2.2 开盘，其余收盘）")
    execute.add_argument("--period", required=True, help="信号月份 YYYY-MM")
    for command in (signal, execute):
        command.add_argument("--manifest", type=Path, default=FORWARD_INPUT_DIR / "universe_manifest.json")
        command.add_argument("--dates", type=Path, default=FORWARD_INPUT_DIR / "rebalance_dates_monthly.json")
        command.add_argument("--cache-dir", type=Path, default=FORWARD_CACHE_DIR)
        command.add_argument("--journal", type=Path)
    args = parser.parse_args()
    profile = strategy_profile(args.strategy)
    if args.command == "init":
        initialize(strategy_id=args.strategy)
        print(f"{profile['version']} 前向观察已初始化；当前没有伪造信号或成交。")
        return 0
    if args.command == "verify":
        print(json.dumps(verify_forward_contract(strategy_id=args.strategy), ensure_ascii=False, indent=2))
        return 0
    journal_path = args.journal or profile["journal_path"]
    if args.command == "signal":
        event, appended = record_signal(
            args.date, manifest_path=args.manifest, dates_path=args.dates,
            cache_dir=args.cache_dir, journal_path=journal_path, strategy_id=args.strategy,
        )
    else:
        event, appended = record_execution(
            args.period, manifest_path=args.manifest, dates_path=args.dates,
            cache_dir=args.cache_dir, journal_path=journal_path, strategy_id=args.strategy,
        )
    print(("已追加" if appended else "幂等：已有相同记录") + f" {event['event_type']} {event['period']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
