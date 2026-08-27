"""为历史上可能进入动态池的新增在市股票采集并核验不复权收盘价。"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Callable

from build_historical_universe import ROOT, canonical_sha256, write_json_atomic
from collect_listed_dividends import SerialHttpClient, read_json
from verify_delisted_prices import (
    arbitrate_value,
    fetch_tencent_dates,
    tencent_checkpoint_is_complete,
    tencent_checkpoint_path,
)


DEFAULT_MASTER = ROOT / "data" / "historical" / "security_master.json"
DEFAULT_DIVIDENDS = ROOT / "data" / "historical" / "listed_dividends.json"
DEFAULT_FROZEN_MANIFEST = ROOT / "data" / "universe_manifest.json"
DEFAULT_SELECTION_SCOPE = ROOT / "data" / "historical" / "selection_relevant_scope.json"
DEFAULT_CHECKPOINT_DIR = ROOT / "data" / "historical" / "checkpoints" / "listed_prices"
DEFAULT_ARCHIVE = ROOT / "data" / "historical" / "eligible_listed_prices.json.gz"
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "eligible_listed_prices_manifest.json"
VERIFICATION_PRICE_PROVIDERS = ("baostock", "sina", "eastmoney")


def max_consecutive_positive_years(records: list[dict[str, Any]]) -> int:
    years = sorted({
        int(row["year"])
        for row in records
        if str(row.get("year") or "").isdigit() and float(row.get("dps") or 0) > 0
    })
    best = current = 0
    previous: int | None = None
    for year in years:
        current = current + 1 if previous is not None and year == previous + 1 else 1
        best = max(best, current)
        previous = year
    return best


def eligible_targets(
    dividends: dict[str, Any],
    master: dict[str, Any],
    frozen_codes: set[str],
    *,
    min_years: int = 3,
    allow_primary_only: bool = False,
) -> list[dict[str, Any]]:
    """取动态池可能用到的安全上界，不用价格做事后筛选。"""
    if dividends.get("primary_provider_complete") is not True:
        raise RuntimeError("在市股票东财主源尚未全量完成，拒绝确定正式价格目标集合")
    if (
        not allow_primary_only
        and dividends.get("eligible_scope_independently_verified") is not True
    ):
        raise RuntimeError("潜在候选分红尚未全部通过独立核验，拒绝确定正式价格目标集合")
    master_by_code = {row["code"]: row for row in master.get("records", [])}
    targets = []
    for stock in dividends.get("stocks", []):
        code = stock["code"]
        if stock.get("verification_required") is not True:
            continue
        if not allow_primary_only and not (
            stock.get("primary_provider_complete")
            and stock.get("verification_provider_complete")
            and stock.get("independently_verified")
        ):
            raise RuntimeError(f"在市股票 {code} 分红未通过逐股双源核验")
        if code in frozen_codes:
            continue
        if max_consecutive_positive_years(stock.get("records", [])) < min_years:
            continue
        master_row = master_by_code.get(code)
        if not master_row:
            raise RuntimeError(f"历史主表缺少 {code}")
        targets.append(master_row)
    return sorted(targets, key=lambda row: row["code"])


def targets_from_codes(
    codes: set[str], master: dict[str, Any], *, scope_name: str,
) -> list[dict[str, Any]]:
    """把已冻结的代码范围映射回证券主表，缺失时失败关闭。"""
    master_by_code = {row["code"]: row for row in master.get("records", [])}
    missing = sorted(codes - master_by_code.keys())
    if missing:
        raise RuntimeError(f"证券主表缺少{scope_name}代码: {', '.join(missing[:20])}")
    return [master_by_code[code] for code in sorted(codes)]


def verified_decision_targets(
    dividends: dict[str, Any], master: dict[str, Any],
) -> list[dict[str, Any]]:
    """正式价格产物只覆盖通过人工数据门禁的决策路径股票。"""
    if dividends.get("manual_data_gate_complete") is not True:
        raise RuntimeError("在市分红人工数据门禁尚未完成，拒绝构建正式价格产物")
    codes = set(dividends.get("data_quality_eligible_codes") or [])
    if not codes:
        raise RuntimeError("在市分红人工数据门禁没有放行任何股票")
    return targets_from_codes(codes, master, scope_name="分红门禁放行")


def normalize_price_rows(rows: dict[str, Any], start_date: str, as_of: str) -> list[dict[str, Any]]:
    normalized = []
    for day, value in rows.items():
        day = str(day)[:10]
        if not (start_date <= day <= as_of):
            continue
        try:
            close = float(value)
        except (TypeError, ValueError):
            continue
        if close > 0:
            normalized.append({"date": day, "close": round(close, 4)})
    return sorted(normalized, key=lambda row: row["date"])


def fetch_baostock_prices(
    code: str, start_date: str, as_of: str, bs_module: Any,
) -> dict[str, Any]:
    source_code = ("sh." if code.startswith(("5", "6", "9")) else "sz.") + code
    result = bs_module.query_history_k_data_plus(
        source_code,
        "date,close,tradestatus",
        start_date=start_date,
        end_date=as_of,
        frequency="d",
        adjustflag="3",
    )
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock 行情失败: {result.error_code} {result.error_msg}")
    rows: dict[str, float] = {}
    source_rows = []
    while result.next():
        row = dict(zip(result.fields, result.get_row_data()))
        source_rows.append(row)
        if row.get("tradestatus") != "1":
            continue
        try:
            close = float(row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close > 0:
            rows[row["date"]] = close
    normalized = normalize_price_rows(rows, start_date, as_of)
    if not normalized:
        raise RuntimeError("BaoStock 查询成功但没有有效交易日")
    return {
        "provider_complete": True,
        "source": "baostock/query_history_k_data_plus",
        "qualification_status": "provisional_primary_dividend_scope",
        "source_rows_sha256": canonical_sha256(source_rows),
        "rows": normalized,
    }


def fetch_sina_prices(
    code: str, start_date: str, as_of: str, client: SerialHttpClient,
) -> dict[str, Any]:
    market = "sh" if code.startswith("6") else "sz"
    response = client.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        params={"symbol": market + code, "scale": "240", "ma": "no", "datalen": "5000"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("新浪行情响应结构异常")
    rows = {
        str(row.get("day") or row.get("date") or "")[:10]: row.get("close")
        for row in payload
    }
    normalized = normalize_price_rows(rows, start_date, as_of)
    if not normalized:
        raise RuntimeError("新浪查询成功但没有有效交易日")
    return {
        "provider_complete": True,
        "source": "sina/CN_MarketData.getKLineData",
        "source_rows_sha256": canonical_sha256(payload),
        "rows": normalized,
    }


def fetch_eastmoney_prices(
    code: str, start_date: str, as_of: str, client: SerialHttpClient,
) -> dict[str, Any]:
    """东财不复权日线，只用于 selection scope 的第二来源核验。"""
    market = "1" if code.startswith("6") else "0"
    response = client.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": f"{market}.{code}",
            "fields1": "f1",
            "fields2": "f51,f52,f53,f54,f55,f56",
            "klt": "101",
            "fqt": "0",
            "beg": start_date.replace("-", ""),
            "end": as_of.replace("-", ""),
            "lmt": "10000",
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    raw_rows = ((payload.get("data") or {}).get("klines") or [])
    rows = {}
    for raw in raw_rows:
        fields = str(raw).split(",")
        if len(fields) >= 3:
            rows[fields[0][:10]] = fields[2]
    normalized = normalize_price_rows(rows, start_date, as_of)
    if not normalized:
        raise RuntimeError("东财查询成功但没有有效交易日")
    return {
        "provider_complete": True,
        "source": "eastmoney/push2his klt=101 fqt=0",
        "source_rows_sha256": canonical_sha256(raw_rows),
        "rows": normalized,
    }


def decode_tonghuashun_prices(
    payload: dict[str, Any], start_date: str, as_of: str,
) -> list[dict[str, Any]]:
    """解码同花顺未复权全历史日线的压缩价格字段。"""
    try:
        total = int(payload["total"])
        price_factor = float(payload["priceFactor"])
        year_counts = [(int(year), int(count)) for year, count in payload["sortYear"]]
        month_days = [value for value in str(payload["dates"]).split(",") if value]
        prices = [int(value) for value in str(payload["price"]).split(",") if value]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"同花顺全历史响应字段无效: {exc}") from exc
    if total <= 0 or price_factor <= 0:
        raise RuntimeError("同花顺全历史响应没有有效价格记录")
    if sum(count for _, count in year_counts) != total:
        raise RuntimeError("同花顺全历史年度计数与 total 不一致")
    if len(prices) != len(month_days) * 4:
        raise RuntimeError("同花顺全历史价格字段数量与日期数量不一致")
    # 盘中接口会把当天先计入 total/当年计数，但尚未写入压缩日期和价格。
    pending_tail_rows = total - len(month_days)
    if pending_tail_rows not in {0, 1}:
        raise RuntimeError("同花顺全历史日期数量与 total 不一致")
    if pending_tail_rows:
        year, count = year_counts[-1]
        if count < pending_tail_rows:
            raise RuntimeError("同花顺全历史末年计数无效")
        year_counts[-1] = (year, count - pending_tail_rows)

    dates: list[str] = []
    offset = 0
    for year, count in year_counts:
        for month_day in month_days[offset:offset + count]:
            if len(month_day) != 4 or not month_day.isdigit():
                raise RuntimeError(f"同花顺全历史日期片段无效: {month_day!r}")
            day = f"{year:04d}-{month_day[:2]}-{month_day[2:]}"
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise RuntimeError(f"同花顺全历史日期无效: {day}") from exc
            dates.append(day)
        offset += count
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise RuntimeError("同花顺全历史日期未严格递增或存在重复")

    rows = {}
    for index, day in enumerate(dates):
        low = prices[index * 4]
        close_delta = prices[index * 4 + 3]
        rows[day] = (low + close_delta) / price_factor
    return normalize_price_rows(rows, start_date, as_of)


def fetch_tonghuashun_prices(
    code: str, start_date: str, as_of: str, client: SerialHttpClient,
) -> dict[str, Any]:
    """同花顺 00 口径未复权全历史日线，用于全候选快速初筛。"""
    response = client.get(
        f"https://d.10jqka.com.cn/v6/line/hs_{code}/00/all.js",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://stockpage.10jqka.com.cn/{code}/",
        },
        timeout=30,
    )
    response.raise_for_status()
    raw = response.text.strip()
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right <= left:
        raise RuntimeError("同花顺全历史响应不是有效 JSONP")
    try:
        payload = json.loads(raw[left + 1:right])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"同花顺全历史 JSON 解析失败: {exc}") from exc
    rows = decode_tonghuashun_prices(payload, start_date, as_of)
    if not rows:
        raise RuntimeError("同花顺查询成功但没有有效交易日")
    return {
        "provider_complete": True,
        "source": "tonghuashun/v6 line 00 all.js",
        "source_rows_sha256": canonical_sha256({
            key: payload.get(key)
            for key in ("start", "total", "sortYear", "priceFactor", "price", "dates")
        }),
        "rows": rows,
    }


def price_checkpoint_path(base: Path, provider: str, code: str) -> Path:
    return base / provider / f"{code}.json"


def price_checkpoint_is_complete(
    payload: dict[str, Any], provider: str, code: str, start_date: str, as_of: str,
) -> bool:
    rows = payload.get("rows")
    if not (
        payload.get("schema_version") == 1
        and payload.get("provider") == provider
        and payload.get("code") == code
        and payload.get("start_date") == start_date
        and payload.get("as_of") == as_of
        and payload.get("price_format") == "unadjusted_close"
        and payload.get("provider_complete") is True
        and isinstance(rows, list)
        and rows
        and payload.get("rows_sha256") == canonical_sha256(rows)
    ):
        return False
    dates = [row.get("date") for row in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        return False
    for row in rows:
        if set(row) != {"date", "close"} or not (start_date <= row["date"] <= as_of):
            return False
        try:
            close = float(row["close"])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(close) or close <= 0:
            return False
    return True


def collect_price_provider(
    targets: list[dict[str, Any]],
    checkpoint_dir: Path,
    provider: str,
    as_of: str,
    fetcher: Callable[[str, str, str], dict[str, Any]],
    *,
    retry: int = 2,
    limit: int = 0,
    max_consecutive_failures: int = 5,
) -> dict[str, int]:
    attempted = skipped = succeeded = failed = 0
    consecutive_failures = 0
    for index, stock in enumerate(targets, 1):
        start_date = max("2015-01-01", stock["list_date"])
        path = price_checkpoint_path(checkpoint_dir, provider, stock["code"])
        if path.exists():
            try:
                existing = read_json(path)
            except (OSError, json.JSONDecodeError):
                existing = {}
            if price_checkpoint_is_complete(
                existing, provider, stock["code"], start_date, as_of
            ):
                skipped += 1
                continue
        if limit and attempted >= limit:
            break
        attempted += 1
        result: dict[str, Any] = {}
        error = ""
        for _ in range(max(retry, 0) + 1):
            try:
                result = fetcher(stock["code"], start_date, as_of)
                error = ""
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        rows = result.get("rows", []) if not error else []
        payload = {
            "schema_version": 1,
            "provider": provider,
            "code": stock["code"],
            "name": stock["name"],
            "list_date": stock["list_date"],
            "start_date": start_date,
            "as_of": as_of,
            "price_format": "unadjusted_close",
            "provider_complete": bool(result.get("provider_complete")) and not error,
            "error": error,
            "row_count": len(rows),
            "rows": rows,
            "rows_sha256": canonical_sha256(rows),
            "source_metadata": {
                key: value for key, value in result.items()
                if key not in {"provider_complete", "rows"}
            },
        }
        write_json_atomic(path, payload)
        if payload["provider_complete"]:
            succeeded += 1
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1
        if attempted % 25 == 0 or index == len(targets):
            print(
                f"{provider} 价格 {index}/{len(targets)}，"
                f"新完成 {succeeded}，失败 {failed}，跳过 {skipped}"
            )
        if (
            max_consecutive_failures > 0
            and consecutive_failures >= max_consecutive_failures
        ):
            raise RuntimeError(
                f"{provider} 连续 {consecutive_failures} 只采集失败，"
                "可能已被限流或服务中断，已停止继续请求"
            )
    return {"attempted": attempted, "skipped": skipped, "succeeded": succeeded, "failed": failed}


def collect_tencent_arbitration(
    targets: list[dict[str, Any]],
    checkpoint_dir: Path,
    as_of: str,
    fetcher: Callable[[str, list[str]], dict[str, Any]],
    *,
    retry: int = 2,
    max_consecutive_failures: int = 5,
) -> dict[str, int]:
    """只查询同花顺与新浪不一致的日期，并保存腾讯仲裁断点。"""
    attempted = skipped = succeeded = failed = 0
    consecutive_failures = 0
    for stock in targets:
        code = stock["code"]
        start_date = max("2015-01-01", stock["list_date"])
        primary_path = price_checkpoint_path(checkpoint_dir, "tonghuashun", code)
        verifier_path = price_checkpoint_path(checkpoint_dir, "sina", code)
        primary = read_json(primary_path) if primary_path.exists() else {}
        verifier = read_json(verifier_path) if verifier_path.exists() else {}
        if not price_checkpoint_is_complete(
            primary, "tonghuashun", code, start_date, as_of
        ) or not price_checkpoint_is_complete(
            verifier, "sina", code, start_date, as_of
        ):
            raise RuntimeError(f"{code} 缺少完整的同花顺或新浪价格断点")
        primary_map = {
            row["date"]: row["close"]
            for row in _price_projection(primary["rows"])
        }
        verifier_map = {
            row["date"]: row["close"]
            for row in _price_projection(verifier["rows"])
        }
        dates = sorted(
            (primary_map.keys() ^ verifier_map.keys())
            | {
                day for day in primary_map.keys() & verifier_map.keys()
                if primary_map[day] != verifier_map[day]
            }
        )
        if not dates:
            continue
        path = tencent_checkpoint_path(checkpoint_dir, code)
        existing = read_json(path) if path.exists() else {}
        if tencent_checkpoint_is_complete(existing, code, dates):
            skipped += 1
            continue
        attempted += 1
        result: dict[str, Any] = {}
        error = ""
        for _ in range(max(retry, 0) + 1):
            try:
                result = fetcher(code, dates)
                error = ""
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        values = result.get("values", {}) if not error else {}
        payload = {
            "schema_version": 1,
            "provider": "tencent_raw_daily",
            "source": "tencent/appstock/app/kline/kline day（无复权参数）",
            "code": code,
            "requested_dates": dates,
            "provider_complete": bool(result.get("provider_complete")) and not error,
            "error": error,
            "values": values,
            "values_sha256": canonical_sha256(values),
            "response_hashes": result.get("response_hashes", {}),
        }
        write_json_atomic(path, payload)
        if payload["provider_complete"]:
            succeeded += 1
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1
        if (
            max_consecutive_failures > 0
            and consecutive_failures >= max_consecutive_failures
        ):
            raise RuntimeError(
                f"腾讯仲裁连续 {consecutive_failures} 只失败，已停止继续请求"
            )
    return {
        "attempted": attempted,
        "skipped": skipped,
        "succeeded": succeeded,
        "failed": failed,
    }


def _price_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"date": row["date"], "close": round(float(row["close"]), 2)}
        for row in rows
    ]


def write_gzip_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with temporary.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as compressed:
            compressed.write(raw)
    temporary.replace(path)


def read_gzip_json(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def build_price_artifact(
    targets: list[dict[str, Any]],
    checkpoint_dir: Path,
    archive_path: Path,
    as_of: str,
    *,
    primary_provider: str = "tonghuashun",
    verification_providers: tuple[str, ...] = VERIFICATION_PRICE_PROVIDERS,
) -> dict[str, Any]:
    if not verification_providers:
        raise ValueError("至少需要一个独立价格核验来源")
    stocks = []
    for stock in targets:
        start_date = max("2015-01-01", stock["list_date"])
        primary_path = price_checkpoint_path(checkpoint_dir, primary_provider, stock["code"])
        primary = read_json(primary_path) if primary_path.exists() else {}
        primary_ok = price_checkpoint_is_complete(
            primary, primary_provider, stock["code"], start_date, as_of
        )
        primary_rows = _price_projection(primary.get("rows", [])) if primary_ok else []
        primary_map = {row["date"]: row["close"] for row in primary_rows}
        verifier_options = []
        for provider in verification_providers:
            verifier_path = price_checkpoint_path(
                checkpoint_dir, provider, stock["code"]
            )
            verifier = read_json(verifier_path) if verifier_path.exists() else {}
            verifier_ok = price_checkpoint_is_complete(
                verifier, provider, stock["code"], start_date, as_of
            )
            verifier_rows = (
                _price_projection(verifier.get("rows", []))
                if verifier_ok else []
            )
            verifier_map = {
                row["date"]: row["close"] for row in verifier_rows
            }
            verifier_options.append({
                "provider": provider,
                "provider_complete": verifier_ok,
                "rows": verifier_rows,
                "map": verifier_map,
                "exact_match": (
                    primary_ok and verifier_ok and primary_map == verifier_map
                ),
            })
        exact_option = next(
            (item for item in verifier_options if item["exact_match"]), None
        )
        verifier_option = exact_option or next(
            (
                item for item in verifier_options
                if item["provider"] == "sina" and item["provider_complete"]
            ),
            next(
                (item for item in verifier_options if item["provider_complete"]),
                verifier_options[0],
            ),
        )
        verifier_ok = verifier_option["provider_complete"]
        verifier_rows = verifier_option["rows"]
        verifier_map = verifier_option["map"]
        exact_match = verifier_option["exact_match"]
        dispute_dates = sorted(
            (primary_map.keys() ^ verifier_map.keys())
            | {
                day for day in primary_map.keys() & verifier_map.keys()
                if primary_map[day] != verifier_map[day]
            }
        ) if primary_ok and verifier_ok else []
        arbitration_path = tencent_checkpoint_path(checkpoint_dir, stock["code"])
        arbitration = (
            read_json(arbitration_path) if arbitration_path.exists() else {}
        )
        arbitration_ok = bool(dispute_dates) and tencent_checkpoint_is_complete(
            arbitration, stock["code"], dispute_dates
        )
        decisions = []
        accepted_map = dict(primary_map)
        if dispute_dates:
            tencent_values = arbitration.get("values", {}) if arbitration_ok else {}
            for day in dispute_dates:
                decision = arbitrate_value(
                    primary_map.get(day),
                    verifier_map.get(day),
                    tencent_values.get(day) if arbitration_ok else None,
                )
                if decision.get("resolved"):
                    provider_names = {
                        "baostock": primary_provider,
                        "sina": verifier_option["provider"],
                        "tencent": "tencent",
                    }
                    decision["majority_sources"] = [
                        provider_names.get(name, name)
                        for name in decision.get("majority_sources", [])
                    ]
                decisions.append({
                    "date": day,
                    "tonghuashun_close": primary_map.get(day),
                    "verification_close": verifier_map.get(day),
                    "tencent_close": (
                        tencent_values.get(day) if arbitration_ok else None
                    ),
                    **decision,
                })
        independently_verified = exact_match or (
            verifier_ok and arbitration_ok and bool(decisions)
            and all(decision["resolved"] for decision in decisions)
        )
        if independently_verified and decisions:
            for decision in decisions:
                day = decision["date"]
                accepted = decision["accepted_value"]
                if accepted is None:
                    accepted_map.pop(day, None)
                else:
                    accepted_map[day] = round(float(accepted), 4)
        canonical_rows = [
            {"date": day, "close": close}
            for day, close in sorted(accepted_map.items())
        ]
        exclusion_reason = ""
        if not primary_ok:
            exclusion_reason = "primary_price_unavailable"
        elif not verifier_ok:
            exclusion_reason = "verification_price_unavailable"
        elif not independently_verified:
            exclusion_reason = (
                "price_arbitration_unavailable"
                if dispute_dates and not arbitration_ok
                else "independent_price_mismatch_unresolved"
            )
        stocks.append({
            "code": stock["code"],
            "name": stock["name"],
            "list_date": stock["list_date"],
            "start_date": start_date,
            "end_date": as_of,
            "price_format": "unadjusted_close",
            "rows": canonical_rows if primary_ok else [],
            "rows_sha256": canonical_sha256(canonical_rows),
            "provider_complete": primary_ok,
            "independently_verified": independently_verified,
            "data_quality_eligible": independently_verified,
            "exclusion_reason": exclusion_reason,
            "verification": {
                "provider": verifier_option["provider"],
                "provider_complete": verifier_ok,
                "exact_match": exact_match,
                "primary_row_count": len(primary_rows),
                "verification_row_count": len(verifier_rows),
                "missing_date_count": len(primary_map.keys() - verifier_map.keys()),
                "extra_date_count": len(verifier_map.keys() - primary_map.keys()),
                "different_close_count": sum(
                    primary_map[day] != verifier_map[day]
                    for day in primary_map.keys() & verifier_map.keys()
                ),
            },
            "arbitration": {
                "provider": "tencent_raw_daily",
                "provider_complete": exact_match or arbitration_ok,
                "requested_date_count": len(dispute_dates),
                "resolved": independently_verified,
                "changed_date_count": sum(
                    decision.get("resolved")
                    and decision.get("accepted_value")
                    != decision.get("tonghuashun_close")
                    for decision in decisions
                ),
                "decisions": decisions,
            },
        })
    archive_payload = {
        "schema_version": 1,
        "source_snapshot_date": as_of,
        "price_format": "unadjusted_close",
        "stocks": stocks,
    }
    write_gzip_json_atomic(archive_path, archive_payload)
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    try:
        portable_archive_path = archive_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        portable_archive_path = archive_path.as_posix()
    provider_complete = all(
        item["provider_complete"]
        and item["verification"]["provider_complete"]
        for item in stocks
    )
    arbitration_complete = all(
        item["arbitration"]["provider_complete"] for item in stocks
    )
    independently_verified = bool(stocks) and all(item["independently_verified"] for item in stocks)
    eligible_codes = [item["code"] for item in stocks if item["data_quality_eligible"]]
    filtered_codes = [item["code"] for item in stocks if not item["data_quality_eligible"]]
    return {
        "schema_version": 1,
        "source_snapshot_date": as_of,
        "scope": "历史上曾连续三年正分红且不在冻结 V1 210 只缓存中的在市股票",
        "price_format": "unadjusted_close",
        "primary_source": primary_provider,
        "verification_sources": list(verification_providers),
        "arbitration_source": "tencent_raw_daily",
        "target_count": len(stocks),
        "target_codes_sha256": canonical_sha256([stock["code"] for stock in stocks]),
        "provider_complete": provider_complete,
        "arbitration_complete": arbitration_complete,
        "independently_verified": independently_verified,
        "eligible_scope_independently_verified": bool(eligible_codes),
        "manual_data_gate_complete": True,
        "manual_data_gate_status": (
            "complete_with_exclusions" if filtered_codes else "complete"
        ),
        "data_quality_eligible_count": len(eligible_codes),
        "data_quality_eligible_codes": eligible_codes,
        "data_quality_eligible_codes_sha256": canonical_sha256(eligible_codes),
        "filtered_unverifiable_count": len(filtered_codes),
        "filtered_unverifiable_codes": filtered_codes,
        "filtered_unverifiable_codes_sha256": canonical_sha256(filtered_codes),
        "verified_stock_count": sum(item["independently_verified"] for item in stocks),
        "mismatched_stock_count": sum(
            item["provider_complete"] and item["verification"]["provider_complete"]
            and not item["independently_verified"]
            for item in stocks
        ),
        "failed_stock_count": sum(
            not item["provider_complete"] or not item["verification"]["provider_complete"]
            for item in stocks
        ),
        "row_count": sum(len(item["rows"]) for item in stocks),
        "arbitration_date_count": sum(
            item["arbitration"]["requested_date_count"] for item in stocks
        ),
        "changed_date_count": sum(
            item["arbitration"]["changed_date_count"] for item in stocks
        ),
        "archive": {
            "path": portable_archive_path,
            "sha256": archive_sha256,
            "compression": "gzip-json-mtime-0",
        },
        "stocks": [{
            "code": item["code"],
            "row_count": len(item["rows"]),
            "rows_sha256": item["rows_sha256"],
            "independently_verified": item["independently_verified"],
            "data_quality_eligible": item["data_quality_eligible"],
            "exclusion_reason": item["exclusion_reason"],
            "verification": item["verification"],
            "arbitration": item["arbitration"],
        } for item in stocks],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=(
            "baostock", "sina", "tonghuashun", "eastmoney", "tencent", "build"
        ),
        required=True,
    )
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--dividends", type=Path, default=DEFAULT_DIVIDENDS)
    parser.add_argument("--frozen-manifest", type=Path, default=DEFAULT_FROZEN_MANIFEST)
    parser.add_argument("--selection-scope", type=Path, default=DEFAULT_SELECTION_SCOPE)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT_DIR)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sina-interval", type=float, default=0.20)
    parser.add_argument("--tonghuashun-interval", type=float, default=0.05)
    parser.add_argument("--eastmoney-interval", type=float, default=1.05)
    parser.add_argument("--tencent-interval", type=float, default=0.05)
    parser.add_argument(
        "--phase", choices=("scan", "verify"), default="scan",
        help="scan 为全候选快速初筛；verify 只采集 selection scope 中的股票",
    )
    parser.add_argument("--shard-count", type=int, default=1, help="并行分片总数")
    parser.add_argument("--shard-index", type=int, default=0, help="当前分片编号，从 0 开始")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    master = read_json(args.master)
    dividends = read_json(args.dividends)
    frozen = read_json(args.frozen_manifest)
    if args.provider == "build":
        targets = verified_decision_targets(dividends, master)
    elif args.phase == "scan":
        # 同花顺初筛同时覆盖冻结 V1 股票，便于最终和东财做完整双源价格门禁。
        excluded_frozen = set() if args.provider == "tonghuashun" else set(frozen["codes"])
        targets = eligible_targets(
            dividends, master, excluded_frozen, allow_primary_only=True
        )
    else:
        if not args.selection_scope.exists():
            raise RuntimeError("缺少 selection_relevant_scope.json，拒绝启动正式价格核验")
        selection_scope = read_json(args.selection_scope)
        if (
            selection_scope.get("status") not in {"complete", "complete_with_exclusions"}
            or selection_scope.get("manual_data_gate_complete") is not True
        ):
            raise RuntimeError("selection_relevant_scope 尚未完整，拒绝启动正式价格核验")
        selection_codes = set(
            selection_scope.get("selection_relevant_codes") or []
        )
        dividend_targets = verified_decision_targets(dividends, master)
        dividend_codes = {stock["code"] for stock in dividend_targets}
        outside_scope = sorted(dividend_codes - selection_codes)
        if outside_scope:
            raise RuntimeError(
                "分红门禁放行代码超出 selection scope: "
                + ", ".join(outside_scope[:20])
            )
        targets = [
            stock for stock in dividend_targets
            if stock["code"] in selection_codes
        ]
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("shard-count 必须大于 0，且 shard-index 必须在有效范围内")
    all_target_count = len(targets)
    targets = [
        stock for index, stock in enumerate(targets)
        if index % args.shard_count == args.shard_index
    ]
    print(
        f"价格目标 {all_target_count} 只；当前分片 "
        f"{args.shard_index + 1}/{args.shard_count} 共 {len(targets)} 只"
    )
    if args.provider == "baostock":
        import baostock as bs

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败: {login.error_code} {login.error_msg}")
        try:
            result = collect_price_provider(
                targets,
                args.checkpoint_dir,
                "baostock",
                args.as_of,
            lambda code, start, end: fetch_baostock_prices(code, start, end, bs),
                retry=args.retry,
                limit=args.limit,
            )
        finally:
            bs.logout()
        print(json.dumps({"baostock": result}, ensure_ascii=False))
    if args.provider == "sina":
        client = SerialHttpClient(args.sina_interval, 0.10)
        result = collect_price_provider(
            targets,
            args.checkpoint_dir,
            "sina",
            args.as_of,
            lambda code, start, end: fetch_sina_prices(code, start, end, client),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"sina": result}, ensure_ascii=False))
    if args.provider == "tonghuashun":
        client = SerialHttpClient(args.tonghuashun_interval, 0.05)
        result = collect_price_provider(
            targets,
            args.checkpoint_dir,
            "tonghuashun",
            args.as_of,
            lambda code, start, end: fetch_tonghuashun_prices(
                code, start, end, client
            ),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"tonghuashun": result}, ensure_ascii=False))
    if args.provider == "eastmoney":
        if args.phase != "verify":
            raise RuntimeError("东财价格只允许用于 selection scope 的第二来源核验")
        client = SerialHttpClient(args.eastmoney_interval, 0.20)
        result = collect_price_provider(
            targets,
            args.checkpoint_dir,
            "eastmoney",
            args.as_of,
            lambda code, start, end: fetch_eastmoney_prices(code, start, end, client),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"eastmoney": result}, ensure_ascii=False))
    if args.provider == "tencent":
        if args.phase != "verify":
            raise RuntimeError("腾讯只允许用于价格差异日的第三源仲裁")
        client = SerialHttpClient(args.tencent_interval, 0.02)
        result = collect_tencent_arbitration(
            targets,
            args.checkpoint_dir,
            args.as_of,
            lambda code, dates: fetch_tencent_dates(code, dates, client),
            retry=args.retry,
        )
        print(json.dumps({"tencent": result}, ensure_ascii=False))
    if args.provider == "build":
        manifest = build_price_artifact(
            targets, args.checkpoint_dir, args.archive, args.as_of,
            primary_provider="tonghuashun",
        )
        write_json_atomic(args.output, manifest)
        print(json.dumps({
            "output": str(args.output),
            "target_count": manifest["target_count"],
            "provider_complete": manifest["provider_complete"],
            "independently_verified": manifest["independently_verified"],
            "manual_data_gate_complete": manifest["manual_data_gate_complete"],
            "data_quality_eligible_count": manifest["data_quality_eligible_count"],
            "filtered_unverifiable_count": manifest["filtered_unverifiable_count"],
            "verified_stock_count": manifest["verified_stock_count"],
            "mismatched_stock_count": manifest["mismatched_stock_count"],
            "failed_stock_count": manifest["failed_stock_count"],
            "arbitration_date_count": manifest["arbitration_date_count"],
            "changed_date_count": manifest["changed_date_count"],
        }, ensure_ascii=False, indent=2))
        if (
            not manifest["manual_data_gate_complete"]
            or not manifest["eligible_scope_independently_verified"]
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
