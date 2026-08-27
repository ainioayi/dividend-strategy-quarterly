"""用新浪独立核验退市候选的不复权日收盘价。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from build_historical_universe import ROOT, canonical_sha256, write_json_atomic
from collect_listed_dividends import SerialHttpClient, read_json


DEFAULT_INPUT = ROOT / "data" / "historical" / "eligible_delisted_prices.json"
DEFAULT_CHECKPOINT_DIR = (
    ROOT / "data" / "historical" / "checkpoints" / "delisted_price_verification"
)
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "delisted_price_verification.json"
DEFAULT_VERIFIED_OUTPUT = (
    ROOT / "data" / "historical" / "eligible_delisted_prices_verified.json"
)
SINA_ENDPOINT = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
TENCENT_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"


def validate_rows(rows: Any, *, allow_empty: bool = False) -> bool:
    if not isinstance(rows, list) or (not rows and not allow_empty):
        return False
    dates: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"date", "close"}:
            return False
        day = row.get("date")
        if not isinstance(day, str) or len(day) != 10:
            return False
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(close) or close <= 0:
            return False
        dates.append(day)
    return dates == sorted(dates) and len(dates) == len(set(dates))


def primary_rows(stock: dict[str, Any]) -> list[dict[str, Any]]:
    rows = stock.get("rows")
    if not validate_rows(rows, allow_empty=True):
        raise ValueError(f"主输入 {stock.get('code')} 含重复日期或非正收盘价")
    if stock.get("row_count") != len(rows):
        raise ValueError(f"主输入 {stock.get('code')} 的 row_count 不一致")
    normalized = [
        {"date": row["date"], "close": round(float(row["close"]), 4)} for row in rows
    ]
    expected = stock.get("rows_sha256")
    if expected and expected != canonical_sha256(rows):
        raise ValueError(f"主输入 {stock.get('code')} 的 rows_sha256 不一致")
    return normalized


def fetch_sina(
    code: str, start_date: str, end_date: str, client: SerialHttpClient,
) -> dict[str, Any]:
    market = "sh" if code.startswith("6") else "sz"
    response = client.get(
        SINA_ENDPOINT,
        params={"symbol": market + code, "scale": "240", "ma": "no", "datalen": "5000"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("新浪行情响应不是列表")

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    raw_dates: list[str] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise RuntimeError("新浪行情行不是对象")
        day = str(raw.get("day") or raw.get("date") or "")[:10]
        if not day:
            raise RuntimeError("新浪行情缺少日期")
        if day in seen:
            raise RuntimeError(f"新浪行情存在重复日期: {day}")
        seen.add(day)
        raw_dates.append(day)
        if not start_date or not end_date or not (start_date <= day <= end_date):
            continue
        try:
            close = float(raw.get("close"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"新浪 {day} 收盘价无效") from exc
        if not math.isfinite(close) or close <= 0:
            raise RuntimeError(f"新浪 {day} 收盘价非正或非有限")
        rows.append({"date": day, "close": round(close, 4)})
    rows.sort(key=lambda row: row["date"])
    if not payload:
        raise RuntimeError("新浪返回空列表，无法证明代码有效或目标窗口为空")
    return {
        "provider_complete": True,
        "source": "sina/CN_MarketData.getKLineData",
        "raw_row_count": len(payload),
        "raw_first_date": min(raw_dates),
        "raw_last_date": max(raw_dates),
        "raw_response_sha256": canonical_sha256(payload),
        "rows": rows,
    }


def checkpoint_path(base: Path, code: str) -> Path:
    return base / "sina" / f"{code}.json"


def tencent_checkpoint_path(base: Path, code: str) -> Path:
    return base / "tencent" / f"{code}.json"


def fetch_tencent_dates(
    code: str, dates: list[str], client: SerialHttpClient,
) -> dict[str, Any]:
    """逐日查询腾讯原始日线；空数组表示该日没有该股票交易。"""
    symbol = ("sh" if code.startswith("6") else "sz") + code
    values: dict[str, float | None] = {}
    response_hashes: dict[str, str] = {}
    for day in dates:
        response = client.get(
            TENCENT_ENDPOINT,
            params={"param": f"{symbol},day,{day},{day},2"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"腾讯原始日线拒绝: code={payload.get('code')} msg={payload.get('msg')}")
        node = payload.get("data", {}).get(symbol)
        if not isinstance(node, dict) or not isinstance(node.get("day"), list):
            raise RuntimeError("腾讯原始日线响应结构异常")
        rows = node["day"]
        if len(rows) > 1:
            raise RuntimeError(f"腾讯单日查询返回多行: {day}")
        if not rows:
            values[day] = None
        else:
            row = rows[0]
            if not isinstance(row, list) or len(row) < 5 or row[0] != day:
                raise RuntimeError(f"腾讯单日行情日期或字段异常: {day}")
            try:
                close = float(row[2])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"腾讯 {day} 收盘价无效") from exc
            if not math.isfinite(close) or close <= 0:
                raise RuntimeError(f"腾讯 {day} 收盘价非正或非有限")
            values[day] = round(close, 4)
        response_hashes[day] = canonical_sha256(payload)
    return {"provider_complete": True, "values": values, "response_hashes": response_hashes}


def tencent_checkpoint_is_complete(
    payload: dict[str, Any], code: str, requested_dates: list[str],
) -> bool:
    values = payload.get("values")
    return bool(
        payload.get("schema_version") == 1
        and payload.get("provider") == "tencent_raw_daily"
        and payload.get("code") == code
        and payload.get("requested_dates") == requested_dates
        and payload.get("provider_complete") is True
        and isinstance(values, dict)
        and sorted(values) == requested_dates
        and all(
            value is None
            or (isinstance(value, (int, float)) and math.isfinite(value) and value > 0)
            for value in values.values()
        )
        and payload.get("values_sha256") == canonical_sha256(values)
    )


def dispute_dates(stock: dict[str, Any], sina: dict[str, Any]) -> list[str]:
    primary = {row["date"]: round(float(row["close"]), 4) for row in primary_rows(stock)}
    verifier = {
        row["date"]: round(float(row["close"]), 4) for row in sina.get("rows", [])
    }
    return sorted(
        (primary.keys() ^ verifier.keys())
        | {day for day in primary.keys() & verifier.keys() if primary[day] != verifier[day]}
    )


def collect_tencent_arbitration(
    stocks: list[dict[str, Any]], checkpoint_dir: Path,
    fetcher: Callable[[str, list[str]], dict[str, Any]], *, retry: int = 2,
) -> dict[str, int]:
    succeeded = failed = skipped = 0
    for stock in stocks:
        code = stock["code"]
        sina_path = checkpoint_path(checkpoint_dir, code)
        sina = read_json(sina_path) if sina_path.exists() else {}
        dates = dispute_dates(stock, sina) if sina.get("provider_complete") else []
        if not dates:
            continue
        path = tencent_checkpoint_path(checkpoint_dir, code)
        if path.exists():
            try:
                existing = read_json(path)
            except (OSError, json.JSONDecodeError):
                existing = {}
            if tencent_checkpoint_is_complete(existing, code, dates):
                skipped += 1
                continue
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
        else:
            failed += 1
    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


def arbitrate_value(primary: float | None, sina: float | None, tencent: float | None) -> dict[str, Any]:
    values = {"baostock": primary, "sina": sina, "tencent": tencent}
    groups: dict[float | None, list[str]] = {}
    for provider, value in values.items():
        groups.setdefault(value, []).append(provider)
    winners = [(value, providers) for value, providers in groups.items() if len(providers) >= 2]
    if len(winners) != 1:
        return {"resolved": False, "accepted_value": None, "basis": "三源无两源一致"}
    value, providers = winners[0]
    basis = "两源一致确认无交易" if value is None else "两源一致确认不复权收盘价"
    return {
        "resolved": True,
        "accepted_value": value,
        "majority_sources": providers,
        "basis": basis,
    }


def checkpoint_is_complete(
    payload: dict[str, Any], code: str, start_date: str, end_date: str,
) -> bool:
    rows = payload.get("rows")
    return bool(
        payload.get("schema_version") == 1
        and payload.get("provider") == "sina"
        and payload.get("code") == code
        and payload.get("start_date") == start_date
        and payload.get("end_date") == end_date
        and payload.get("price_format") == "unadjusted_close"
        and payload.get("provider_complete") is True
        and payload.get("raw_row_count", 0) > 0
        and validate_rows(rows, allow_empty=True)
        and payload.get("row_count") == len(rows)
        and payload.get("rows_sha256") == canonical_sha256(rows)
    )


def collect(
    stocks: list[dict[str, Any]], checkpoint_dir: Path,
    fetcher: Callable[[str, str, str], dict[str, Any]], *, retry: int = 2,
    codes: set[str] | None = None,
) -> dict[str, int]:
    selected = [stock for stock in stocks if codes is None or stock["code"] in codes]
    succeeded = failed = skipped = 0
    for index, stock in enumerate(selected, 1):
        code = stock["code"]
        start_date = str(stock.get("start_date") or "")
        end_date = str(stock.get("end_date") or "")
        path = checkpoint_path(checkpoint_dir, code)
        if path.exists():
            try:
                existing = read_json(path)
            except (OSError, json.JSONDecodeError):
                existing = {}
            if checkpoint_is_complete(existing, code, start_date, end_date):
                skipped += 1
                continue
        result: dict[str, Any] = {}
        error = ""
        for _ in range(max(retry, 0) + 1):
            try:
                result = fetcher(code, start_date, end_date)
                error = ""
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        rows = result.get("rows", []) if not error else []
        payload = {
            "schema_version": 1,
            "provider": "sina",
            "code": code,
            "name": stock.get("name", ""),
            "start_date": start_date,
            "end_date": end_date,
            "price_format": "unadjusted_close",
            "provider_complete": bool(result.get("provider_complete")) and not error,
            "error": error,
            "raw_row_count": result.get("raw_row_count", 0),
            "raw_first_date": result.get("raw_first_date", ""),
            "raw_last_date": result.get("raw_last_date", ""),
            "raw_response_sha256": result.get("raw_response_sha256", ""),
            "row_count": len(rows),
            "rows": rows,
            "rows_sha256": canonical_sha256(rows),
        }
        write_json_atomic(path, payload)
        if payload["provider_complete"]:
            succeeded += 1
        else:
            failed += 1
        if index % 25 == 0 or index == len(selected):
            print(f"新浪核验 {index}/{len(selected)}，成功 {succeeded}，失败 {failed}，跳过 {skipped}")
    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


def build_artifact(source: dict[str, Any], checkpoint_dir: Path) -> dict[str, Any]:
    stocks = []
    for stock in source.get("stocks", []):
        code = stock["code"]
        start_date = str(stock.get("start_date") or "")
        end_date = str(stock.get("end_date") or "")
        primary = primary_rows(stock)
        path = checkpoint_path(checkpoint_dir, code)
        verifier = read_json(path) if path.exists() else {}
        verifier_ok = checkpoint_is_complete(verifier, code, start_date, end_date)
        verification = verifier.get("rows", []) if verifier_ok else []
        primary_map = {row["date"]: row["close"] for row in primary}
        verifier_map = {row["date"]: round(float(row["close"]), 4) for row in verification}
        exact_match = verifier_ok and primary_map == verifier_map
        different_dates = sorted(
            day for day in primary_map.keys() & verifier_map.keys()
            if primary_map[day] != verifier_map[day]
        )
        dates = sorted(
            (primary_map.keys() ^ verifier_map.keys())
            | {day for day in primary_map.keys() & verifier_map.keys()
               if primary_map[day] != verifier_map[day]}
        )
        arbitration_path = tencent_checkpoint_path(checkpoint_dir, code)
        arbitration = read_json(arbitration_path) if arbitration_path.exists() else {}
        arbitration_ok = bool(dates) and tencent_checkpoint_is_complete(arbitration, code, dates)
        decisions = []
        if dates:
            tencent_values = arbitration.get("values", {}) if arbitration_ok else {}
            for day in dates:
                primary_value = primary_map.get(day)
                sina_value = verifier_map.get(day)
                tencent_value = tencent_values.get(day) if arbitration_ok else None
                decision = arbitrate_value(primary_value, sina_value, tencent_value)
                decisions.append({
                    "date": day,
                    "baostock_close": primary_value,
                    "sina_close": sina_value,
                    "tencent_close": tencent_value,
                    **decision,
                })
        independently_verified = exact_match or (
            verifier_ok and arbitration_ok and bool(decisions)
            and all(decision["resolved"] for decision in decisions)
        )
        stocks.append({
            "code": code,
            "name": stock.get("name", ""),
            "primary_row_count": len(primary),
            "verification_row_count": len(verification),
            "verification_provider_complete": verifier_ok,
            "empty_range_confirmed": verifier_ok and not primary and not verification,
            "missing_dates": sorted(primary_map.keys() - verifier_map.keys()),
            "extra_dates": sorted(verifier_map.keys() - primary_map.keys()),
            "different_close_dates": different_dates,
            "arbitration_provider_complete": exact_match or arbitration_ok,
            "arbitration": decisions,
            "independently_verified": independently_verified,
        })
    provider_complete = bool(stocks) and all(
        stock["verification_provider_complete"] for stock in stocks
    )
    independently_verified = provider_complete and all(
        stock["independently_verified"] for stock in stocks
    )
    return {
        "schema_version": 1,
        "source_snapshot_date": source.get("source_snapshot_date"),
        "scope": "eligible_delisted_prices.json 中 139 只退市高息候选的逐日独立价格核验",
        "price_format": "unadjusted_close",
        "primary_source": "baostock/query_history_k_data_plus adjustflag=3 tradestatus=1",
        "verification_source": "sina/CN_MarketData.getKLineData",
        "arbitration_source": "tencent/appstock/app/kline/kline day（无复权参数）",
        "source_file_sha256": canonical_sha256(source),
        "target_count": len(stocks),
        "provider_complete": provider_complete,
        "independently_verified": independently_verified,
        "verified_stock_count": sum(stock["independently_verified"] for stock in stocks),
        "mismatched_stock_count": sum(
            stock["verification_provider_complete"] and not stock["independently_verified"]
            for stock in stocks
        ),
        "failed_stock_count": sum(not stock["verification_provider_complete"] for stock in stocks),
        "stocks": stocks,
        "decision_rule": (
            "新浪与 BaoStock 全量一致，或每个差异日均由腾讯原始日线形成明确两源多数，"
            "才把该股票核验通过；全部股票通过时全局才为 true。"
        ),
        "source_semantics_evidence": {
            "unadjusted_probe": (
                "600466 在 2017-07-11：腾讯原始日线收盘 8.44、腾讯前复权 5.339，"
                "原始入口可与复权入口明确区分。"
            ),
            "suspension_probe": (
                "2015-12-09 市场有交易且 600565 有日线；600466 前后交易但当日腾讯返回空数组，"
                "证明停牌日不生成交易行。"
            ),
        },
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_verified_prices(
    source: dict[str, Any], verification: dict[str, Any],
    *, source_file_sha256: str, verification_file_sha256: str,
) -> dict[str, Any]:
    """只应用三源已解决的争议，生成可回放价格输入。"""
    if not (
        verification.get("provider_complete") is True
        and verification.get("independently_verified") is True
    ):
        raise RuntimeError("三源价格核验尚未全量通过，拒绝生成正式可回放输入")
    source_stocks = source.get("stocks", [])
    decisions_by_code = {stock["code"]: stock for stock in verification.get("stocks", [])}
    if source.get("target_count") != len(source_stocks):
        raise RuntimeError("BaoStock 主输入目标数量不一致")
    if set(decisions_by_code) != {stock["code"] for stock in source_stocks}:
        raise RuntimeError("三源核验股票集合与 BaoStock 主输入不一致")

    stocks = []
    changed_dates: list[dict[str, Any]] = []
    arbitration_applications: list[dict[str, Any]] = []
    for original in source_stocks:
        code = original["code"]
        rows = primary_rows(original)
        values = {row["date"]: row["close"] for row in rows}
        verification_stock = decisions_by_code[code]
        if not verification_stock.get("independently_verified"):
            raise RuntimeError(f"股票 {code} 尚未独立核验通过")
        for decision in verification_stock.get("arbitration", []):
            if not decision.get("resolved") or "accepted_value" not in decision:
                raise RuntimeError(f"股票 {code} 存在未解决仲裁")
            day = decision["date"]
            before = values.get(day)
            accepted = decision["accepted_value"]
            if accepted is None:
                values.pop(day, None)
                action = "delete" if before is not None else "exclude_confirmed_no_trade"
            else:
                accepted = round(float(accepted), 4)
                values[day] = accepted
                action = "insert" if before is None else (
                    "replace" if before != accepted else "keep_baostock"
                )
            application = {
                "code": code, "date": day, "action": action,
                "before": before, "after": accepted,
                "majority_sources": decision.get("majority_sources", []),
            }
            arbitration_applications.append(application)
            if action in {"delete", "insert", "replace"}:
                changed_dates.append(application)
        verified_rows = [
            {"date": day, "close": f"{value:.4f}"} for day, value in sorted(values.items())
        ]
        stock = dict(original)
        stock.update({
            "source": "baostock_with_three_source_arbitration",
            "independently_verified": True,
            "start_date": verified_rows[0]["date"] if verified_rows else "",
            "end_date": verified_rows[-1]["date"] if verified_rows else "",
            "row_count": len(verified_rows),
            "rows_sha256": canonical_sha256(verified_rows),
            "rows": verified_rows,
        })
        stocks.append(stock)

    expected_changed = {
        (decision["code"], item["date"])
        for decision in verification.get("stocks", [])
        for item in decision.get("arbitration", [])
        if item.get("resolved") and item.get("accepted_value") != item.get("baostock_close")
    }
    actual_changed = {(item["code"], item["date"]) for item in changed_dates}
    if actual_changed != expected_changed:
        raise RuntimeError("派生价格修改范围超出或遗漏已仲裁日期")
    independently_verified = bool(stocks) and all(
        stock.get("independently_verified") for stock in stocks
    )
    return {
        "schema_version": 1,
        "source_snapshot_date": source.get("source_snapshot_date"),
        "scope": source.get("scope"),
        "price_format": "unadjusted_close",
        "primary_source": "baostock/query_history_k_data_plus adjustflag=3 tradestatus=1",
        "verification_source": "sina + tencent raw daily three-source arbitration",
        "source_file": "data/historical/eligible_delisted_prices.json",
        "source_file_sha256": source_file_sha256,
        "verification_file": "data/historical/delisted_price_verification.json",
        "verification_file_sha256": verification_file_sha256,
        "target_count": len(stocks),
        "completed_stock_count": len(stocks),
        "failed_stock_count": 0,
        "provider_complete": True,
        "independently_verified": independently_verified,
        "row_count": sum(stock["row_count"] for stock in stocks),
        "stocks_sha256": canonical_sha256(stocks),
        "changed_date_count": len(changed_dates),
        "changes": changed_dates,
        "arbitration_date_count": len(arbitration_applications),
        "confirmed_no_trade_exclusion_count": sum(
            item["action"] == "exclude_confirmed_no_trade"
            for item in arbitration_applications
        ),
        "arbitration_applications": arbitration_applications,
        "stocks": stocks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("collect", "arbitrate", "build", "verified", "both"),
        default="both",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verified-output", type=Path, default=DEFAULT_VERIFIED_OUTPUT)
    parser.add_argument("--codes", default="", help="逗号分隔的探针代码；留空表示全量")
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = read_json(args.input)
    stocks = source.get("stocks", [])
    if source.get("target_count") != len(stocks) or not source.get("provider_complete"):
        raise RuntimeError("主输入目标数不一致或 BaoStock 采集未完成")
    codes = {item.strip() for item in args.codes.split(",") if item.strip()} or None
    if codes and not codes <= {stock["code"] for stock in stocks}:
        raise ValueError("--codes 包含主输入中不存在的代码")
    if args.mode in {"collect", "both"}:
        client = SerialHttpClient(args.interval, 0.10)
        result = collect(
            stocks, args.checkpoint_dir,
            lambda code, start, end: fetch_sina(code, start, end, client),
            retry=args.retry, codes=codes,
        )
        print(json.dumps(result, ensure_ascii=False))
    if args.mode in {"arbitrate", "both"}:
        if codes:
            raise ValueError("腾讯仲裁会自动只取差异股票，不允许 --codes")
        client = SerialHttpClient(args.interval, 0.10)
        result = collect_tencent_arbitration(
            stocks, args.checkpoint_dir,
            lambda code, dates: fetch_tencent_dates(code, dates, client),
            retry=args.retry,
        )
        print(json.dumps({"tencent_arbitration": result}, ensure_ascii=False))
    if args.mode in {"build", "both"}:
        if codes:
            raise ValueError("生成最终产物时不允许 --codes 子集")
        artifact = build_artifact(source, args.checkpoint_dir)
        write_json_atomic(args.output, artifact)
        print(json.dumps({
            key: artifact[key] for key in (
                "target_count", "provider_complete", "independently_verified",
                "verified_stock_count", "mismatched_stock_count", "failed_stock_count",
            )
        }, ensure_ascii=False, indent=2))
    if args.mode in {"verified", "both"}:
        if args.mode == "both":
            verification = artifact
        else:
            verification = read_json(args.output)
        verified = build_verified_prices(
            source,
            verification,
            source_file_sha256=file_sha256(args.input),
            verification_file_sha256=file_sha256(args.output),
        )
        write_json_atomic(args.verified_output, verified)
        print(json.dumps({
            "verified_output": str(args.verified_output),
            "target_count": verified["target_count"],
            "row_count": verified["row_count"],
            "changed_date_count": verified["changed_date_count"],
            "independently_verified": verified["independently_verified"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
