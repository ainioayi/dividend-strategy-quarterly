"""用固定十年基线和最新模型账本生成 GitHub Pages 首页。"""
from __future__ import annotations

import argparse
import html
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "data" / "backtest_baseline.json"
CONFIG = ROOT / "config" / "strategy.json"
TEMPLATE = ROOT / "site" / "archive" / "2026-08-25-initial-report.html"
INDEX = ROOT / "site" / "index.html"
AUDIT = ROOT / "site" / "audit.json"
LEDGER_DIR = ROOT / "data" / "ledgers"

AUTO_START = "<!-- AUTO_SIGNAL_START -->"
AUTO_END = "<!-- AUTO_SIGNAL_END -->"
WEALTH_ANCHOR = '<section class="section panel"><div class="section-head"><h2>财富曲线</h2>'


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value: Any, digits: int = 2) -> str:
    try:
        return f"¥{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def raw_percent(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def percent(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100.0:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _row_for(ledger: dict[str, Any], code: str) -> dict[str, Any]:
    return (ledger.get("rows_by_code") or {}).get(code, {})


def _holding_rows(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_code, holding in (ledger.get("holdings") or {}).items():
        code = str(raw_code).zfill(6)
        source = _row_for(ledger, code)
        price = source.get("price")
        price_status = "本期核验价"
        if price is None:
            price = holding.get("entry_price")
            price_status = "账本最近核验价"
        shares = int(holding.get("shares") or 0)
        market_value = shares * float(price or 0.0)
        rows.append({
            "code": code,
            "name": holding.get("name") or source.get("name") or code,
            "sector": holding.get("sector") or source.get("sector") or "未知行业",
            "shares": shares,
            "price": price,
            "price_status": price_status,
            "market_value": market_value,
            "yield": source.get("yield", holding.get("entry_yield")),
            "pr": source.get("pr", holding.get("entry_pr")),
            "sustainability": (
                source.get("sustainability")
                or ("可持续（初始核验）" if holding.get("entry_yield") is not None else "待下季复核")
            ),
            "dps": source.get("dps", holding.get("dps")),
        })
    return sorted(rows, key=lambda row: (-float(row.get("yield") or -1.0), row["code"]))


def _ledger_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    holdings = _holding_rows(ledger)
    market_value = sum(float(row["market_value"]) for row in holdings)
    cash = float(ledger.get("cash") or 0.0)
    nav = cash + market_value
    annual_dividend = sum(
        int(row["shares"]) * float(row.get("dps") or 0.0)
        for row in holdings
    )
    return {
        "holdings": holdings,
        "holding_count": len(holdings),
        "cash": cash,
        "market_value": market_value,
        "nav": nav,
        "annual_dividend": annual_dividend,
        "annual_dividend_yield": annual_dividend / nav if nav else None,
    }


def _action_label(action: dict[str, Any]) -> tuple[str, str]:
    if action.get("action") == "buy":
        return "买入", "buy"
    if action.get("action") == "sell":
        return "卖出", "sell"
    if action.get("kind") == "data_gap":
        return "保留（数据缺口）", "dividend"
    return "继续持有", ""


def _action_rows(ledger: dict[str, Any]) -> str:
    output: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for action in ledger.get("last_actions") or []:
        code = str(action.get("code") or "").zfill(6)
        identity = (code, str(action.get("action") or ""), str(action.get("kind") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        holding = (ledger.get("holdings") or {}).get(code, {})
        source = _row_for(ledger, code)
        label, css_class = _action_label(action)
        reasons = "；".join(str(item) for item in (action.get("reasons") or [])) or "规则未触发变化"
        output.append(
            f"<tr><td class='code'>{esc(code)}</td>"
            f"<td>{esc(holding.get('name') or source.get('name') or code)}</td>"
            f"<td class='{css_class}'>{esc(label)}</td><td>{esc(reasons)}</td></tr>"
        )
    return "".join(output) or "<tr><td colspan='4' class='muted'>本季度没有买卖或数据缺口信号</td></tr>"


def _current_holding_rows(summary: dict[str, Any]) -> str:
    nav = float(summary.get("nav") or 0.0)
    output: list[str] = []
    for index, row in enumerate(summary["holdings"], 1):
        weight = float(row["market_value"]) / nav if nav else None
        output.append(
            f"<tr><td>{index}</td><td class='code'>{esc(row['code'])}</td>"
            f"<td>{esc(row['name'])}<small>{esc(row['sector'])}</small></td>"
            f"<td>{esc(row['sustainability'])}</td>"
            f"<td class='num'>{raw_percent(row.get('yield'))}</td>"
            f"<td class='num'>{number(row.get('pr'))}</td>"
            f"<td class='num'>{number(row['shares'], 0)}</td>"
            f"<td class='num' title='{esc(row['price_status'])}'>{money(row.get('price'))}</td>"
            f"<td class='num'>{money(row['market_value'])}</td>"
            f"<td class='num'>{percent(weight)}</td></tr>"
        )
    return "".join(output) or "<tr><td colspan='10' class='muted'>模型当前为空仓</td></tr>"


def _history_rows(ledger: dict[str, Any]) -> str:
    output: list[str] = []
    for item in reversed(list(ledger.get("signal_history") or [])[-8:]):
        counts = {
            key: len(set(str(code).zfill(6) for code in (item.get(key) or [])))
            for key in ("buys", "sells", "holds", "data_gaps")
        }
        output.append(
            f"<tr><td>{esc(item.get('period'))}</td><td>{esc(item.get('as_of'))}</td>"
            f"<td class='num'>{counts['buys']}</td>"
            f"<td class='num'>{counts['sells']}</td>"
            f"<td class='num'>{counts['holds']}</td>"
            f"<td class='num'>{counts['data_gaps']}</td>"
            f"<td class='num'>{money(item.get('nav'))}</td>"
            f"<td class='num'>{money(item.get('cash'))}</td></tr>"
        )
    return "".join(output) or "<tr><td colspan='8' class='muted'>尚无信号历史</td></tr>"


def _signal_summary(ledger: dict[str, Any]) -> str:
    actions = list(ledger.get("last_actions") or [])
    buys = len({item.get("code") for item in actions if item.get("action") == "buy"})
    sells = len({item.get("code") for item in actions if item.get("action") == "sell"})
    gaps = len({item.get("code") for item in actions if item.get("kind") == "data_gap"})
    holds = len({
        item.get("code") for item in actions
        if item.get("action") == "hold" and item.get("kind") != "data_gap"
    })
    return f"买 {buys} · 卖 {sells} · 持有 {holds} · 缺口 {gaps}"


def build_signal_section(
    ledger: dict[str, Any],
    cap20_ledger: dict[str, Any],
) -> str:
    summary = _ledger_summary(ledger)
    cap20 = _ledger_summary(cap20_ledger)
    period = ledger.get("last_processed_period") or "尚未生成"
    as_of = ledger.get("as_of") or "-"
    return f'''{AUTO_START}
<section class="section panel" id="latest-signal"><div class="section-head"><h2>本季度自动调仓信号</h2><span class="hint">{esc(period)} · 数据截至 {esc(as_of)}</span></div>
<div class="rule-grid"><div class="rule"><b>模型净资产</b><span>{money(summary['nav'])}</span><br><small>10 万元初始模拟账户</small></div><div class="rule"><b>可用现金</b><span>{money(summary['cash'])}</span><br><small>持仓市值 {money(summary['market_value'])}</small></div><div class="rule"><b>当前持仓</b><span>{summary['holding_count']} 只</span><br><small>税前股息估算 {raw_percent((summary.get('annual_dividend_yield') or 0) * 100)}</small></div><div class="rule"><b>本期信号</b><span>{esc(_signal_summary(ledger))}</span><br><small>只更新模型账本，不自动下单</small></div></div>
<div class="notice"><strong>当前执行状态：</strong>这是按固定规则生成的研究模型信号。先核验可持续性、连续分红、支付率、现金覆盖和 PR，再按真实股息率排序；数据缺失时保留原持仓并停止发布，不把缺数当卖出。20% 单票上限对照账本当前净资产为 {money(cap20['nav'])}。</div>
<div class="section-head"><h2>本期动作</h2><span class="hint">低买 / 高卖 / 保留</span></div><div class="table-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>动作</th><th>规则原因</th></tr></thead><tbody>{_action_rows(ledger)}</tbody></table></div>
<div class="section-head"><h2>当前模型持仓</h2><span class="hint">先过硬门槛，再按真实股息率降序</span></div><div class="table-wrap"><table><thead><tr><th>序</th><th>代码</th><th>名称 / 行业</th><th>可持续性</th><th class="num">真实股息率</th><th class="num">PR</th><th class="num">股数</th><th class="num">核验价</th><th class="num">市值</th><th class="num">权重</th></tr></thead><tbody>{_current_holding_rows(summary)}</tbody></table></div>
<div class="section-head"><h2>季度信号历史</h2><span class="hint">最近 8 次模型检查</span></div><div class="table-wrap"><table><thead><tr><th>季度</th><th>数据日期</th><th class="num">买入</th><th class="num">卖出</th><th class="num">持有</th><th class="num">数据缺口</th><th class="num">净资产</th><th class="num">现金</th></tr></thead><tbody>{_history_rows(ledger)}</tbody></table></div>
<div class="notice danger"><strong>不能保证未来收益：</strong>十年结果是固定现存候选池的历史回放，存在幸存者和样本选择偏差；本季度信号也会受公告修订、数据源变化、滑点、停牌、涨跌停、税费和真实成交价格影响。模型不连接券商、不读取账户、不自动交易。</div></section>
{AUTO_END}'''


def build_page(template: str, section: str, ledger: dict[str, Any]) -> str:
    if WEALTH_ANCHOR not in template:
        raise RuntimeError("初始报告模板缺少财富曲线锚点")
    page = template.replace(WEALTH_ANCHOR, section + WEALTH_ANCHOR, 1)
    page = page.replace(
        '<h2>从现在开始：放宽稳定性 10 万元建仓</h2><span class="hint">2026-08-24 当前快照',
        '<h2>初始模型建仓基线（2026-08-24）</h2><span class="hint">报告生成时的原始快照',
        1,
    )
    page = page.replace(
        "dividend_strategy_quarterly_backtest_100k_20260825.json",
        "audit.json",
    )
    page = page.replace(
        "dividend_strategy_quarterly_100k_20260825.html",
        "archive/2026-08-25-initial-ledger.html",
    )
    page = page.replace(
        "https://github.com/flyshub/dividend-calculator",
        "https://github.com/ainioayi/dividend-strategy-quarterly",
        1,
    )
    page = page.replace(
        "生成日期：2026-08-25（Asia/Shanghai）。",
        f"十年基线生成：2026-08-25；模型信号更新：{esc(ledger.get('as_of') or '-')}（Asia/Shanghai）。",
        1,
    )
    return page.replace(
        "DIVIDEND CALCULATOR · HISTORICAL CASH BACKTEST",
        "DIVIDEND STRATEGY · QUARTERLY MODEL SIGNAL",
        1,
    )


def build_audit(
    baseline: dict[str, Any],
    ledger: dict[str, Any],
    cap20_ledger: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    payload = deepcopy(baseline)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(microsecond=0).isoformat()
    payload.update({
        "site_schema_version": "2.0",
        "site_generated_at": now,
        "latest_model_period": ledger.get("last_processed_period"),
        "latest_model_as_of": ledger.get("as_of"),
        "model_ledgers": {
            "relaxed": ledger,
            "relaxed_cap20": cap20_ledger,
        },
        "automation": {
            **(config.get("automatic_update") or {}),
            "candidate_source": config.get("candidate_source"),
            "upstream_repository": config.get("upstream_repository"),
            "upstream_commit": config.get("upstream_commit"),
            "failure_policy": "候选或持仓数据不完整时停止发布并保留上一版网页",
        },
        "model_disclaimer": "研究用模拟账本，不连接券商、不读取真实账户、不自动下单、不承诺收益。",
    })
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()

    required = [
        BASELINE,
        CONFIG,
        args.template,
        LEDGER_DIR / "relaxed.json",
        LEDGER_DIR / "relaxed_cap20.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(
            "缺少报告输入，请先运行 scripts/bootstrap_state.py：" + "；".join(missing)
        )

    baseline = _load_json(BASELINE)
    config = _load_json(CONFIG)
    ledger = _load_json(LEDGER_DIR / "relaxed.json")
    cap20_ledger = _load_json(LEDGER_DIR / "relaxed_cap20.json")
    template = args.template.read_text(encoding="utf-8")

    page = build_page(template, build_signal_section(ledger, cap20_ledger), ledger)
    audit = build_audit(baseline, ledger, cap20_ledger, config)
    _atomic_write_text(args.index, page)
    _atomic_write_json(args.audit, audit)

    if not args.no_archive:
        period = str(ledger.get("last_processed_period") or "initial")
        as_of = str(ledger.get("as_of") or "unknown")
        archive = ROOT / "site" / "archive" / f"{period}-{as_of}.html"
        _atomic_write_text(archive, page)
        print(f"季度归档: {archive}")

    summary = _ledger_summary(ledger)
    print(f"首页: {args.index}")
    print(f"审计: {args.audit}")
    print(f"季度: {ledger.get('last_processed_period')}；数据截至: {ledger.get('as_of')}")
    print(f"模型净资产: {money(summary['nav'])}；持仓: {summary['holding_count']} 只")


if __name__ == "__main__":
    main()
