"""冻结股债下穿验证用ETF/LOF真实场内价格、分红及核验记录。"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

import requests

from tradeable_benchmark import parse_dividends

ROOT = Path(__file__).resolve().parents[1]
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
CSI = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
SPECS = {
    "510300": {"name": "华泰柏瑞沪深300ETF", "type": "ETF", "symbol": "sh510300", "listing_date": "2012-05-28"},
    "160706": {"name": "嘉实沪深300ETF联接（LOF）A", "type": "LOF", "symbol": "sz160706", "listing_date": "2005-10-17"},
}
# 人工逐份读取管理人公告的场内日期，固定PDF指纹；不是把场外日期统一平移。
LOF_NOTICES = {
    "2018-01-15": ("2018-01-16", "2018-01-18", .0233, "AN201801101075891990", "db3d64dfc75f6215b930ed90dae0a254d13e3f35a33ca47e85ce993bc5eb68ae"),
    "2020-04-02": ("2020-04-03", "2020-04-08", .03, "AN202003301377198390", "b26a923c43790e24292d4070534803cf945c4837ab5b8bbf333187a175896e39"),
    "2021-04-20": ("2021-04-21", "2021-04-23", .1979, "AN202104161485519975", "764d2b71a26174f9e8234129e615d5c9e9a6d11c993065115de5112f3f864397"),
    "2025-01-15": ("2025-01-16", "2025-01-20", .015, "AN202501121641896576", "586dc7f3bb3d76fdf99e208ea7893cb42a6d8447fae6aebf5ed9e7b9df2837f5"),
    "2026-01-16": ("2026-01-19", "2026-01-21", .0355, "AN202601131816982257", "0cade6e182a34119befb8192c0452496cdcd854bff945e1027825a004ee0fa34"),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期必须是YYYY-MM-DD")
    return value


def number(value, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError("数字不能为布尔值")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise ValueError("行情或公司事件包含无效数字")
    return result


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


class SplitParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = self.cell = False
        self.rows, self.cells, self.parts = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and "fhxq" in dict(attrs).get("class", "").split():
            self.active = True
        elif self.active and tag == "tr":
            self.cells = []
        elif self.active and tag in {"td", "th"}:
            self.cell, self.parts = True, []

    def handle_data(self, data):
        if self.cell:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.cell and tag in {"td", "th"}:
            self.cells.append("".join(self.parts).strip())
            self.cell = False
        elif self.active and tag == "tr":
            self.rows.append(self.cells)
        elif self.active and tag == "table":
            self.active = False


def parse_splits(html, as_of):
    parser = SplitParser()
    parser.feed(html)
    splits, recognized = [], False
    for row in parser.rows:
        if len(row) != 4 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[1]):
            continue
        split_day = day(row[1])
        match = re.fullmatch(r"([\d.]+)\s*[:：]\s*([\d.]+)", row[3])
        if not match:
            raise ValueError("拆分比例无法解析，拒绝忽略公司事件")
        ratio = number(match[2]) / number(match[1])
        recognized = True
        if split_day <= as_of:
            splits.append({"date": split_day, "ratio": ratio, "description": row[2]})
    if not recognized and not any("暂无拆分" in cell for row in parser.rows for cell in row):
        raise ValueError("未识别到拆分记录或明确的暂无拆分状态")
    return sorted(splits, key=lambda item: item["date"])


def parse_sina(payload, listing, as_of):
    if not isinstance(payload, list):
        raise ValueError("新浪没有返回场内日线数组")
    rows, seen = [], set()
    for raw in payload:
        stamp = day(raw["day"])
        close, opening = number(raw["close"]), number(raw["open"])
        volume = number(raw["volume"], allow_zero=True)
        if stamp in seen:
            raise ValueError("新浪日线日期重复")
        seen.add(stamp)
        if listing <= stamp <= as_of:
            rows.append({"date": stamp, "close": close, "open": opening, "volume": volume})
    rows.sort(key=lambda item: item["date"])
    if not rows or rows[0]["date"] != listing:
        raise ValueError("主源未覆盖上市首日，拒绝静默截短历史")
    return rows


def parse_calendar(payload, as_of):
    rows = payload.get("data")
    if str(payload.get("code")) != "200" or not isinstance(rows, list) or not rows:
        raise ValueError("市场交易日历为空或结构异常")
    dates = []
    for row in rows:
        if row.get("indexCode") != "000300":
            raise ValueError("日历源返回其他证券")
        stamp = datetime.strptime(row["tradeDate"], "%Y%m%d").date().isoformat()
        if stamp > as_of:
            raise ValueError("市场交易日历越过请求截止日")
        dates.append(stamp)
    if dates != sorted(set(dates)):
        raise ValueError("市场交易日历必须严格升序且日期唯一")
    # 非交易日起查时，中证会把首个交易日复制到请求边界；该固定伪点不是真实交易日。
    if dates[0] == "2005-01-01":
        dates = dates[1:]
    if len(dates) < 5000 or dates[0] != "2005-01-04":
        raise ValueError("市场交易日历未覆盖固定起点以来的完整历史")
    if (date.fromisoformat(as_of) - date.fromisoformat(dates[-1])).days > 14:
        raise ValueError("市场交易日历尾部明显落后，拒绝以截短日历推断没有缺口")
    return dates


def calendar_from_folder(folder):
    payload = json.loads(gzip.decompress((folder / "raw" / "000300-calendar.response.gz").read_bytes()))
    as_of = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["requested_as_of"]
    return parse_calendar(payload, as_of)


def calendar_metadata(dates):
    return {"first": dates[0], "last": dates[-1], "rows": len(dates), "dates_sha256": canonical(dates),
            "excluded_request_boundary_dates": ["2005-01-01"]}


def parse_tencent(payload, spec, as_of):
    if payload.get("code") != 0:
        raise ValueError("腾讯行情业务错误")
    inner = payload.get("data", {}).get(spec["symbol"], {})
    quote = inner.get("qt", {}).get(spec["symbol"], [])
    if len(quote) < 3 or quote[2] != spec["symbol"][2:] or ("300" not in quote[1]):
        raise ValueError("腾讯返回了其他证券，拒绝混用代码或市场")
    rows = []
    for raw in inner.get("day", []):
        stamp = day(raw[0])
        opening, close = number(raw[1]), number(raw[2])
        if spec["listing_date"] <= stamp <= as_of:
            rows.append({"date": stamp, "open": opening, "close": close})
    if rows != sorted(rows, key=lambda item: item["date"]) or len({row["date"] for row in rows}) != len(rows):
        raise ValueError("腾讯分页日期乱序或重复")
    return rows, quote[1]


def fetch_tencent(request, code, spec, as_of):
    end, seen, rows, pages = as_of, set(), {}, []
    while True:
        payload = request(f"{code}-tencent-{len(pages) + 1}", TENCENT, params={"param": f"{spec['symbol']},day,{spec['listing_date']},{end},640,day"})
        batch, name = parse_tencent(payload, spec, as_of)
        pages.append({"end": end, "rows": len(batch)})
        if not batch:
            break
        if batch[0]["date"] in seen:
            raise ValueError("腾讯分页没有向历史前移")
        seen.add(batch[0]["date"])
        for row in batch:
            if row["date"] in rows and rows[row["date"]] != row:
                raise ValueError("腾讯分页重叠日期值冲突")
            rows[row["date"]] = row
        if batch[0]["date"] == spec["listing_date"] or len(batch) < 640:
            break
        end = (date.fromisoformat(batch[0]["date"]) - timedelta(days=1)).isoformat()
        time.sleep(.1)
    if not rows or min(rows) != spec["listing_date"]:
        raise ValueError("腾讯对账源未覆盖上市首日")
    return [rows[stamp] for stamp in sorted(rows)], {"name": name, "pages": pages}


def reconcile(primary, secondary):
    left, right = ({row["date"]: row for row in values} for values in [primary, secondary])
    conflicts = []
    for stamp in sorted(set(left) & set(right)):
        fields = [key for key in ["open", "close"] if not math.isclose(left[stamp][key], right[stamp][key], abs_tol=1e-9)]
        if fields:
            conflicts.append({"date": stamp, "fields": fields, "sina": {key: left[stamp][key] for key in fields}, "tencent": {key: right[stamp][key] for key in fields}})
    return {"compared_rows": len(set(left) & set(right)), "conflicts": conflicts,
            "only_sina_dates": sorted(set(left) - set(right)), "only_tencent_dates": sorted(set(right) - set(left))}


def prepare_dividends(code, records, request, as_of):
    dividends, unresolved = [], []
    for record in records:
        record = dict(record)
        for key in ["record_date", "ex_date", "pay_date"]:
            day(record[key])
        number(record["cash_per_unit"])
        if code == "510300":
            if not record["record_date"] < record["ex_date"] <= record["pay_date"]:
                raise ValueError("ETF分红日期顺序异常")
            record.update({"verified": True, "basis": "场内ETF基金档案分红表"})
        elif record["record_date"] in LOF_NOTICES:
            ex, pay, cash, notice, expected = LOF_NOTICES[record["record_date"]]
            raw = request(f"{code}-{notice}", f"https://pdf.dfcfw.com/pdf/H2_{notice}_1.pdf", binary=True)
            if digest(raw) != expected or not math.isclose(record["cash_per_unit"], cash, abs_tol=1e-10):
                raise ValueError("人工核实的LOF场内分红公告或金额变动，需要重新审核")
            original = dict(record)
            record.update({"ex_date": ex, "pay_date": pay, "verified": True, "basis": "管理人收益分配公告中明确列示的场内日期", "notice_id": notice, "original_fund_table": original})
        else:
            record.update({"verified": False, "basis": "仅取得基金档案场外日期，场内日期未核实"})
            unresolved.append({"start": record["record_date"], "end": (date.fromisoformat(record["pay_date"]) + timedelta(days=5)).isoformat(), "reason": "LOF历史分红仅有场外日期，场内除息/发放日未核实", "event": dict(record)})
        if record["ex_date"] <= as_of:
            dividends.append(record)
    return dividends, unresolved


def acquire(folder, as_of, *, replay_from=None):
    day(as_of)
    folder.mkdir(parents=True)
    raw_dir = folder / "raw"
    raw_dir.mkdir()
    sources = []
    session = requests.Session() if replay_from is None else None
    if session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    replay_sources = {row["file"]: row for row in json.loads((replay_from / "manifest.json").read_text(encoding="utf-8"))["sources"]} if replay_from else {}

    def request(name, url, *, params=None, binary=False, text=False):
        suffix = ".pdf.gz" if binary else ".html.gz" if text else ".response.gz"
        path = raw_dir / (name + suffix)
        relative = str(path.relative_to(folder)).replace("\\", "/")
        if replay_from:
            raw = gzip.decompress((replay_from / relative).read_bytes())
            response_url = requests.Request("GET", url, params=params).prepare().url
            if replay_sources[relative]["url"] != response_url:
                raise ValueError("原始响应的请求地址与当前参数不符")
        else:
            response = requests.get(url, params=params, timeout=30) if binary else session.get(url, params=params, timeout=30)
            response.raise_for_status()
            raw, response_url = response.content, response.url
        if binary and not raw.startswith(b"%PDF"):
            raise ValueError("公告端点未返回PDF，拒绝当成已核实公告")
        path.write_bytes(gzip.compress(raw, mtime=0))
        sources.append({"file": relative, "url": response_url,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(), "raw_sha256": digest(raw), "file_sha256": digest(path.read_bytes())})
        return raw if binary else raw.decode("utf-8") if text else json.loads(raw)

    try:
        calendar_payload = request("000300-calendar", CSI, params={"indexCode": "000300", "startDate": "20050101", "endDate": as_of.replace("-", "")})
        calendar = parse_calendar(calendar_payload, as_of)
        calendar_set = set(calendar)
        assets = {}
        for code, spec in SPECS.items():
            asset_start = len(sources)
            tencent, identity = fetch_tencent(request, code, spec, as_of)
            sina = parse_sina(request(code + "-sina", SINA, params={"symbol": spec["symbol"], "scale": "240", "ma": "no", "datalen": "10000"}), spec["listing_date"], as_of)
            quoted_dates = {row["date"] for row in sina + tencent if row["date"] <= calendar[-1]}
            if quoted_dates - calendar_set:
                raise ValueError("市场日历缺少基金实际有报价的交易日，拒绝推断没有缺口")
            check = reconcile(sina, tencent)
            html = request(f"{code}-actions", f"https://fundf10.eastmoney.com/fhsp_{code}.html", text=True)
            if code not in html:
                raise ValueError("基金档案未确认代码")
            dividends, unresolved = prepare_dividends(code, parse_dividends(html, as_of), request, as_of)
            split_table = parse_splits(html, as_of)
            splits = [row for row in split_table if row["date"] >= spec["listing_date"]]
            price_dates = {row["date"] for row in sina}
            missing = [stamp for stamp in calendar if spec["listing_date"] <= stamp <= sina[-1]["date"] and stamp not in price_dates]
            blocked = [{"start": row["date"], "end": row["date"], "reason": "新浪与腾讯未复权行情不一致，尚未取得交易所裁定"} for row in check["conflicts"]]
            blocked += [{"start": stamp, "end": stamp, "reason": "市场交易日无场内报价，尚未逐日核实停牌原因"} for stamp in missing]
            blocked += [{key: row[key] for key in ["start", "end", "reason"]} for row in unresolved]
            warnings = ["主价格统一采用新浪未复权场内价；腾讯只用于全历史对账，不静默拼接不同源。"]
            if check["conflicts"]:
                warnings.append(f"{len(check['conflicts'])}个历史交易日存在双源价格冲突；受影响窗口排除。")
            if check["only_sina_dates"]:
                warnings.append(f"腾讯缺少{len(check['only_sina_dates'])}个新浪有报价日期，保留主源并披露对账覆盖，不宣称全日双源确认。")
            if code == "160706":
                warnings += ["2012-08-21由沪深300指数LOF转为ETF联接LOF，不是2005年起一直存在的ETF。", "五笔2018年及以后分红逐份按管理人PDF核实场内除息日与到账日；其余历史分红场内日期未解。"]
            quality = {"status": "partial" if blocked else "usable", "warnings": warnings, "splits_verified": True,
                       "split_verification_basis": "基金档案拆分折算表（含明确暂无拆分状态），不是按价格跳变猜测", "unresolved_events": unresolved,
                       "blocked_ranges": blocked, "excluded_intervals": blocked, "missing_market_dates": missing,
                       "price_reconciliation": check, "pre_listing_splits": [row for row in split_table if row["date"] < spec["listing_date"]]}
            assets[code] = {**{key: spec[key] for key in ["name", "type", "listing_date"]}, "actual_as_of": sina[-1]["date"],
                            "prices": sina, "dividends": dividends, "splits": splits,
                            "sources": {"symbol": spec["symbol"], "price_basis": "unadjusted_exchange_price", "selected_price_provider": "sina",
                                        "tencent_identity": identity, "tencent_price_rows": len(tencent), "primary_price_rows": len(sina),
                                        "dividends": f"https://fundf10.eastmoney.com/fhsp_{code}.html", "raw_files": [row["file"] for row in sources[asset_start:]]},
                            "quality": quality}
    finally:
        if session:
            session.close()
    result = {"schema_version": 1, "requested_as_of": as_of, "actual_as_of": min(asset["actual_as_of"] for asset in assets.values()), "assets": assets,
              "limitations": ["真实场内成交价不同于基金净值或沪深300全收益指数。", "场内收盘模拟无法复现历史盘口、实际成交与冲击成本。", "所有未核实公司事件和价格冲突均保留，并排除覆盖其区间的评价窗口。", "已上市产品的历史比较存在幸存者选择，不能解释为未来盈利保证。"],
              "hashes": {code: canonical(asset) for code, asset in assets.items()}}
    write_json(folder / "inputs.json", result)
    manifest = {"schema_version": 1, "requested_as_of": as_of, "generated_at": datetime.now(timezone.utc).isoformat(), "inputs_sha256": digest((folder / "inputs.json").read_bytes()),
                "calendar": calendar_metadata(calendar), "sources": sources}
    write_json(folder / "manifest.json", manifest)
    verify(folder, rebuild=False)
    return result


def verify(folder, *, rebuild=True):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    raw = (folder / "inputs.json").read_bytes()
    if digest(raw) != manifest["inputs_sha256"]:
        raise ValueError("资产冻结输入指纹不匹配")
    result = json.loads(raw)
    as_of = day(result["requested_as_of"])
    if result["schema_version"] != 1 or set(result["assets"]) != set(SPECS) or manifest["requested_as_of"] != as_of:
        raise ValueError("资产输入契约不一致")
    for source in manifest["sources"]:
        path = folder / source["file"]
        if path.resolve().parent != (folder / "raw").resolve():
            raise ValueError("原始来源路径越界")
        data = path.read_bytes()
        if digest(data) != source["file_sha256"] or digest(gzip.decompress(data)) != source["raw_sha256"]:
            raise ValueError("原始来源指纹不一致")
    calendar = calendar_from_folder(folder)
    if manifest.get("calendar") != calendar_metadata(calendar):
        raise ValueError("市场日历覆盖范围与冻结清单不一致")
    for code, asset in result["assets"].items():
        spec = SPECS[code]
        if asset["listing_date"] != spec["listing_date"] or asset["sources"]["symbol"] != spec["symbol"]:
            raise ValueError("资产身份或上市边界改变")
        dates = [day(row["date"]) for row in asset["prices"]]
        if not dates or dates != sorted(set(dates)) or dates[0] != spec["listing_date"] or dates[-1] != asset["actual_as_of"] or dates[-1] > as_of:
            raise ValueError("资产历史覆盖或截止日不一致")
        for row in asset["prices"]:
            number(row["close"]); number(row["open"]); number(row["volume"], allow_zero=True)
        for event in asset["dividends"]:
            record, ex, pay = (day(event[key]) for key in ["record_date", "ex_date", "pay_date"])
            number(event["cash_per_unit"])
            if ex > as_of or (event["verified"] and not record < ex <= pay):
                raise ValueError("分红时序无效")
        for split in asset["splits"]:
            if not spec["listing_date"] <= day(split["date"]) <= as_of:
                raise ValueError("拆分日期不在交易历史内")
            number(split["ratio"])
        if canonical(asset) != result["hashes"][code]:
            raise ValueError("资产内容指纹不一致")
    if result["actual_as_of"] != min(asset["actual_as_of"] for asset in result["assets"].values()):
        raise ValueError("资产实际截至日期不一致")
    if rebuild:
        with tempfile.TemporaryDirectory(prefix="crossing-assets-verify-") as tmp:
            rebuilt = acquire(Path(tmp) / "rebuilt", as_of, replay_from=folder)
        if result != rebuilt:
            raise ValueError("资产输入不能从冻结原始响应完整重建")
    return result


def refresh(folder, as_of):
    day(as_of)
    folder.parent.mkdir(parents=True, exist_ok=True)
    old = verify(folder) if folder.exists() else None
    with tempfile.TemporaryDirectory(prefix=".crossing-assets-", dir=folder.parent) as tmp:
        stage, backup = Path(tmp) / "new", Path(tmp) / "old"
        result = acquire(stage, as_of)
        if old:
            if set(calendar_from_folder(folder)) - set(calendar_from_folder(stage)):
                raise ValueError("新市场日历丢失已有交易日，保留冻结输入")
            for code in SPECS:
                previous, current = old["assets"][code], result["assets"][code]
                if current["actual_as_of"] < previous["actual_as_of"] or {row["date"] for row in previous["prices"]} - {row["date"] for row in current["prices"]}:
                    raise ValueError("新资产数据倒退或丢失已有历史，保留冻结输入")
        if folder.exists():
            os.replace(folder, backup)
        try:
            os.replace(stage, folder)
        except BaseException:
            if backup.exists():
                os.replace(backup, folder)
            raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--as-of", type=day)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "crossing-assets")
    args = parser.parse_args(argv)
    if args.refresh == args.verify_only or (args.refresh and args.as_of is None):
        parser.error("请选择--refresh --as-of显式截止日，或--verify-only")
    result = refresh(args.output_dir, args.as_of) if args.refresh else verify(args.output_dir)
    print(json.dumps({code: {"rows": len(asset["prices"]), "first": asset["prices"][0]["date"], "as_of": asset["actual_as_of"], "quality": asset["quality"]["status"], "blocked_ranges": len(asset["quality"]["blocked_ranges"])} for code, asset in result["assets"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
