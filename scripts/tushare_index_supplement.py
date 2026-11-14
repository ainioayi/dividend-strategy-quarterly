"""可选的Tushare同代码尾段；保留中证历史，不消费额外的身份查询额度。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import os
import shutil

import requests

URL = "https://api.tushare.pro"
FILES = {"tushare-h00300-raw.json.gz", "tushare-calendar-raw.json.gz"}
PRICE_FIELDS = "ts_code,trade_date,close"
CALENDAR_FIELDS = "exchange,cal_date,is_open"


class Unavailable(ValueError):
    """只携带安全错误类别，不包含token或供应商任意错误文本。"""


def table(payload, fields):
    if not isinstance(payload, dict):
        raise Unavailable("补充响应不是JSON对象")
    if payload.get("code") != 0:
        code = payload.get("code")
        if code == 40203:
            raise Unavailable("Tushare调用频率或额度受限(code=40203)，本轮不重试")
        suffix = str(code) if type(code) is int else "unknown"
        raise Unavailable("供应商业务错误(code=" + suffix + ")")
    data = payload.get("data")
    expected = fields.split(",")
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or set(data["fields"]) != set(expected) or len(data["fields"]) != len(expected):
        raise Unavailable("补充响应字段不符")
    rows = data.get("items")
    if not isinstance(rows, list) or not rows:
        raise Unavailable("补充响应没有有效行")
    if any(not isinstance(row, list) or len(row) != len(expected) for row in rows):
        raise Unavailable("补充响应行长度异常")
    return [dict(zip(data["fields"], row)) for row in rows]


def compact_day(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise Unavailable("补充响应日期格式无效")
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        raise Unavailable("补充响应日期无效") from None


def ordered(dates):
    if len(dates) != len(set(dates)) or dates not in (sorted(dates), sorted(dates, reverse=True)):
        raise Unavailable("补充响应日期乱序或重复")


def merge(stock, calendar, prices_payload, calendar_payload, start, end):
    """两份响应都核实后才补日历和价格；历史重叠只对账，不覆盖原值。"""
    if start > end or start not in stock:
        raise Unavailable("补充请求缺少中证重叠起点")
    prices, dates = {}, []
    for row in table(prices_payload, PRICE_FIELDS):
        if row["ts_code"] != "H00300.CSI":
            raise Unavailable("补充响应不是H00300.CSI")
        stamp = compact_day(row["trade_date"])
        try:
            value = float(row["close"])
        except (TypeError, ValueError):
            raise Unavailable("补充点位无效") from None
        if isinstance(row["close"], bool) or not math.isfinite(value) or value <= 0 or not start <= stamp <= end:
            raise Unavailable("补充点位非有限正数或日期越界")
        dates.append(stamp)
        prices[stamp] = value
    ordered(dates)
    overlap = set(stock) & set(prices)
    if start not in overlap or any(not math.isclose(stock[d], prices[d], rel_tol=0, abs_tol=.01) for d in overlap):
        raise Unavailable("同代码重叠点位不一致，拒绝拼接")
    calendar_dates, open_days = [], set()
    for row in table(calendar_payload, CALENDAR_FIELDS):
        if row["exchange"] != "SSE" or type(row["is_open"]) is bool or row["is_open"] not in [0, 1, "0", "1"]:
            raise Unavailable("补充交易日历身份或开市状态无效")
        stamp = compact_day(row["cal_date"])
        if not start <= stamp <= end:
            raise Unavailable("补充交易日历日期越界")
        calendar_dates.append(stamp)
        if str(row["is_open"]) == "1":
            open_days.add(stamp)
    ordered(calendar_dates)
    first = datetime.fromisoformat(start).date()
    count = (datetime.fromisoformat(end).date() - first).days + 1
    if set(calendar_dates) != {(first + timedelta(days=i)).isoformat() for i in range(count)}:
        raise Unavailable("补充交易日历未覆盖请求区间")
    if set(prices) != open_days:
        raise Unavailable("补充指数日期未完整对应SSE开市日")
    official_last = max(stock)
    merged = dict(stock)
    merged.update({d: value for d, value in prices.items() if d > official_last})
    market = dict(calendar)
    market.update({d: 1.0 for d in open_days if d > max(calendar)})
    market = {stamp: value for stamp, value in market.items() if stamp <= max(merged)}
    if set(market) - set(merged):
        raise Unavailable("合并后全收益未覆盖交易日历")
    return merged, market


def replay(stock, calendar, original, sources, as_of):
    """依据不含token的请求和冻结响应，离线重建补尾。"""
    records = {row["file"]: row for row in sources if row["file"] in FILES}
    if set(records) != FILES:
        raise Unavailable("补充原始响应或日历不完整")
    first, second = records["tushare-h00300-raw.json.gz"], records["tushare-calendar-raw.json.gz"]
    body = first.get("body", {})
    params = body.get("params", {})
    cal_body = second.get("body", {})
    if (first.get("url") != URL or second.get("url") != URL or first.get("method") != "POST" or second.get("method") != "POST"
            or set(body) != {"api_name", "params", "fields"} or set(cal_body) != {"api_name", "params", "fields"}
            or set(params) != {"ts_code", "start_date", "end_date"} or params.get("ts_code") != "H00300.CSI"
            or body.get("api_name") != "index_daily" or body.get("fields") != PRICE_FIELDS
            or cal_body.get("api_name") != "trade_cal" or cal_body.get("fields") != CALENDAR_FIELDS
            or cal_body.get("params") != {"exchange": "SSE", "start_date": params.get("start_date"), "end_date": params.get("end_date")}):
        raise Unavailable("补充原始请求身份或字段不符")
    start, end = compact_day(params["start_date"]), compact_day(params["end_date"])
    if end > as_of:
        raise Unavailable("缓存补充请求越过当前截止日")
    return merge(stock, calendar, original[first["file"]], original[second["file"]], start, end)


def fetch(stock, calendar, folder, sources, as_of, *, previous=None):
    """本轮每个API最多一次；失败只复用已能离线核验的旧补尾。"""
    token = os.environ.get("TUSHARE_TOKEN", "")
    prior_manifest = json.loads((previous / "manifest.json").read_text(encoding="utf-8")) if previous else None
    prior_records = [row for row in prior_manifest.get("sources", []) if row["file"] in FILES] if prior_manifest else []
    status = {"provider": "Tushare", "stock_code": "H00300.CSI", "status": "not_configured", "message": "未配置可选股票补充源；继续使用中证官方历史。"}
    if token:
        try:
            captured, original = [], {}
            start = max(stock).replace("-", "")
            end = as_of.replace("-", "")
            requests_spec = [("tushare-h00300-raw.json.gz", "index_daily", {"ts_code": "H00300.CSI", "start_date": start, "end_date": end}, PRICE_FIELDS),
                             ("tushare-calendar-raw.json.gz", "trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end}, CALENDAR_FIELDS)]
            for name, api, params, fields in requests_spec:
                public_body = {"api_name": api, "params": params, "fields": fields}
                try:
                    response = requests.post(URL, json={**public_body, "token": token}, timeout=30)
                    response.raise_for_status()
                    raw = response.content
                    if token.encode("utf-8") in raw:
                        raise Unavailable("补充响应含凭据回显，拒绝保存")
                    payload = json.loads(raw)
                    if token in json.dumps(payload, ensure_ascii=False):
                        raise Unavailable("补充响应含凭据回显，拒绝保存")
                except (requests.RequestException, json.JSONDecodeError):
                    raise Unavailable("补充源网络或响应格式异常") from None
                table(payload, fields)
                captured.append((name, raw, public_body))
                original[name] = payload
            merged, market = merge(stock, calendar, original[requests_spec[0][0]], original[requests_spec[1][0]], compact_day(start), as_of)
            for name, raw, body in captured:
                path = folder / name
                path.write_bytes(gzip.compress(raw, mtime=0))
                sources.append({"file": name, "url": URL, "method": "POST", "body": body,
                                "raw_sha256": hashlib.sha256(raw).hexdigest(), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            status.update({"status": "applied", "message": "中证官方历史保留原值，Tushare同代码与SSE日历核验后补充尾段。",
                           "last_success_at": datetime.now(timezone.utc).isoformat()})
            return merged, market, status
        except (Unavailable, KeyError, TypeError, ValueError, OverflowError) as error:
            status.update({"status": "unavailable", "message": "本轮股票补充未采用：" + (str(error) if isinstance(error, Unavailable) else "响应结构校验失败") + "。"})
    if prior_records:
        original = {}
        for row in prior_records:
            raw_file = (previous / row["file"]).read_bytes()
            raw = gzip.decompress(raw_file)
            if hashlib.sha256(raw_file).hexdigest() != row["file_sha256"] or hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                raise Unavailable("旧补充缓存原始指纹不一致")
            original[row["file"]] = json.loads(raw)
        try:
            merged, market = replay(stock, calendar, original, prior_records, as_of)
        except Unavailable as error:
            cached_dates = {compact_day(row["trade_date"]) for row in table(original["tushare-h00300-raw.json.gz"], PRICE_FIELDS)}
            if str(error) == "同代码重叠点位不一致，拒绝拼接" and cached_dates <= set(stock):
                status.update({"status": "unavailable", "message": status["message"] + "旧补充与更新的官方点位不一致，但已被官方完整覆盖；弃用旧补充并保留官方历史。"})
                return stock, calendar, status
            raise Unavailable("旧补充与本轮官方数据不能一致重建，且官方尚未完整覆盖；保留已有页面。") from None
        for row in prior_records:
            path = previous / row["file"]
            shutil.copyfile(path, folder / row["file"])
            sources.append(row)
        status.update({"status": "cached", "message": status["message"] + "复用已核验的旧同代码补充，未伪造新日期。",
                       "last_success_at": prior_manifest.get("stock_supplement", {}).get("last_success_at")})
        return merged, market, status
    return stock, calendar, status
