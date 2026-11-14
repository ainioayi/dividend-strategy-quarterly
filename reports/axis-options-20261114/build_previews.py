"""从同一冻结快照生成两个可离线打开的坐标展示方案，不改生产页面。"""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
snapshot = json.loads((ROOT / "site/stock-bond.json").read_text(encoding="utf-8"))
points = [point for point in snapshot["crossings"] if point["direction"] == "down"]
assert len({point["date"] for point in points}) == len(points)
assert all(point["date"] == snapshot["chart"]["dates"][point["index"]] for point in points)
assert snapshot["chart"]["stock"][0] == snapshot["chart"]["bond"][0] == 1000
# 相同线性坐标变换不得改变任意两条线的相对高低。
for stock, bond in zip(snapshot["chart"]["stock"], snapshot["chart"]["bond"]):
    if bond is not None:
        assert ((stock > bond) - (stock < bond)) == ((stock / 10 - 100 > bond / 10 - 100) - (stock / 10 - 100 < bond / 10 - 100))

base_css = (ROOT / "site/chart.css").read_text(encoding="utf-8")
css = base_css + """
.preview-nav{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}.preview-nav a{padding:9px 15px;color:#245b45;border:1px solid #a5b8a9;border-radius:6px;text-decoration:none;background:white;font-size:13px;min-height:44px;display:inline-flex;align-items:center}.preview-nav a[aria-current=page]{background:#245b45;color:white;border-color:#245b45}.preview-caption{color:#5d6c65;font-size:12px;margin:0 0 16px}.explanation{padding:18px 22px;border:1px solid #d8dfd7;border-radius:8px;background:#edf2e9;margin-bottom:18px}.explanation h2{font-size:18px;margin:0 0 5px}.explanation p{margin:3px 0;font-size:13px}.explanation small{font-size:12px;color:#5d6c65}.prototype-warning{padding:15px 20px;background:#f3e9d6;border-left:3px solid #80501b;color:#67441e;font-size:12px;margin-bottom:18px;border-radius:0 6px 6px 0}.prototype-warning details{margin-top:6px}.prototype-warning summary{cursor:pointer}.prototype-warning p{margin:8px 0 0}.legend{padding:16px 24px;display:flex;gap:22px;font-size:12px;flex-wrap:wrap;color:#5d6c65}.legend span:before{content:'';display:inline-block;width:17px;height:2px;background:#245b45;margin-right:7px;vertical-align:middle}.legend span:nth-child(2):before{background:#94714f}.legend span:nth-child(3):before{height:7px;width:7px;background:#a84836;border-radius:50%}#chart-panel{scroll-margin-top:12px}#chart{display:block;width:100%;height:auto;padding:0 10px}.axis-heading{fill:#1d302d;font-size:14px;font-weight:600}.selected-label{fill:#a84836;font-size:13px;font-weight:600}.badge-number{font-family:system-ui,sans-serif;font-size:11px;font-weight:600;fill:#a84836}.badge-number.active{fill:#fff}.date-index{background:#fff;border:1px solid #d8dfd7;border-radius:12px;padding:23px;margin-top:20px}.date-index h2{font-size:19px;margin:0}.date-index>p{font-size:12px;color:#5d6c65;margin:7px 0 19px}.date-year+.date-year{border-top:1px solid #e2e7df;margin-top:17px;padding-top:14px}.date-year h3{font-size:13px;color:#5d6c65;margin:0 0 10px}.date-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}.date-button{display:flex;gap:10px;align-items:center;min-height:45px;border:1px solid #d8dfd7;background:#fbfcf8;border-radius:6px;padding:8px 10px;cursor:pointer;color:#1d302d;font-size:12px;font-variant-numeric:tabular-nums}.date-button[aria-pressed=true]{background:#e7eee6;border-color:#245b45}.date-number{display:grid;place-items:center;min-width:22px;height:22px;border:1px solid #a84836;border-radius:50%;font-size:10px;color:#a84836}.date-button[aria-pressed=true] .date-number{background:#a84836;color:white}a:focus-visible,button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #245b45;outline-offset:3px}.date-index .index-status{margin:15px 0 0;padding:12px 0 0;border-top:1px solid #d8dfd7;font-size:12px;color:#5d6c65}.header-row{flex-wrap:wrap;padding-top:12px;padding-bottom:12px}.header-row h1{white-space:normal}.boundary-note{margin-bottom:12px}@media(max-width:900px){.date-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:680px){.date-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.date-index{padding:18px}.date-button{font-size:12px;gap:7px}.axis-heading{font-size:14px}.selected-label{font-size:12px}.legend{padding:14px 18px;gap:12px;font-size:11px}.explanation{padding:16px}.explanation h2{font-size:17px}.preview-nav a{font-size:12px;padding:8px 11px}.prototype-warning{padding:14px}.header-row{gap:4px}.header-row h1{font-size:16px}.data-date{white-space:nowrap}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""
script = (HERE / "preview.js").read_text(encoding="utf-8")
payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
for mode, filename, name in [("percent", "returns.html", "A · 累计收益百分比"), ("indexed", "indexed.html", "B · 共同起点1000")]:
    active_a = ' aria-current="page"' if mode == "percent" else ""
    active_b = ' aria-current="page"' if mode == "indexed" else ""
    description = "自2004-12-31起的累计收益率" if mode == "percent" else "自2004-12-31起的累计表现指数"
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"><link rel="icon" href="data:,"><title>{name}｜股债交点对照预览</title><style>{css}</style></head>
<body data-axis="{mode}"><header class="site-header"><div class="header-row wrap"><h1>{name}</h1><p class="data-date">共同数据截至：<time id="as-of"></time></p></div></header>
<main class="wrap"><p class="preview-caption">本地方案预览 · 同一份公开指数快照 · 不改变历史交点</p><nav class="preview-nav" aria-label="选择坐标对照方案"><a href="returns.html"{active_a}>A 累计收益（%）</a><a href="indexed.html"{active_b}>B 起点＝1000</a></nav>
<section class="explanation" aria-labelledby="axis-heading"><h2 id="axis-heading">{description}</h2><p id="preview-basis"></p><small>它描述指数表现，不是你的账户收益；两种坐标表示相同涨跌，所有下穿日期相同。</small></section>
<aside class="prototype-warning" aria-label="数据核验边界"><b>这不是510300ETF收益回测，历史下穿仍待核验。</b> 债基来源的日期和年度数值存在争议，不能把图上的点直接当作买入指令。<details><summary>查看当前公开来源的质量记录</summary><p id="quality-detail"></p></details></aside>
<section class="real-frame" id="chart-panel" aria-label="带全部下穿编号的公开指数曲线"><div class="real-toolbar"><label for="point-select">选择下穿日期（全部原始点）<select id="point-select"></select></label><button id="zoom" type="button" aria-pressed="false">放大选中点</button></div><div class="legend"><span>沪深300全收益 · 分红再投入</span><span>万得二级债基 · 第三方曲线</span><span>历史下穿 · 待核验</span></div><svg id="chart" role="img" aria-labelledby="chart-title chart-description"></svg><p class="real-note" id="period" role="status" aria-live="polite" aria-atomic="true"></p></section>
<section class="date-index" aria-labelledby="point-count"><h2 id="point-count"></h2><p>红点与圆形编号一一对应。点击任意日期会放大附近区间，包含未进入原8个保留点的事件。密集编号自动分层显示。</p><div id="dates"></div><p class="index-status" id="selected-note" role="status" aria-live="polite" aria-atomic="true"></p></section>
<p class="boundary-note">拟验证对象：沪深300ETF。510300于2012-05-28上市；此前的指数下穿不能当作该ETF的交易业绩。</p></main>
<script id="snapshot" type="application/json">{payload}</script><script>{script}</script></body></html>'''
    (HERE / filename).write_text(html, encoding="utf-8", newline="\n")
    assert html.count('id="snapshot"') == 1
    assert 'fetch(' not in html
    print(filename, "已生成；原始下穿", len(points), "；数据截至", snapshot["as_of"])
