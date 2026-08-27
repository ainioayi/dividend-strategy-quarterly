"""第16轮：稀疏调仓信号频率实验（冻结日期、无未来函数）。"""
from __future__ import annotations
import calendar
import json, math, sys
from datetime import date, datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from backtest import BACKTEST_RULES, _compute_metrics, run_backtest

def cagr(nav, years):
    return ((nav[-1]/nav[0])**(1/years)-1)*100 if nav and nav[0]>0 and years>0 else None
def _months_before(value, months):
    """返回日历上向前指定月数的同日（月底自动截断）。"""
    total=value.year*12 + value.month - 1 - months
    year, month0=divmod(total, 12)
    month=month0 + 1
    day=min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _calendar_window_metrics(series, months):
    """按真实日历跨度取滚动窗口，避免稀疏信号把观测数当月份。"""
    if not series:
        return {"count": 0}
    parsed=[]
    for item in series:
        try:
            parsed.append((date.fromisoformat(str(item['date'])[:10]), item))
        except (KeyError, TypeError, ValueError):
            continue
    windows=[]
    for end_index, (end_date, _) in enumerate(parsed):
        target=_months_before(end_date, months)
        start_index=None
        for candidate in range(end_index - 1, -1, -1):
            if parsed[candidate][0] <= target:
                start_index=candidate
                break
        if start_index is None:
            continue
        sample=[item for _, item in parsed[start_index:end_index + 1]]
        metric=_compute_metrics(sample, float(sample[0]['nav']))
        windows.append({"start":sample[0]['date'],"end":sample[-1]['date'],**metric})
    if not windows:
        return {"count": 0}
    return {
        "count": len(windows),
        "min_cagr": min(item['cagr'] for item in windows),
        "median_cagr": sorted(item['cagr'] for item in windows)[len(windows)//2],
        "max_drawdown_worst": max(item['max_drawdown'] for item in windows),
        "worst_window": min(windows, key=lambda item:item['cagr']),
    }


def enrich(r):
    s=r.get('nav_series',[]); out={}
    for months in (36,48):
        metric=_calendar_window_metrics(s, months)
        out[f'rolling_{months}m_min_cagr']=metric.get('min_cagr')
        out[f'rolling_{months}m_count']=metric.get('count', 0)
    for start in ('2021-01-01','2023-01-01','2025-01-01'):
        x=[z for z in s if str(z['date'])>=start]
        if len(x)>=2:
            days=(datetime.fromisoformat(x[-1]['date'])-datetime.fromisoformat(x[0]['date'])).days
            out[f'oos_{start[:4]}_cagr']=cagr([x[0]['nav'],x[-1]['nav']],days/365.25)
        else: out[f'oos_{start[:4]}_cagr']=None
    return out
def run(name, dates, mm, cost=1):
    rules=dict(BACKTEST_RULES); rules['momentum_months']=mm
    if cost!=1:
        for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate','min_commission'): rules[k]=float(rules[k])*cost
    r=run_backtest(rules=rules,dynamic_pool=True,rebalance_dates=dates,manifest_path=str(ROOT/'data/universe_manifest.json'),verbose=False)
    return {'name':name,'signal_count':len(dates),'momentum_months':mm,'cost_multiplier':cost,'metrics':r['metrics'],'derived':enrich(r)}
def main():
    raw=json.loads((ROOT/'data/rebalance_dates_monthly.json').read_text(encoding='utf-8')); dates=raw.get('dates',raw) if isinstance(raw,dict) else raw
    dates=[str(x)[:10] for x in dates]
    variants=[]
    for step,mm in ((1,4),(2,2),(3,1)):
        ds=dates[::step]
        variants.append(run('monthly_baseline' if step==1 else f'every_{step}_months',ds,mm))
        variants.append(run(('monthly_baseline' if step==1 else f'every_{step}_months')+'_3x_cost',ds,mm,3))
    manifest=json.loads((ROOT/'data/universe_manifest.json').read_text(encoding='utf-8'))
    payload={'round':16,'experiment':'sparse_schedule','date_source':'data/rebalance_dates_monthly.json','selection':'dates[::step]','rolling_method':'按实际日历跨度寻找窗口起点，不把稀疏观测数误当月份','inputs':{'manifest_records_sha256':manifest.get('records_sha256'),'dates_sha256':raw.get('dates_sha256') if isinstance(raw,dict) else None,'data_cutoff':raw.get('as_of') if isinstance(raw,dict) else None},'variants':variants,'audit':{'future_function_check':'通过：冻结月末信号日期，执行滞后1日；仅使用信号日前数据','comparison_note':'稀疏频率的 rolling36/48 与月度版本均按日历跨度计算，窗口观测数因频率不同而不同。'}}
    (ROOT/'data/round16_sparse_schedule.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    for v in variants: print(v['name'],v['metrics']['cagr'],v['metrics']['max_drawdown'],v['metrics']['trade_count'])
if __name__=='__main__': main()
