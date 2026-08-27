"""可版本化候选池 manifest 的生成与加载工具。

manifest 是回测候选池的输入边界，不依赖 ``backtest_cache`` 目录内容。
记录哈希使用规范化 JSON 计算，保证同一数据和规则在不同机器上得到相同结果。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
# 覆盖沪深、创业板、科创板及北交所六位证券代码。
CODE_RE = re.compile(r"^[03689]\d{5}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    """对规范化 JSON 求哈希，供 manifest 生成与运行时复核共用。"""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def records_hash(records: Iterable[dict[str, Any]]) -> str:
    """返回候选记录的 SHA-256；记录顺序按代码固定。"""
    normalized = sorted((dict(r) for r in records), key=lambda r: str(r.get("code", "")))
    return hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest()


def build_manifest(
    records: Iterable[dict[str, Any]],
    *,
    as_of: str,
    top: int,
    min_years: int,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建不可变候选池描述；``as_of`` 必须由调用方明确提供。"""
    if not as_of or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError("as_of 必须为 YYYY-MM-DD")
    normalized = []
    for raw in records:
        code = str(raw.get("code", "")).zfill(6)
        if not CODE_RE.fullmatch(code):
            continue
        item = dict(raw)
        item["code"] = code
        normalized.append(item)
    normalized.sort(key=lambda r: str(r["code"]))
    codes = [r["code"] for r in normalized]
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "rules": {"top": int(top), "min_years": int(min_years), "sort": "code"},
        "source": source or {"name": "eastmoney", "endpoint": "RPT_SHAREBONUS_DET"},
        "records": normalized,
        "codes": codes,
        "records_sha256": records_hash(normalized),
    }


def load_manifest(path: str | Path) -> dict[str, Any]:
    """读取并严格校验 manifest，失败时抛出 ValueError。"""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的 universe manifest schema_version")
    records = data.get("records")
    codes = data.get("codes")
    if not isinstance(records, list) or not isinstance(codes, list):
        raise ValueError("manifest 缺少 records/codes")
    expected_codes = [str(r.get("code", "")) for r in records]
    if expected_codes != sorted(expected_codes) or codes != expected_codes:
        raise ValueError("manifest 代码列表未按 code 排序或与 records 不一致")
    if any(not CODE_RE.fullmatch(c) for c in codes) or len(codes) != len(set(codes)):
        raise ValueError("manifest 含无效或重复股票代码")
    if data.get("records_sha256") != records_hash(records):
        raise ValueError("manifest records_sha256 校验失败")
    as_of = str(data.get("as_of") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError("manifest as_of 无效")
    # 若记录包含事件日期，禁止出现截止日之后的数据。
    for record in records:
        for key in ("latest_event_date", "ex_dividend_date", "data_max_date"):
            value = record.get(key)
            if not value:
                continue
            value_text = str(value)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value_text):
                raise ValueError(f"manifest 记录 {record.get('code')} 的 {key} 日期无效")
            if value_text > as_of:
                raise ValueError(f"manifest 记录 {record.get('code')} 的 {key} 晚于 as_of")
    return data


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_cache_snapshot(manifest: dict[str, Any], cache_dir: str | Path) -> None:
    """复核 manifest 记录对应的本地行情/分红快照未被替换。"""
    records = manifest.get("records") or []
    if not any("kline_sha256" in r or "dividend_detail_sha256" in r for r in records):
        return
    root = Path(cache_dir)
    as_of = str(manifest.get("as_of") or "")[:10]
    for record in records:
        code = str(record.get("code") or "")
        kl_path = root / f"kl_{code}.json"
        if not kl_path.exists():
            raise ValueError(f"manifest 对应 K 线缓存不存在: {code}")
        try:
            prices = json.loads(kl_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"K 线缓存无法读取: {code}") from exc
        point_prices = {
            str(day)[:10]: value for day, value in (prices or {}).items()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)[:10])
            and str(day)[:10] <= as_of
        }
        if record.get("data_max_date") != max(point_prices, default=None):
            raise ValueError(f"K 线截止日与 manifest 不一致: {code}")
        if record.get("kline_sha256") and canonical_hash(point_prices) != record["kline_sha256"]:
            raise ValueError(f"K 线哈希与 manifest 不一致: {code}")

        detail_path = root / f"dvd_{code}.json"
        if record.get("dividend_detail_sha256"):
            if not detail_path.exists():
                raise ValueError(f"manifest 对应分红明细不存在: {code}")
            try:
                details = json.loads(detail_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"分红明细无法读取: {code}") from exc
            known = [
                row for row in (details or [])
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("ex_date") or "")[:10])
                and str(row.get("ex_date"))[:10] <= as_of
            ]
            if canonical_hash(known) != record["dividend_detail_sha256"]:
                raise ValueError(f"分红明细哈希与 manifest 不一致: {code}")
