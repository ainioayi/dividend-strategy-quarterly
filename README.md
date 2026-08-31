# 月度高息动量策略（动态池）

这是一个面向研究和审计的 A 股高股息策略项目。回测只使用已经落盘的
不复权收盘价、逐笔分红明细、版本化候选池和版本化月末日期；模型账本不
连接券商、不读取账户，也不会自动下单。

公开站点：<https://ainioayi.github.io/dividend-strategy-quarterly/>

当前权威状态见 [docs/STRATEGY_STATUS.md](docs/STRATEGY_STATUS.md)。公开站点同时展示
高息动量 V1（2只正式）、高息动量 V2（3只影子）、高息动量 V3（4只影子）、
高息动量 V5（附件规则影子）和多资产风险预算 V2.2（全球版影子）五套 10 万元
账户，以及与高息动量 V1 首笔模拟成交同日建仓的 510300 沪深300 ETF
基准；每套策略可单独查看累计盈亏、持仓、交易和账本指纹。历史回测曲线不会
混入前向模拟盘。
每轮参数、候选池和稳健性证据集中记录在
[docs/EXPLORATION_LOG.md](docs/EXPLORATION_LOG.md)。

## 当前候选

数据截止 `2026-08-25`，回测信号点为 128 个（`2016-01-29` 至
`2026-08-25`），信号在月末形成，默认在下一可用交易日收盘执行。

| 指标 | 当前值 |
| --- | ---: |
| CAGR | **41.38%** |
| 最大回撤 | 28.06% |
| Sharpe | 1.217 |
| 期末 NAV（10 万元起） | ¥3,876,245.51 |
| 交易次数 | 75 |
| 滚动 36 月最差 CAGR | 9.58% |
| 滚动 48 月最差 CAGR | 17.29% |

这些是固定历史样本上的模型结果，不是未来收益承诺。

## V5 独立影子

V5 不替换冻结 V1，也不是 V1 的参数变体。它在同一 210 只冻结覆盖集合上重建，
使用六只等权、行业唯一、派息覆盖、分红削减退出、60 日波动率门、双下行风险
阀门、现金计息和 H00922 240 日入场门。历史账户以 100 万元起步，前向影子账户
独立使用 10 万元。

| 指标 | V5 重建 | 三倍费用 |
|---|---:|---:|
| CAGR | 12.88% | 12.49% |
| 最大回撤 | 21.03% | 21.30% |
| Sharpe | 0.798 | 0.778 |
| 交易次数 | 198 | 200 |
| 滚动 36 月最差 CAGR | 1.63% | 1.25% |
| 滚动 48 月最差 CAGR | 3.83% | 3.51% |

附件报告的 16.40% 只作参考。本项目按当前冻结缓存、点时 EPS/行业证据和费用
口径重建，不声明精确复现。V5 输入指纹为
`2d19f3944d66b6e120357fabaac6508c595c3368ea2e858f8ea7839b26c3a6d2`；
冻结集合仍可能缺少退市股票，存在幸存者偏差。

## 多资产风险预算 V2.2（全球版影子）

V2.2 使用 510300、518880、513100 三只风险资产和 511010 国债 ETF：126 日正
动量门控，63 日逆波动率风险预算，组合波动目标 10%，剩余权重进入国债 ETF。
月末收盘计算信号，下一真实交易日开盘模拟成交，单边换手成本 0.1%。

| 指标 | 当前复算 | 三倍费用 |
|---|---:|---:|
| CAGR | 13.12% | 12.02% |
| 最大回撤 | 13.58% | 13.65% |
| Sharpe | 1.040 | 0.947 |
| 交易次数 | 418 | 418 |
| 滚动 36 月最差 CAGR | 4.32% | 2.94% |
| 滚动 48 月最差 CAGR | 6.68% | 5.68% |

冻结输入覆盖 3,182 个共同交易日和 24 条基金分红，指纹为
`e1a9236ccf872b41ac4fb4a2eca22fd5adb64ef6857ab1c0aa3f78d3cc07c994`。参考包没有
附带自检所需的 `weights_multi.csv` 和 `nav_multi.csv`，因此这里只声明规则和汇总
指标可复算，不声明逐点完全复现。V2.2 不替换高息动量 V1。

## 当前决策：冻结 V1，进入模拟盘观察

- V1 已冻结在提交 `c7d128ff0bc1b4b21c60bc7c6e2894dabf513fae`，参数、
  manifest、日期文件和截止日均不再针对这段历史样本调优。
- 第 30 轮脆弱性审计显示，剔除最大盈利股票后 CAGR（年化收益率）从
  41.38% 降至 26.55%，最大回撤从 28.06% 升至 47.45%；V1 明显依赖少数
  个股和周期行业，主策略暂不修改，但研究信心需要由新数据重新建立。
- 人工数据质量门禁历史回放覆盖 190 只在市股票和 47 只退市股票，共 237 只。
  同一缓存控制组精确复现 V1；过滤历史池的 CAGR 为 11.29%、最大回撤
  55.46%、Sharpe 为 0.416。15 只在市股票和 91 只退市候选因分红证据无法
  一一闭合而排除，过滤规则不读取回测盈亏。该结果反映人工可交易边界，仍有
  数据可得性偏差，不能称为全市场无偏回测，也不替换冻结 V1。
- 模拟盘采用只追加账本：2026-08-31 收盘后生成第一期信号，若下一真实
  交易日为 2026-09-01，高息动量策略在当日收盘模拟执行，V2.2 在当日开盘模拟
  成交并按收盘估值。此前账本保持空白
  是正确门禁。V1 对策略账户按 100% 目标投入，不设置额外现金保留；实际成交
  仍遵守 A 股 100 股整数手和交易费用，因此允许留下无法继续买入的现金尾差。
  计划持续观察 6–12 个月，期间 V1 参数冻结。用户明确要求的第 31 轮一次性
  持仓上限扫描已完成，但没有改变 V1。高息动量 V2/V3/V5 和多资产风险预算
  V2.2 已上线为影子观察，
  只能写入 `data/forward/shadow/`，不能改写 V1 账本。

## 公开业绩自动更新

- GitHub Actions 在每个工作日北京时间 18:30 首次运行，并在 20:30 做同日
  幂等重试；每次先校验五策略冻结合同和交易日历，再要求 510300
  已有当日收盘价。任一门禁不满足都不会发布旧数据冒充新数据。
- 自动任务失败时会创建或更新固定 GitHub Issue；后续运行恢复后自动关闭。
  公开页面超过一个工作日没有新数据时会显示“数据可能滞后”提示。
- 真实交易日会刷新五策略每日盯市净值、累计盈亏、持仓、交易明细和 510300
  含分红总回报，写入 `data/forward/performance.json` 和
  `site/performance.json`，随后自动部署 GitHub Pages。510300 只在 V1 首笔模拟
  成交日同步建仓，此前双方均保持 10 万元现金和 0% 收益。
- 月末信号和下一交易日模拟执行仍由独立的失败关闭门禁控制。季度模型任务只
  更新季度账本，不再重写公开首页。
- 每次正式任务开始前，先用 210 只正式前向缓存的隔离副本同时预演五策略的
  `2026-08-31` 信号、`2026-09-01` 执行和公开业绩生成。演练使用最近已知价
  平移出的模拟未来
  数据，只验证程序链路；它不写正式账本，也不是预测或提前选股。
- 可在真实日期到来前手动运行 GitHub 隔离演练：
  `gh workflow run monthly-forward.yml -f mode=rehearsal`。该模式会联网核验两个
  交易日并执行完整预演，但不会创建或关闭正式更新告警。最近一次本地审计结果
  保存在 `data/forward_first_cycle_rehearsal.json`。

## V1 主策略规则

- 候选覆盖 manifest 中的 210 只股票；每个信号日重新检查截至当日已经除权的分红记录，连续正分红至少 3 年才进入动态池。
- 候选池年度确认边界默认为 `pool_switch_month=7`；1–6 月使用更保守的前两年确认年度，7 月起才纳入上一年度。
- 用截至信号日可见的后置 DPS 和不复权收盘价计算真实股息率。真实股息率达到 `7.5%` 才允许新入场，低于 `5.5%` 才退出。
- 软退出默认只需 1 个已发生的月度信号确认；`exit_yield` 暂为理论触发价审计字段，不参与当前卖出路径。
- 新入场股票须满足当前价格 / 四个月前价格不低于 `0.85`；已有持仓不因短期动量过滤被强制卖出。
- 最多持有 2 只，按真实股息率排序；等权目标偏离超过 `2.0` 倍才再平衡。
- 送转股和分红按实际除权日入账，分红现金按持仓比例再投资整数手；手续费、印花税和过户费计入现金。模拟账户目标投入 100%，现金保留额为 0，但整数手和费用造成的尾差不强行成交。
- `stop_loss_pct=0`，不使用额外止损；缺少执行价时不成交，旧价格只用于估值。
- `dividend_information_lag_days=0` 明确记录当前点时口径；第 17 轮的 30/60/90 天保守延迟仅作压力测试。

## 复现

先确认 `data/backtest_cache/price_format.json` 的格式为
`unadjusted_close`，再运行：

```powershell
python scripts/backtest.py `
  --dynamic-pool `
  --manifest data/universe_manifest.json `
  --rebalance-dates data/rebalance_dates_monthly.json `
  --param entry_yield 7.5 `
  --param hold_yield 5.5 `
  --param momentum_months 4 `
  --param momentum_threshold 0.85 `
  --param pool_min_consecutive_years 3 `
  --param pool_switch_month 7 `
  --param max_holdings 2 `
  --param rebalance_threshold 2.0 `
  --param execution_lag_days 1 `
  --param dividend_information_lag_days 0 `
  --param reinvest_cash_reserve 0 `
  --json data/current_best.json
```

固定输入的哈希必须同时匹配：

- manifest records：`24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409`
- 月末日期：`f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e`

重建输入时显式指定截止日，不要让脚本使用系统当前日期：

```powershell
python scripts/build_universe_manifest.py `
  --from-cache data/backtest_cache `
  --as-of 2026-08-25 `
  --top 0 `
  --min-years 0 `
  --pool-min-years 3 `
  --output data/universe_manifest.json

python scripts/build_rebalance_dates.py `
  --manifest data/universe_manifest.json `
  --cache-dir data/backtest_cache `
  --as-of 2026-08-25 `
  --output data/rebalance_dates_monthly.json
```

## 本轮结果

第 7 轮（2026-08-26）只搜索当前参数的局部邻域，产物为
`data/round7_local.json`。实验共 21 组，均使用完整账本后再切连续 OOS，
不重新初始化账户。

动量阈值的稳健邻域为 `0.84–0.87`：

| 动量（月数 / 阈值） | CAGR | 最大回撤 | 滚动 36 月最差 | OOS 2021 起 |
| --- | ---: | ---: | ---: | ---: |
| 4 / 0.84 | 38.07% | 28.06% | 9.13% | 37.39% |
| **4 / 0.85** | **41.38%** | 28.06% | **9.58%** | **43.62%** |
| 4 / 0.86 | 38.92% | 28.06% | 9.07% | 38.98% |
| 4 / 0.87 | 37.14% | 28.06% | 9.03% | 35.65% |

入场线的对照也从同一个 `4/0.85` 基线开始；`entry_yield=7.5%`
仍为局部最优。此前基于旧实验默认 `momentum_threshold=0.95` 的数字已从
当前结论中移除。

## 数据审计

第 7 轮候选池审计写入 `data/round7_pool_audit.json`：210 只股票的 K 线和
分红缓存均存在，异常价格为 0；价格覆盖 `2015-01-05` 至 `2026-08-25`。
动态池月度数量最少 78、最多 169、中位数 122。

时间与执行审计写入 `data/round7_temporal_audit.json`，相关测试 32 项
全部通过，覆盖以下边界：

- `execution_lag_days=1` 只取信号日之后的下一缓存交易日；
- 信号只读信号日及之前的价格和分红；
- `ex_date <= entry_date` 的分红不入账，税期按实际除权日计算；
- 停牌时不以陈旧价格成交，陈旧价格仅作估值；
- 复投使用执行价并遵守现金保留额和仓位上限。

## 第 8–31 轮探索与当前决策

第 8 轮确认连续分红 3 年、入场线 7.5%、最多 2 只优于相邻候选；第 9
轮的动量排序、多周期动量、再平衡和高息上限对照均未改善综合结果。第 10–11
轮发现 `hold_yield=5.575%–5.675%` 的历史平台（5.6% 代表值 CAGR
42.90%），但第 12 轮真实重置窗口显示该提升只在 2018 起点明显，2020/2021
起点略逊，2022/2023 起点相同；约 3 倍交易成本时优势反转。因此当前主配置
仍保留 `hold_yield=5.5%`，5.6% 只作为挑战者，不把单一全样本峰值当成未来收益
承诺。

第 12 轮还为 `data/current_best.json` 增加了逐信号日 `pool_provenance`：记录
动态池数量、候选代码哈希和执行日期（末个信号点无下一交易日时为 `null`），
便于机器复核候选池和执行缺口。

第 13 轮测试高息上限 `12%–100%`，没有优于不设上限；严格 warm-up 窗口中
`hold_yield=5.6%` 只在多数起点略优、在 2022 年反转，因此仍不切换主规则。
第 14 轮的持仓动量退出和候选池连续性替代规则均未改善；冻结全体 manifest
的等权价格基准为 CAGR 3.53%，但不含分红、成本且有生存偏差，不能当作严格
总回报指数。第 15 轮的 DPS 不下降过滤降低 CAGR，固定池/无动量/无复投控制
仅为 8.65%，均不采纳。

第 16 轮验证了个股最新已知年度池、分红再投资贡献和稀疏调仓：替代池的 CAGR
仅 27.12%–28.99%，双月/三月调仓也没有同时改善收益与稳定性；复投版本在
当前账本为 41.38%，关闭复投降至 24.33%。第 17 轮将 `ex_date` 信息延迟真实
接入回测，30/60/90 天延迟的 CAGR 分别为 34.72%/27.51%/26.78%，滚动窗口
明显变差。收益率口径替代（近 12 个月已支付 DPS、已知上一年度 DPS）分别为
6.60% 和 35.25%，均低于当前 point-in-time 口径；18 组联合网格的 5.6% 全样本
峰值在重置窗口和三倍成本下不稳。因此主策略继续 `hold_yield=5.5%`，完整表格
和限制见 [EXPLORATION_LOG.md](docs/EXPLORATION_LOG.md)。

第 18 轮将候选池确认月份参数化：5/6 月切换使 CAGR 降至 26.56%/27.94%，
7 月与 8 月全样本相同，且 7 月规则更简单；信息延迟下的局部邻域和严格训练/测试
选参均未在滚动、重置和三倍费用口径同时超过基线。第 19 轮的多周期动量几何均值
和关闭动量均显著低于单 4 月动量；分红公告/登记字段审计发现当前缓存不完整，
暂不把未经时点核验的字段接入信号。集中度对照显示 `max_sector=1` 会显著降级，
而 `max_banks=1` 与取消行业上限在当前样本等价，生产仍保留 `2/2` 的简单约束。

第 20 轮的仓位上限和现金准备金只降低部分回撤，均牺牲 CAGR、滚动窗口或独立
起点表现；实际软退出参数比较显示 `hold_yield=5.5%`、单次月度确认的综合结果
最好。`hold_yield=5.3%` 在近期 OOS 和三倍费用下较强，但没有跨越独立区块；
配置中的 `exit_yield` 当前仅用于理论触发价审计，并未参与卖出判断。第 20 轮还
审计了 210 个分红缓存文件（3,159 条记录），未发现重复事件或负值异常。

第 21 轮将亏损持仓线设为 4.5%/5.0%/5.5%/6.0% 做窄实验：前三者逐笔交易
完全相同，6.0% 反而降级。2016–2019、2020–2022、2023–2026 非重叠区块对照
也未支持切换到 5.3% 挑战者。动态池年度字段同时增加了整数/数字字符串规范化，
不改变当前冻结缓存结果。完整表格和限制见 [docs/EXPLORATION_LOG.md](docs/EXPLORATION_LOG.md)。

第 22 轮复核连续分红 3/4/5 年与再平衡阈值 1.5/2.0/2.5：3 年门槛仍在
全样本、滚动窗口和连续 OOS 上综合最好；阈值 2.0 与 2.5 逐笔等价，1.5 只增加
交易并降低 CAGR，因此保持 3 年和 2.0。第 23 轮比较最多 1/2/3 只持仓，正常
成本 CAGR 分别为 23.67%/41.38%/40.68%；3 只只在三倍费用压力下局部占优，
不足以替换 2 只。第 24 轮的动量排序 CAGR 28.88%、最大回撤 47.45%，明显低于
按真实股息率排序的 41.38%/28.06%，继续使用 `rank_by=yield`。
第 25 轮尝试量化退市股票的幸存者偏差：东财 API 对 271 只 2015 年后退市股
均不返回分红历史，确认了免费数据源的覆盖缺口。幸存者偏差风险无法用现有
数据源量化或排除，后续需换用包含退市股完整历史的付费数据源。
第 26 轮日频 NAV 审计确认：月度最大回撤 28.06% 低估了真实日内回撤约
4.38pp（日频为 32.44%）。策略参数不变，但报告月度回撤时应标注此参考值。
第 27 轮将动量回看周期从 3 测试到 6 个月，4 个月在 CAGR、最大回撤、Sharpe、
滚动窗口、连续 OOS、三倍费用和所有重置窗口全面领先，是核心参数的强力确认。
第 28 轮测试波动率调整排序（收益率/波动率），CAGR 下降 8.5pp 且未改善回撤，
确认纯收益率排序仍为最优。
第 29 轮做收益归因分析：资本利得占总盈亏 80.5%、分红收入占 19.5%；
前 3 只持仓贡献 69%；18 只持仓中 14 只盈利（胜率 77.8%），平均持仓
约 353 天。主策略不变。

第 30 轮不调参数，只剔除头部盈利股和行业代理簇做脆弱性审计，并加入
510880 红利 ETF 含分红可交易基准。剔除最大盈利股后 CAGR 降至 26.55%、
最大回撤升至 47.45%；材料化工和汽车产业链代理簇被剔除后，滚动 48 月最差
CAGR 分别为 -4.66% 和 -5.31%。因此冻结 V1 做前向观察，不继续密集搜索。

第 31 轮按用户明确要求，将 `max_holdings` 从 2 扫描到 10，其余冻结规则不变。
2 只在正常成本下继续取得最高 CAGR 41.38%、Sharpe 1.217 和 2023 起连续
样本外 CAGR 24.74%；3 只的正常成本 CAGR 为 40.68%，但三倍费用 CAGR
40.29% 为全组最高。8–10 只把月频最大回撤降到约 22.8%，同时把 CAGR 降到
26.08%–26.78%，滚动 36 月最差 CAGR 仅 0.52%–1.11%。主策略继续保持 2 只；
3 只是历史证据中唯一接近 V1 的挑战者，现作为 V2 影子观察；4 只按用户要求
作为 V3 影子对照上线，但历史综合结果明显低于 V1，不视为推荐主策略。

历史股票池随后完成了人工数据质量门禁回放：只纳入分红和价格证据均能闭合的
237 只股票，结果为 CAGR 11.29%、最大回撤 55.46%。这进一步降低了对冻结
V1 历史高收益的信心。第 31 轮一次性持仓扫描没有消除该数据边界，主线仍是
用固定 V1 做模拟盘前向观察。

历史回测和实时季度模型是两层规则：回测把三项 PR 设为 999，以隔离纯股息率
策略；实时路径先用 `optimized_strategy.py` 的 `pr_ceiling=1.2`、连续分红 8 年
等硬门槛，再应用季度账本规则。不能把回测的 `entry_pr=999` 解释为实时模型取消
PR 门槛，也不能把实时的 8 年门槛倒灌到历史回测。

复现第 31 轮持仓上限扫描：

```powershell
python scripts/round31_holdings_sweep.py
```

复现第 32 轮 V5 重建：

```powershell
python scripts/v5_strategy.py `
  --input data/v5_inputs.json `
  --dates data/rebalance_dates_monthly.json `
  --cache-dir data/backtest_cache `
  --output data/round32_v5_rebuild.json `
  --initial-capital 1000000

python scripts/monthly_forward.py --strategy v5 verify
```

复现第 33 轮多资产风险预算 V2.2：

```powershell
python scripts/ma_v22_strategy.py `
  --input data/ma_v22_inputs.json `
  --output data/round33_ma_v22_rebuild.json `
  --initial-capital 1000000

python scripts/monthly_forward.py --strategy ma_v22 verify
```

## 运行检查

```powershell
python scripts/verify_v1_freeze.py
python scripts/rehearse_forward_cycle.py --verify-live-calendar
pytest -q
python -m compileall -q scripts tests
git diff --check
```

## 存档与限制

- `data/current_best.json`：当前主策略完整 NAV、交易和输入元数据。
- `data/current_lowdd.json`：低回撤对照，不作为主配置。
- `data/round5_walkforward.json`、`data/round6_robustness.json`、`data/round7_local.json`：参数与连续 OOS 实验。
- `data/round8_pool.json`、`data/round8_strategy.json`、`data/round8_temporal.json`：候选池、参数和时点审计。
- `data/round9_controls_agent.json`、`data/round9_config_audit.json`、`data/round9_data_quality.json`：控制变量、配置和缓存质量审计。
- `data/round10_micro.json`、`data/round11_hold_stability.json`、`data/round11_pool_stability.json`、`data/round11_simple_controls.json`：局部平台与简单规则复核。
- `data/round12_walkforward_hold.json`、`data/round12_cost_stress.json`：真实重置窗口和费用压力测试。
- `data/round13_simple_rule.json`、`data/round13_walkforward_strict.json`、`data/round13_audit.json`：高息上限、严格 warm-up 和时间审计。
- `data/round14_momentum_exit.json`、`data/round14_pool_continuity.json`、`data/round14_benchmark_audit.json`：退出、候选池和价格基准审计。
- `data/round15_dividend_quality.json`、`data/round15_benchmark_control.json`：分红质量过滤和控制对照。
- `data/round16_latest_known_pool.json`、`data/round16_reinvestment_control.json`、`data/round16_sparse_schedule.json`：候选池时点、复投贡献和稀疏频率。
- `data/round17_information_lag.json`、`data/round17_yield_definition.json`、`data/round17_joint_robustness.json`：信息延迟、收益率口径和联合稳健性。
- `data/round18_pool_switch.json`、`data/round18_lag_robustness.json`、`data/round18_train_test_selection.json`：候选池确认月份、延迟下邻域稳健性和严格训练/测试选参。
- `data/round19_momentum_periods.json`、`data/round19_dividend_timing_audit.json`、`data/round19_concentration.json`：多周期动量、分红时点字段审计和集中度约束对照，均包含窗口/成本边界或明确的数据缺口说明。
- `data/round20_position_reserve.json`、`data/round20_exit_confirmation.json`、`data/round20_dividend_quality_audit.json`：仓位/准备金、实际退出确认和分红缓存交叉审计。
- `data/round21_loss_hold.json`、`data/round21_hold_challenger.json`：亏损持仓线敏感性和 5.3% 挑战者非重叠区块复核。
- `data/round22_pool_continuity.json`、`data/round22_simple_controls.json`：连续分红年限和再平衡阈值复核。
- `data/round23_holdings.json`：持仓数量的完整账本、三倍费用和重置窗口复核。
- `data/round24_rank_by.json`：收益率排序与动量排序对照。
- `data/round25_survivorship_audit.json`：退市股票幸存者偏差审计（东财 API 覆盖缺口）。
- `data/round26_daily_nav_audit.json`：日频 NAV 审计（月度回撤 vs 日频回撤对照）。
- `data/round27_momentum_periods.json`：动量回看周期 3/4/5/6 个月对照。
- `data/round28_yield_vol_rank.json`：波动率调整排序对照（收益率/波动率 vs 纯收益率）。
- `data/round29_attribution.json`：收益归因分析（个股/分红vs资本利得/年度/集中度）。
- `data/round30_fragility_audit.json`：冻结 V1 的个股、行业和三倍费用脆弱性审计，以及 510880 含分红可交易基准。
- `data/round31_holdings_sweep.json`：`max_holdings=2..10` 的完整账本、连续样本外、滚动窗口、三倍费用和重置窗口比较。
- `data/round32_v5_rebuild.json`、`data/v5_inputs.json`、`data/v5_industries.json`：V5 重建结果、冻结辅助输入和历史行业分类证据。
- `data/round33_ma_v22_rebuild.json`、`data/ma_v22_inputs.json`：多资产风险预算 V2.2 的规则复算结果、四 ETF 行情、基金分红和参考附件指纹。
- `data/historical_universe_status.json`、`data/historical_filtered_manifest.json`、`data/historical_v1_filtered.json`：人工数据质量门禁状态、237 只过滤清单和正式回放结果；状态为 `complete_with_exclusions`，不是全市场无偏回测。
- `data/historical/eligible_listed_prices.json.gz`、`data/historical/eligible_listed_prices_manifest.json`、`data/historical/listed_dividends.json`：在市股票价格归档、价格核验清单和分红门禁证据。
- `data/historical_v1_provisional.json`：早期仅补入退市股票的临时压力回放，已由人工门禁正式回放取代，但保留作历史审计。
- `data/v1_freeze.json`：V1 提交、规则、输入和基线结果的固定指纹。
- `data/forward_first_cycle_rehearsal.json`：210 只缓存的五策略首期信号、执行、
  资金尾差和公开业绩隔离演练报告；使用模拟未来价格，只能证明程序链路。
- `data/forward/`：与冻结回测隔离的前向缓存、版本化输入和只追加模拟账本。
- `docs/UNIVERSE_MANIFEST.md`：候选池 manifest 的生成和校验规则。
- `site/`：五策略前向公开业绩页面和发布数据；旧季度页面保存在 `site/archive/`。

当前 210 只代码是截至截止日冻结的现存缓存集合，缺少退市股票和历史成分
变化，仍有幸存者偏差；月度 NAV 不包含月内路径，税费也不是逐笔 FIFO
税务模拟。刷新缓存、数据源、manifest 或日期文件后，必须重新计算哈希并
重跑全部实验。历史 CAGR 不代表未来收益，也不构成投资建议。

项目复用了 `flyshub/dividend-calculator` 的 GPL-3.0 代码和数据口径，继
续以 GPL-3.0 发布。
