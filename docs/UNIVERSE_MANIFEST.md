# 候选池 Manifest

`data/universe_manifest.json` 是回测候选池的固定输入边界。缓存目录只是
数据存储，不能通过新增或删除 `kl_*.json` 自动改变回测样本。

## 当前版本

- `as_of`：`2026-08-25`
- 覆盖代码：210 只
- 动态池规则：连续正分红至少 3 年
- 记录哈希：`24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409`
- 行情格式：不复权收盘价（`unadjusted_close`）
- 行情主源标记：`sina_stock_zh_a_daily`
- 在线刷新失败时的回退：东财在线接口

manifest 的每条记录保存 K 线和逐笔分红的规范化哈希。回测启动时通过
`scripts/universe_manifest.py` 校验代码排序、记录哈希、截止日期和缓存内容；
不匹配会直接失败，不会静默换数据。

## 从固定缓存生成

`as-of` 必须由运行者明确给出，并且应与缓存标记和 manifest 一致：

```powershell
python scripts/build_universe_manifest.py `
  --from-cache data/backtest_cache `
  --as-of 2026-08-25 `
  --top 0 `
  --min-years 0 `
  --pool-min-years 3 `
  --output data/universe_manifest.json
```

`--top 0` 表示保留所有符合输入条件的缓存代码；`--min-years` 是 manifest
覆盖范围的最低分红年数，和回测运行时的 `pool_min_consecutive_years` 是两
个不同层次的规则。不要使用系统当前日期隐式生成历史输入。

## 月末日期

调仓日期由 `scripts/build_rebalance_dates.py` 根据 manifest 指定代码的缓存
交易日并集生成，并与 manifest 哈希绑定。当前日期文件为
`data/rebalance_dates_monthly.json`，共 128 个点，哈希为：

`f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e`

```powershell
python scripts/build_rebalance_dates.py `
  --manifest data/universe_manifest.json `
  --cache-dir data/backtest_cache `
  --as-of 2026-08-25 `
  --start-date 2016-01-01 `
  --output data/rebalance_dates_monthly.json
```

## 动态筛选时点

回测在每个信号日对 manifest 覆盖代码重新计算动态池：

1. 只读取该信号日已经存在的逐笔除权分红记录；
2. 检查连续年度是否完整，缺一年就不入池；
3. 已持仓股票即使暂时离开动态池，仍保留在核验和估值集合中；
4. 新入场再经过股息率、动量、行业和交易约束。

因此 manifest 不是事后挑出的持仓名单，而是冻结的数据覆盖范围；每月候选
数量会随当时已知的分红历史变化。当前审计中月度候选数最少 78、最多 169、
中位数 122。

## 重新刷新后的验收

刷新任何 K 线或分红明细后，必须按新的 `as_of` 重建 manifest 和日期文件，
再运行完整回测与测试。不能只替换单个缓存文件，也不能继续引用旧哈希对应
的 `current_best.json` 或实验结论。

## 历史股票池状态

`data/historical_universe_status.json` 是历史点时股票池补齐状态的权威机器文件。
截至 2026-08-27：

- 股票主数据 5,549 只，其中在市 5,212 只、退市 337 只；
- 2015 年后退市目标 255 只，分红查询 255/255 完成；
- 139 只曾连续三年正现金分红，价格查询 139/139 完成；
- 数据源流水线完成，但 `independently_verified=false`，因此总体状态仍是
  `incomplete`，`manifest_generation_allowed=false`。

`scripts/build_historical_v1_replay.py` 会把这些原始数据和冻结 V1 缓存复制到
临时的 `data/historical_v1_cache/` 后回放。该目录可重建，已由 Git 忽略；
可审计结果保存在 `data/historical_v1_provisional.json`。临时回放只补了
退市高息股票，没有重建 5,212 只在市股票的完整历史分红，因此不能生成正式
历史 manifest，也不能替换本页顶部的冻结 manifest。

## 前向输入隔离

模拟盘只使用 `data/forward/cache/` 和 `data/forward/inputs/`。初始前向缓存
来自冻结 V1，但后续月末刷新只写 `data/forward/`，不会修改
`data/backtest_cache/`、`data/universe_manifest.json` 或
`data/rebalance_dates_monthly.json`。每次信号和执行都会保存当时 manifest、
日期文件与缓存文件哈希，扩展输入如果改变了信号日前的历史内容会被拒绝。
