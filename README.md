# 股债交点图与历史策略研究

公开首页以累计收益百分比展示沪深300全收益与万得二级债基公开曲线，0%为共同起点；全部原始下穿都有编号和常显日期索引，可逐个放大。右上角分别显示实际共同“行情截至”日期和“最近成功检查”时间；源没有新行情时，检查时间更新，行情日期保持真实值。

公开页面：<https://ainioayi.github.io/dividend-strategy-quarterly/>

## 图表数据与复算

H00300.CSI历史来自中证指数官方全收益收盘数据；配置Tushare机密变量后，可使用同代码的较新尾部数据补充，必须先通过重叠点位和开市日期校验，不覆盖原有历史。885007.WI继续来自韭圈儿公开转发的原指数累计收益曲线，存在年度数值和日期疑点，页面保留质量说明，不能称为万得官方高精度复现。两侧不要求同一供应商，共同截至仍取实际都有数据的日期。

`config/stock_bond.json` 固定共同起点和冷却间隔，默认2004-12-31归一为1000、间隔126个交易日。自动更新会按当前配置重算交点、下拉选项和曲线，不会根据已知收益择优改规则。

```powershell
# 用保存的输入离线重算，再核对页面数据。
python scripts/stock_bond_page.py
python scripts/stock_bond_page.py --verify-only
# 显式联网刷新时写出截止日；实际共同日期可以更早。
python scripts/stock_bond_page.py --refresh --as-of 2026-11-13
```

独立输入、原始响应和指纹保存在 `data/stock-bond/`，不修改原高息策略冻结数据和模拟账户。触线后仍可能继续下跌，历史表现不保证未来收益，不构成买卖建议。项目不连接券商、不读取账户、不自动下单。

## 历史下穿产品研究（已从公开页面移除）

按用户要求，公开首页不再展示、加载或发布产品回放。下面的固定输入与结果仅保留在仓库供复核，不参与首页定时更新，也不阻塞图表发布。

用户已固定规则：510300 ETF主验证，160706 LOF单独补充2008年；每笔独立10万元，信号后下一可用交易日收盘一次买入，100份整手，分别持有6/12/24个月。买卖各计万三佣金、每笔最低5元，再各计0.05%滑点；分红现金不复投。费用翻倍和额外延迟两交易日作为固定压力检查，不按结果选择参数。

这是按事后公开曲线定义的条件性回放，没有证明债基信号当年何时可知，也不是实时可执行胜率。510300上市前事件不纳入ETF业绩，LOF与ETF不拼接。每个信号独立投入，窗口可能重叠，不能把逐笔结果当成一个10万元账户的连续收益。

价格统一采用完整新浪未复权场内日线，并与腾讯全历史对账；价格冲突、未核实的停牌或公司事件对应窗口会排除并列出原因。160706的部分场内分红日期已逐份按管理人公告核实，不使用场外日期冒充。除息后未到账分红计应收权益，现金到账后才转为可用资金；不重复加入复权收益。

来源原响应、管理人公告和指纹保存在 `data/crossing-assets/`，固定参数在 `config/crossing_entry_research.json`；完整结果与[独立报告](data/crossing-entry/report.md)在 `data/crossing-entry/`。当时使用的信号快照固定为同目录 `signals.json`，网页后续更新不改变该历史研究输入。

```powershell
# 从保存的原响应完整重建核验，不联网。
python scripts/refresh_crossing_assets.py --verify-only
# 按当时固定的信号和规则离线生成历史结果、报告，不发布网页。
python scripts/crossing_entry_research.py --signals data/crossing-entry/signals.json --output data/crossing-entry/result.json --summary-md data/crossing-entry/report.md
# 核对结果与输入、信号、参数一致。
python scripts/crossing_entry_research.py --signals data/crossing-entry/signals.json --output data/crossing-entry/result.json --verify-only
```

显式刷新真实产品数据：`python scripts/refresh_crossing_assets.py --refresh --as-of 2026-11-13`。所有研究输入与原高息策略隔离，不能把不完整观察期缩短后计为成功。

## 历史主策略结论

当前主策略是冻结的高息动量 V1。数据截止 `2026-08-25`，共 128 个信号点，
信号在月末形成，默认在下一可用交易日收盘模拟成交。

| 指标 | 冻结 V1 |
| --- | ---: |
| CAGR（年化收益率） | 41.38% |
| 最大回撤 | 28.06% |
| Sharpe（收益与波动的匹配程度） | 1.217 |
| 交易次数 | 75 |
| 滚动 36 月最差 CAGR | 9.58% |
| 滚动 48 月最差 CAGR | 17.29% |

这些数字来自冻结历史样本，不代表未来收益，也不构成投资建议。人工数据质量门禁
历史池的回放 CAGR 仅为 11.29%，说明结果对股票覆盖范围敏感。当前集合仍可能
缺少退市股票和历史成分变化，存在幸存者偏差；月度最大回撤也会忽略月内波动。

完整策略规则、输入指纹和各轮结论以 [策略状态](docs/STRATEGY_STATUS.md)为准。
详细实验见 [探索日志](docs/EXPLORATION_LOG.md)。

## 历史前向观察

项目独立跟踪五套 10 万元模拟账户：高息动量 V1（正式）、V2/V3/V5（影子）
和多资产风险预算 V2.2（影子），并以 510300 沪深 300 ETF 作基准。历史回测
不会混入模拟账户。观察期至少 6 个月、目标 12 个月，期间冻结规则，固定比较
收益、回撤、换手和费用，不根据短期结果临时改标准。

观察规则、价格日期和数据缺口处理见 [前向观察计划](docs/FORWARD_OBSERVATION.md)。
候选池输入规则见 [候选池清单说明](docs/UNIVERSE_MANIFEST.md)。

## 本地检查

安装项目现有 Python 依赖后，在 PowerShell 中运行：

```powershell
python -m pip install -r requirements.txt
python scripts/check_project.py
```

这条命令校验冻结输入指纹、五策略合同，在系统临时目录复算并完整比较
`data/current_best.json`，离线重建基金输入并复核条件性回放、股债图表与历史快照，再运行相关测试、完整测试、Python 编译和 Git 差异
检查。它默认不联网，也不会覆盖正式输入、结果或账本；失败时返回非零退出码，
并打印隔离产物路径。

需要额外验证五策略信号、成交和业绩生成链路时运行：

```powershell
python scripts/check_project.py --rehearsal
```

该演练使用隔离副本和模拟未来价格，只证明程序链路，不是预测或选股信号。

## 常用入口

```powershell
# strategy 可替换为 v2、v3、v5、ma_v22
python scripts/monthly_forward.py --strategy v1 verify
python scripts/round31_holdings_sweep.py
python scripts/v5_strategy.py --input data/v5_inputs.json --dates data/rebalance_dates_monthly.json --cache-dir data/backtest_cache --output data/round32_v5_rebuild.json --initial-capital 1000000
python scripts/ma_v22_strategy.py --input data/ma_v22_inputs.json --output data/round33_ma_v22_rebuild.json --initial-capital 1000000
```

继续实验前先读 [探索日志](docs/EXPLORATION_LOG.md)。修改候选池、截止日或输入
文件前先读 [候选池清单说明](docs/UNIVERSE_MANIFEST.md)，并保持基线和挑战者
使用相同输入、价格及费用口径。

## 自动更新与部署

`.github/workflows/pages.yml` 在北京时间周一至周五19:30主更新，周二至周六08:30补查上一交易日迟发的数据；两轮远超Tushare每小时一次的已知限频间隔。GitHub调度可能排队，来源也可能延迟；实际行情截至与成功检查时间分别展示。修改配置、采集代码或页面并推送到 `main` 后，也会用保存的输入重算发布。手动运行完整取数与部署：

Tushare token仅通过仓库Actions机密变量 `TUSHARE_TOKEN` 进入取数步骤，不写入代码、来源请求记录或公开JSON。每轮 `index_daily` 只请求一次H00300.CSI；指数身份已确定，不每日重复调用 `index_basic`。如遇限频或接口失败，保留已验证的补充缓存或现有来源，不提高调用频率、不把失败当新行情。积分不按每次请求扣减，但可调用接口与每日总量仍取决于账户服务端权限，不能把股票 `daily` 的配额套用于指数 `index_daily`。

排队任务启动时读取最新 `main`，不使用排队前的旧提交；打包前再次核对远端版本。如果运行期间 `main` 已变化，就停止旧快照发布，由最新任务重新计算和校验，避免网页数据倒退。

```powershell
gh workflow run pages.yml --ref main
```

公开部署只打包 `site/index.html`、`site/chart.css`、`site/chart.js`、`site/stock-bond.json`。定时任务刷新股债信号输入，按当前参数重算校验后发布；即使行情日期不变，也保存这次成功检查的真实取数时间。ETF/LOF历史研究不再自动取数、重算或公开打包。关键门禁失败时线上保持上次成功页面；检查成功不代表已经解决供应商的日期疑点。旧业绩、审计和产品回放不再打包到公网。

发布门禁、停牌核验和历史净值更正流程见 [公开数据运维](docs/FORWARD_OPERATIONS.md)。

## 主要文件

- `data/current_best.json`：冻结 V1 的完整回测结果。
- `data/v1_freeze.json`：V1 参数、输入和结果指纹。
- `data/forward/`：前向缓存、版本化输入和只追加模拟账本。
- `site/`：股债交点页面与发布数据；旧研究JSON不打包。
- `data/stock-bond/`、`config/stock_bond.json`：独立曲线输入、来源指纹与固定规则。
- `docs/STRATEGY_STATUS.md`：当前结果的唯一权威说明。
- `docs/EXPLORATION_LOG.md`：历轮实验、限制和取舍。

项目沿用 `flyshub/dividend-calculator` 的 GPL-3.0 代码和数据口径，并继续以
GPL-3.0 发布。
