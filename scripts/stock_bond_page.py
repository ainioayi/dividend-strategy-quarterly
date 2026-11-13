"""更新股债曲线的独立冻结输入与页面数据，不读写策略或账户。"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "2004-12-31"
CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
JCS_URL = "https://api.jiucaishuo.com/v2/fundindex/detail"
JCS_BASIC = "https://api.jiucaishuo.com/v2/guzhi-new2/index-basic"
BOND_SOURCE = "韭圈儿公开转发的万得885007.WI累计收益曲线"


def iso_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期必须为YYYY-MM-DD")
    return value


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def read_json(path):
    def invalid(value):
        raise ValueError("JSON含非有限数字：" + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid)


def load_parameters(path):
    parameters = read_json(path)
    if not isinstance(parameters, dict) or set(parameters) != {"base_date", "cooldown_sessions"}:
        raise ValueError("参数必须且只能包含base_date与cooldown_sessions")
    if iso_day(parameters["base_date"]) < BASE:
        raise ValueError("共同起点早于官方全收益历史")
    cooldown = parameters["cooldown_sessions"]
    if type(cooldown) is not int or cooldown < 1:
        raise ValueError("冷却期必须是正整数交易日")
    return parameters


def positive(value):
    if isinstance(value, bool):
        raise ValueError("点位不能是布尔值")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("点位必须为有限正数")
    return number


def parse_csi(payload, code, as_of):
    if str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list):
        raise ValueError("中证响应无有效数据")
    values, seen = {}, set()
    for row in payload["data"]:
        if row.get("indexCode") != code or (code == "H00300" and "全收益" not in row.get("indexNameCnAll", "")):
            raise ValueError("中证返回了其他指数，拒绝替代")
        day = datetime.strptime(row["tradeDate"], "%Y%m%d").date().isoformat()
        value = positive(row["close"])
        if day in seen:
            raise ValueError("中证响应日期重复")
        seen.add(day)
        if BASE <= day <= as_of:
            values[day] = value
    return values


def parse_bond(basic, payload, as_of):
    identity = {row["title"]: row["name"] for row in basic.get("data", {}).get("files", [])}
    if basic.get("code") != 0 or identity.get("代码") != "885007.WI" or identity.get("全称") != "万得混合债券型二级基金指数":
        raise ValueError("债基代码或名称不符，拒绝替代")
    if payload.get("code") != 0 or payload.get("data", {}).get("index_create_time") != "2003-12-31":
        raise ValueError("债基响应或基期无效")
    chart = payload["data"]["tb_data"]
    own = [series for series in chart["series"] if series.get("name") == "本指数"]
    if len(own) != 1 or len(chart["x_data"]) != len(own[0]["data"]):
        raise ValueError("债基日期与本指数数组不一致")
    values, seen = {}, set()
    for day, change in zip(chart["x_data"], own[0]["data"]):
        iso_day(day)
        if day == "1970-01-01":
            continue
        if isinstance(change, bool):
            raise ValueError("累计收益不能是布尔值")
        value = positive(100 + float(change))
        if day in seen:
            raise ValueError("债基响应日期重复")
        seen.add(day)
        if day <= as_of:
            values[day] = value
    return values, identity["全称"]


def write_csv(path, values):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["date", "value"])
        writer.writerows(sorted(values.items()))


def acquire(folder, as_of):
    iso_day(as_of)
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"})

        def request(name, url, *, params=None, body=None):
            response = session.get(url, params=params, timeout=40) if body is None else session.post(url, json=body, timeout=40)
            response.raise_for_status()
            payload = response.json()
            path = folder / (name + "-raw.json.gz")
            path.write_bytes(gzip.compress(response.content, mtime=0))
            sources.append({"file": path.name, "url": response.url, "method": "GET" if body is None else "POST", "body": body,
                            "raw_sha256": hashlib.sha256(response.content).hexdigest(), "file_sha256": fingerprint(path)})
            return payload

        csi = {}
        for code in ["H00300", "000300"]:
            payload = request(code.lower(), CSI_URL, params={"indexCode": code, "startDate": BASE.replace("-", ""), "endDate": as_of.replace("-", "")})
            csi[code] = parse_csi(payload, code, as_of)
        days = sorted(csi["000300"])
        if len(days) < 5000 or set(days) - set(csi["H00300"]):
            raise ValueError("官方全收益未覆盖完整市场交易日")
        stock = {day: csi["H00300"][day] for day in days}
        if stock.get(BASE) != 1000:
            raise ValueError("官方基日与1000基点不符")
        session.headers["Referer"] = "https://app.jiucaishuo.com/"
        common = {"type": "h5", "version": "2.5.9", "ss": ""}
        basic = request("885007-basic", JCS_BASIC, body={"gu_code": "885007.WI", "category": "pe", "year": 10, **common})
        payload = request("885007", JCS_URL, body={"gu_code": "885007.WI", "date": "all", "ben": "", "search_code": "", "s_time": "", "e_time": "", "line_type": "sy", "is_close": 2, **common})
        bond, name = parse_bond(basic, payload, as_of)
    if len(bond) < 5000 or BASE not in bond:
        raise ValueError("债基历史覆盖不足")
    write_csv(folder / "stock.csv", stock)
    write_csv(folder / "bond.csv", bond)
    manifest = {"requested_as_of": as_of, "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "base_date": BASE,
                "stock": {"code": "H00300.CSI", "basis": "官方全收益收盘点位", "rows": len(stock), "first": min(stock), "last": max(stock)},
                "bond": {"code": "885007.WI", "name": name, "basis": "第三方累计收益百分比加100，不是官方绝对收盘价", "rows": len(bond), "first": min(bond), "last": max(bond), "precision_pp": 0.01},
                "excluded_stock_nontrading_dates": sorted(set(csi["H00300"]) - set(days)), "sources": sources,
                "normalized_sha256": {name: fingerprint(folder / name) for name in ["stock.csv", "bond.csv"]}}
    (folder / "manifest.json").write_text(json_text(manifest), encoding="utf-8")
    return manifest


def load_values(path, as_of):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["date", "value"]:
            raise ValueError("曲线CSV列不符")
        rows = [(iso_day(row["date"]), positive(row["value"])) for row in reader]
    if not rows or rows != sorted(rows) or len(dict(rows)) != len(rows) or rows[-1][0] > as_of:
        raise ValueError("曲线日期为空、乱序、重复或越过冻结截止日")
    return dict(rows)


def load_inputs(folder):
    manifest = read_json(folder / "manifest.json")
    as_of = iso_day(manifest["requested_as_of"])
    if manifest["stock"]["code"] != "H00300.CSI" or manifest["bond"]["code"] != "885007.WI":
        raise ValueError("冻结输入代码不符")
    for name in ["stock.csv", "bond.csv"]:
        if fingerprint(folder / name) != manifest["normalized_sha256"][name]:
            raise ValueError("冻结输入指纹不符：" + name)
    expected_raw = {"h00300-raw.json.gz", "000300-raw.json.gz", "885007-basic-raw.json.gz", "885007-raw.json.gz"}
    if {source["file"] for source in manifest["sources"]} != expected_raw or len(manifest["sources"]) != 4:
        raise ValueError("冻结清单缺少原始数据来源")
    for source in manifest["sources"]:
        name = source["file"]
        if Path(name).name != name or not name.endswith("-raw.json.gz"):
            raise ValueError("原始文件路径无效")
        path = folder / name
        if fingerprint(path) != source["file_sha256"] or hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() != source["raw_sha256"]:
            raise ValueError("原始输入指纹不符：" + name)
    stock, bond = load_values(folder / "stock.csv", as_of), load_values(folder / "bond.csv", as_of)
    if min(stock) != BASE or stock[BASE] != 1000 or min(bond) < "2003-12-31":
        raise ValueError("冻结曲线基日或基点异常")
    for name, values in [("stock", stock), ("bond", bond)]:
        meta = manifest[name]
        if (meta["rows"], meta["first"], meta["last"]) != (len(values), min(values), max(values)):
            raise ValueError("冻结输入覆盖范围与清单不符")
    return manifest, stock, bond


def find_signals(days, stock, bond, base):
    gaps = [stock[day] / stock[base] - bond[day] / bond[base] if day in bond else None for day in days]
    signals = []
    for i in range(1, len(days)):
        previous, current = gaps[i - 1], gaps[i]
        if previous is None or current is None:
            continue  # 缺少相邻交易日数据时，不猜测缺口里的交点。
        if previous > 0 and current <= 0:
            signals.append({"index": i, "date": days[i], "direction": "down"})
        elif previous < 0 and current >= 0:
            signals.append({"index": i, "date": days[i], "direction": "up"})
    return signals


def merge_signals(signals, cooldown):
    kept = []
    for signal in signals:
        if signal["direction"] == "down" and (not kept or signal["index"] - kept[-1]["index"] >= cooldown):
            kept.append(signal)
    return kept


def build(folder, parameters, parameter_hash, generated_at=None):
    manifest, stock, bond = load_inputs(folder)
    base = parameters["base_date"]
    if base not in stock or base not in bond:
        raise ValueError("配置的共同起点缺少数据")
    common = sorted(set(stock) & set(bond))
    end = common[-1]
    days = sorted(day for day in stock if base <= day <= end)
    if len(days) < 2:
        raise ValueError("共同起点之后没有足够曲线数据")
    stock_line = [positive(stock[day] / stock[base] * 1000) for day in days]
    bond_line = [positive(bond[day] / bond[base] * 1000) if day in bond else None for day in days]
    crossings = find_signals(days, stock, bond, base)
    warnings = ["债基来自第三方累计收益曲线，精度为0.01个百分点；存在日期错位疑点，保留原日期，未做事后平移修正。"]
    annual_checks = []
    for year, expected in {2020: 9.12, 2021: 7.82, 2022: -5.07, 2023: 0.57, 2024: 4.90}.items():
        if str(year + 1) > end or not any(day.startswith(str(year)) for day in stock):
            continue
        before = max(day for day in stock if day < str(year))
        after = max(day for day in stock if day.startswith(str(year)))
        observed = (bond[after] / bond[before] - 1) * 100 if before in bond and after in bond else None
        annual_checks.append({"year": year, "published_pct": expected, "curve_pct": observed, "source_start": before, "source_end": after})
        if observed is None:
            warnings.append(f"{year}年末债基数据缺失，不能完成年度交叉核验。")
        elif abs(observed - expected) > 0.02:
            warnings.append(f"{year}年曲线复算{observed:.2f}%，与公开披露{expected:.2f}%不一致。")
    missing = [day for day in days if day not in bond]
    if missing:
        warnings.append(f"共同区间内债基缺少{len(missing)}个市场交易日，保留曲线断点，不跨缺口识别交点。")
    return {"schema_version": 1, "as_of": end, "requested_as_of": manifest["requested_as_of"],
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(), "parameters": parameters,
            "chart": {"dates": days, "stock": stock_line, "bond": bond_line}, "crossings": crossings,
            "signals": merge_signals(crossings, parameters["cooldown_sessions"]),
            "quality": {"warnings": warnings, "bond_source": BOND_SOURCE},
            "audit": {"parameters_sha256": parameter_hash, "manifest_sha256": fingerprint(folder / "manifest.json"),
                      "input_sha256": manifest["normalized_sha256"], "missing_bond_dates": missing,
                      "excluded_nontrading_dates": manifest["excluded_stock_nontrading_dates"], "annual_checks": annual_checks,
                      "annual_reference": "https://www.investorchina.cn/article/81520"}}


def verify(folder, parameter_path, output):
    parameters = load_parameters(parameter_path)
    actual = read_json(output)
    stamp = datetime.fromisoformat(actual["generated_at"])
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("生成时间必须包含时区，且不能晚于当前时间")
    expected = build(folder, parameters, fingerprint(parameter_path), actual["generated_at"])
    if actual != expected:
        raise ValueError("页面数据与输入、参数或时间边界不一致，请离线重算")
    return actual


def update(folder, parameter_path, output, *, refresh=False, as_of=None):
    parameters = load_parameters(parameter_path)
    parameter_hash = fingerprint(parameter_path)
    if refresh and as_of is None:
        raise ValueError("刷新必须显式提供--as-of")
    if as_of is not None:
        iso_day(as_of)
    output.parent.mkdir(parents=True, exist_ok=True)
    folder.parent.mkdir(parents=True, exist_ok=True)
    prior, prior_date = None, None
    if output.exists():
        prior = read_json(output)
        prior_date = iso_day(prior["as_of"])
        iso_day(prior["requested_as_of"])
    if folder.exists():
        _, old_stock, old_bond = load_inputs(folder)
        cache_date = max(set(old_stock) & set(old_bond))
        prior_date = max(prior_date or cache_date, cache_date)
    with tempfile.TemporaryDirectory(prefix=".stock-bond-", dir=folder.parent) as temp:
        stage = Path(temp) / "new"
        if refresh:
            acquire(stage, as_of)
            source = stage
        else:
            source = folder
        result = build(source, parameters, parameter_hash)
        if as_of is not None and result["requested_as_of"] != as_of:
            raise ValueError("命令截止日与冻结输入不符")
        if prior_date and result["as_of"] < prior_date:
            raise ValueError("源数据共同截止日期倒退，保留已有页面")
        if refresh and folder.exists():
            _, new_stock, new_bond = load_inputs(stage)
            if set(old_stock) - set(new_stock) or set(old_bond) - set(new_bond):
                raise ValueError("刷新结果丢失已有历史日期，保留已有页面与缓存")
        if prior is not None:
            visible = ["schema_version", "as_of", "parameters", "chart", "crossings", "signals", "quality"]
            if (refresh and folder.exists() and result["audit"]["parameters_sha256"] == prior["audit"]["parameters_sha256"]
                    and all(result[key] == prior[key] for key in visible)):
                verify(folder, parameter_path, output)
                return prior  # 周末、来源延迟或仅单边更新时，保留已校验的同一份共同曲线。
            comparison = {**result, "generated_at": prior["generated_at"]}
            if refresh and folder.exists():
                comparison["audit"] = {**result["audit"], "manifest_sha256": fingerprint(folder / "manifest.json")}
            if comparison == prior:
                return prior  # 相同曲线与参数不改生成时间，也不替换相同内容的缓存。
        text = json_text(result)
        handle, pending = tempfile.mkstemp(prefix=".stock-bond-", suffix=".json", dir=output.parent)
        pending = Path(pending)
        installed = False
        backup = Path(temp) / "old"
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            if refresh:
                if folder.exists():
                    os.replace(folder, backup)
                os.replace(stage, folder)
                installed = True
            os.replace(pending, output)
        except BaseException:
            if installed:
                os.replace(folder, stage)
            if backup.exists():
                os.replace(backup, folder)
            raise
        finally:
            pending.unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="下载并原子更新独立输入，必须显式提供截止日")
    parser.add_argument("--as-of", type=iso_day)
    parser.add_argument("--verify-only", action="store_true", help="只离线核对已生成页面，不联网、不改文件")
    parser.add_argument("--parameters", type=Path, default=ROOT / "config" / "stock_bond.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "stock-bond")
    parser.add_argument("--output", type=Path, default=ROOT / "site" / "stock-bond.json")
    args = parser.parse_args(argv)
    if args.refresh and (args.verify_only or args.as_of is None):
        parser.error("--refresh不能与--verify-only并用，且必须显式提供--as-of")
    if args.verify_only:
        result = verify(args.data_dir, args.parameters, args.output)
        if args.as_of is not None and args.as_of != result["requested_as_of"]:
            raise ValueError("命令截止日与页面冻结输入不符")
    else:
        result = update(args.data_dir, args.parameters, args.output, refresh=args.refresh, as_of=args.as_of)
    print(f"股债曲线校验通过；实际共同数据截至{result['as_of']}；下穿保留点{len(result['signals'])}个；输出{args.output}")


if __name__ == "__main__":
    main()
