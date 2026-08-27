"""第 26 轮：日频 NAV 审计——核验月度口径是否低估真实最大回撤。

方法：
1. 运行标准月度回测（track_holdings=True），获取每个执行日的持仓快照。
2. 在相邻执行日之间，用缓存日频不复权收盘价逐日估值，并按真实除权日计入
   现金分红和送转股，得到日频 NAV 序列。
3. 对比日频与月度的最大回撤、波动率和 Sharpe。

不改变交易频率、策略规则或缓存数据。
"""
from __future__ import annotations
import json, sys, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import run_backtest, _compute_metrics, BACKTEST_RULES

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"
CACHE = ROOT / "data" / "backtest_cache"

BASE = {
    "entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
    "rebalance_threshold": 2.0, "execution_lag_days": 1,
    "pool_min_consecutive_years": 3, "momentum_months": 4,
    "momentum_threshold": 0.85, "reinvest_cash_reserve": 0,
    "rank_by": "yield", "momentum_periods": "", "max_yield": 999.0,
}


def load_klines():
    """加载全部缓存的日频 K 线。"""
    klines = {}
    for f in CACHE.glob("kl_*.json"):
        code = f.stem.split("_", 1)[1]
        try:
            klines[code] = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return klines


def load_dividend_details():
    """加载全部缓存的逐笔分红明细。"""
    details = {}
    for f in CACHE.glob("dvd_*.json"):
        code = f.stem.split("_", 1)[1]
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
            details[code] = rows
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return details


def build_daily_nav(nav_series, klines, dvd_details):
    """在月度 NAV 执行日之间用日频价格和真实分红填充日频 NAV。

    月度回测在执行日通过 _apply_precise_dividends 统一处理分红。
    日频路径在每个执行日重置为月度状态，然后逐日按真实除权日补入分红，
    以反映月内路径。
    """
    daily_points = []

    for i, entry in enumerate(nav_series):
        exec_date = entry["date"]
        cash = float(entry.get("cash", 0))
        holdings = entry.get("holdings") or {}

        # 生成所有持仓代码的交易日历并集（限本段区间）
        end_date = nav_series[i + 1]["date"] if i + 1 < len(nav_series) else None

        # 收集本段所有交易日
        all_dates = set()
        for code in holdings:
            kl = klines.get(code, {})
            for d in kl:
                if d >= exec_date and (end_date is None or d < end_date):
                    all_dates.add(d)
        all_dates.add(exec_date)
        if end_date:
            all_dates.add(end_date)

        trading_days = sorted(all_dates)

        # 追踪持仓的动态股数和现金（月内分红按真实除权日入账）
        dyn_shares = {c: float(h.get("shares", 0)) for c, h in holdings.items()}
        dyn_cash = cash
        credited_keys = set()

        for day in trading_days:
            # 按真实除权日处理分红
            for code in holdings:
                if dyn_shares.get(code, 0) <= 0:
                    continue
                for r in (dvd_details.get(code) or []):
                    ex_d = str(r.get("ex_date", ""))
                    key = (code, ex_d, r.get("year"))
                    if key in credited_keys:
                        continue
                    if ex_d == day and ex_d > exec_date and (end_date is None or ex_d < end_date):
                        shares_before = dyn_shares[code]
                        dps_val = float(r.get("dps", 0))
                        bonus = float(r.get("bonus_ratio", 0))
                        transfer = float(r.get("transfer_ratio", 0))
                        # 现金分红
                        if dps_val > 0:
                            dyn_cash += round(dps_val * shares_before / 10.0, 2)
                        # 送转股（按比例增加股数，以每股面值 1 元入账简化）
                        if bonus > 0 or transfer > 0:
                            new_shares = int(shares_before * (1 + bonus / 10.0 + transfer / 10.0))
                            dyn_shares[code] = float(new_shares)
                        credited_keys.add(key)

            # 日频估值
            mv = 0.0
            for code in holdings:
                shares = dyn_shares.get(code, 0)
                if shares <= 0:
                    continue
                kl = klines.get(code, {})
                price = kl.get(day)
                if price is not None:
                    mv += shares * float(price)
                else:
                    # 停牌日沿用最近交易日价格
                    nearby = [d for d in kl if d <= day]
                    if nearby:
                        nearest = max(nearby)
                        mv += shares * float(kl[nearest])

            daily_nav = round(dyn_cash + mv, 2)
            daily_points.append({"date": day, "nav": daily_nav})

    return daily_points


def max_drawdown_from_series(nav_series):
    """从 NAV 序列计算最大回撤。"""
    peak = float("-inf")
    max_dd = 0.0
    for p in nav_series:
        nav = float(p["nav"])
        if nav > peak:
            peak = nav
        if peak > 0:
            dd = (peak - nav) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100, 2)


def daily_sharpe(nav_series):
    """从日频 NAV 序列计算年化 Sharpe（假设 252 个交易日）。"""
    if len(nav_series) < 3:
        return 0.0
    rets = []
    for i in range(1, len(nav_series)):
        prev = float(nav_series[i - 1]["nav"])
        curr = float(nav_series[i]["nav"])
        if prev > 0:
            rets.append(curr / prev - 1.0)
    if not rets:
        return 0.0
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    std_r = math.sqrt(var_r)
    if std_r == 0:
        return 0.0
    return round(mean_r / std_r * math.sqrt(252), 3)


def main():
    dates_data = json.loads(DP.read_text(encoding="utf-8"))
    dates = dates_data.get("dates", dates_data)
    manifest = json.loads(MP.read_text(encoding="utf-8"))

    print("运行月度回测（捕获持仓快照）...")
    result = run_backtest(
        rules=BASE, dynamic_pool=True,
        manifest_path=str(MP), rebalance_dates=dates,
        verbose=False, track_holdings=True,
    )
    monthly_nav = result.get("nav_series") or []
    monthly_metrics = result.get("metrics") or {}

    print(f"月度 NAV 点数: {len(monthly_nav)}")
    print(f"月度 CAGR: {monthly_metrics.get('cagr')}%, 最大回撤: {monthly_metrics.get('max_drawdown')}%")

    # 验证持仓快照存在
    has_holdings = sum(1 for e in monthly_nav if e.get("holdings"))
    print(f"有持仓快照的执行日: {has_holdings}")

    print("加载日频缓存...")
    klines = load_klines()
    dvd_details = load_dividend_details()
    print(f"K 线缓存: {len(klines)} 只, 分红明细: {len(dvd_details)} 只")

    print("构建日频 NAV...")
    daily_nav = build_daily_nav(monthly_nav, klines, dvd_details)
    print(f"日频 NAV 点数: {len(daily_nav)}")

    # 计算日频指标
    daily_dd = max_drawdown_from_series(daily_nav)
    daily_sharpe_val = daily_sharpe(daily_nav)

    # 月频指标
    monthly_dd = monthly_metrics.get("max_drawdown", 0)
    monthly_sharpe = monthly_metrics.get("sharpe", 0)
    monthly_cagr = monthly_metrics.get("cagr", 0)

    # 日频 CAGR（按交易日数年化）
    if daily_nav and len(daily_nav) >= 2:
        start_nav = float(daily_nav[0]["nav"])
        end_nav = float(daily_nav[-1]["nav"])
        n_days = len(daily_nav)
        years = n_days / 252.0
        if years > 0 and start_nav > 0:
            daily_cagr = round(((end_nav / start_nav) ** (1.0 / years) - 1) * 100, 2)
        else:
            daily_cagr = 0.0
    else:
        daily_cagr = 0.0

    # 找最大回撤发生的日期区间
    peak_nav = float("-inf")
    peak_date = ""
    trough_date = ""
    current_peak_date = ""
    worst_dd = 0.0
    for p in daily_nav:
        nav = float(p["nav"])
        if nav > peak_nav:
            peak_nav = nav
            current_peak_date = p["date"]
        if peak_nav > 0:
            dd = (peak_nav - nav) / peak_nav
            if dd > worst_dd:
                worst_dd = dd
                trough_date = p["date"]
                peak_date = current_peak_date

    output = {
        "round": 26,
        "description": "daily_nav_intra_month_drawdown_audit",
        "method": (
            "在标准月度回测的持仓快照基础上，用日频不复权收盘价逐日估值，"
            "按真实除权日补入现金分红和送转股，对比日频与月度最大回撤。"
            "不改变交易频率、策略规则或缓存数据。"
        ),
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_data.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "base_rules": BASE,
        "monthly": {
            "nav_points": len(monthly_nav),
            "cagr": monthly_cagr,
            "max_drawdown": monthly_dd,
            "sharpe": monthly_sharpe,
        },
        "daily": {
            "nav_points": len(daily_nav),
            "cagr": daily_cagr,
            "max_drawdown": daily_dd,
            "sharpe": daily_sharpe_val,
            "max_drawdown_peak_date": peak_date,
            "max_drawdown_trough_date": trough_date,
        },
        "comparison": {
            "monthly_underestimates_daily_by_pp": round(daily_dd - monthly_dd, 2),
            "monthly_underestimates_daily_pct": round((daily_dd - monthly_dd) / daily_dd * 100, 1) if daily_dd > 0 else None,
            "daily_sharpe_vs_monthly_sharpe": round(daily_sharpe_val - monthly_sharpe, 3),
        },
        "limitation": (
            "日频 NAV 只在月度持仓快照之间做只读估值，不改变月度交易逻辑。"
            "停牌日沿用最近交易日收盘价近似。月内分红按真实除权日入账但不做再投资。"
            "日频波动率天然高于月频，Sharpe 不可直接比较。"
        ),
    }

    # 保存日频 NAV 序列供后续分析
    output["daily_nav_series"] = daily_nav

    out = ROOT / "data" / "round26_daily_nav_audit.json"
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n结果写入 {out}")
    print(f"月度：CAGR={monthly_cagr}%, 最大回撤={monthly_dd}%, Sharpe={monthly_sharpe}")
    print(f"日频：CAGR={daily_cagr}%, 最大回撤={daily_dd}%, Sharpe={daily_sharpe_val}")
    if daily_dd > 0:
        gap = daily_dd - monthly_dd
        print(f"月度低估日频：{gap:.2f} 个百分点 ({gap / daily_dd * 100:.1f}%)")
    print(f"最大回撤区间：{peak_date} → {trough_date}")


if __name__ == "__main__":
    main()
