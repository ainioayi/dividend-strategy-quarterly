"""第17轮：真实股息率口径稳健性（冻结逐笔除权日）。"""
import json, sys
from datetime import datetime, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
import backtest
import hashlib
from round3_experiments import _window_metrics, _continuous_metrics
def _oos(nav,start):
    return _continuous_metrics(nav,start)

ORIG=backtest.build_snapshot
def install(mode):
    def snap(code,price,hist,as_of,detail=None):
        row=ORIG(code,price,hist,as_of,detail)
        dps=None
        if detail:
            if mode=='paid12m':
                cutoff=(datetime.fromisoformat(as_of)-timedelta(days=365)).date().isoformat()
                dps=sum(float(x.get('dps') or 0) for x in detail if cutoff<=str(x.get('ex_date') or '')[:10]<=as_of)
            elif mode=='annual_latest':
                # 只从已除权明细取上一自然年及更早的最新年度，不能使用
                # 年度汇总接口中可能尚未实施的未来分配。
                known=[]
                for x in detail:
                    ex=str(x.get('ex_date') or '')[:10]
                    try:
                        year=int(x.get('year', 0)); value=float(x.get('dps') or 0)
                    except (TypeError, ValueError):
                        continue
                    if len(ex)==10 and ex<=as_of and year<=int(as_of[:4])-1 and value>0:
                        known.append((year,value))
                latest=max((year for year,_ in known), default=None)
                dps=sum(value for year,value in known if year==latest) if latest is not None else None
        if dps is not None:
            row['dps']=dps; row['yield']=row['real_yield']=dps/price*100 if price>0 else None
        return row
    backtest.build_snapshot=snap
def main():
    raw=json.loads((ROOT/'data/rebalance_dates_monthly.json').read_text(encoding='utf-8')); dates=raw.get('dates',raw) if isinstance(raw,dict) else raw
    variants=[]
    for mode in ('baseline','paid12m','annual_latest'):
        if mode!='baseline': install(mode)
        rr=[]
        for mult in (1,3):
            rules=dict(backtest.BACKTEST_RULES)
            if mult==3:
                for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate','min_commission'): rules[k]=float(rules[k])*3
            x=backtest.run_backtest(rules=rules,dynamic_pool=True,rebalance_dates=dates,manifest_path=str(ROOT/'data/universe_manifest.json'),verbose=False)
            rr.append({'cost_multiplier':mult,'metrics':x['metrics'],'rolling36':_window_metrics(x['nav_series'],36),'rolling48':_window_metrics(x['nav_series'],48),'oos':{y:_oos(x['nav_series'],y+'-01-01') for y in ('2021','2023','2025')}})
        variants.append({'name':mode,'runs':rr})
    manifest=json.loads((ROOT/'data/universe_manifest.json').read_text(encoding='utf-8'))
    out={'round':17,'experiment':'yield_definition','variants':variants,'input':{'rebalance_dates_sha256':hashlib.sha256(json.dumps(dates,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),'dates_sha256':raw.get('dates_sha256') if isinstance(raw,dict) else None,'manifest':'data/universe_manifest.json','manifest_records_sha256':manifest.get('records_sha256'),'data_cutoff':manifest.get('as_of')},'audit':'逐笔 ex_date 仅使用 <= signal_date；annual_latest 定义为 signal_date 年份前一自然年及更早记录中已知 ex_date 的最新年度 DPS；monkeypatch 仅限本实验脚本；未使用未来数据。OOS 使用完整账本连续切片，不重新初始化现金和持仓。'}
    (ROOT/'data/round17_yield_definition.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print([(v['name'],v['runs'][0]['metrics']['cagr'],v['runs'][0]['metrics']['max_drawdown']) for v in variants])
if __name__=='__main__': main()
