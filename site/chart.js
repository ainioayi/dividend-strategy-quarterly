'use strict';

const $ = selector => document.querySelector(selector);
let data;
let zoomed = false;
let downPoints = [];
let research;
let researchLoading = false;

function validate(payload) {
  const { chart, parameters, signals, crossings, quality } = payload;
  const isDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
  const isValue = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
  if (payload.schema_version !== 1 || !isDate(payload.as_of) || !parameters ||
      !isDate(parameters.base_date) || !Number.isInteger(parameters.cooldown_sessions) ||
      parameters.cooldown_sessions < 1 || !chart || !Array.isArray(chart.dates) ||
      chart.dates.length < 2 || !Array.isArray(chart.stock) || !Array.isArray(chart.bond) ||
      chart.stock.length !== chart.dates.length || chart.bond.length !== chart.dates.length ||
      chart.dates.some((date, index) => !isDate(date) || (index && date <= chart.dates[index - 1])) ||
      chart.stock.some(value => !isValue(value)) ||
      chart.bond.some(value => value !== null && !isValue(value)) ||
      !chart.bond.some(isValue) || !Array.isArray(signals) || !Array.isArray(crossings) ||
      !quality || !Array.isArray(quality.warnings) || quality.warnings.some(value => typeof value !== 'string')) {
    throw new Error('数据格式不完整');
  }
  for (const point of [...signals, ...crossings]) {
    if (!Number.isInteger(point.index) || point.index < 0 || point.index >= chart.dates.length ||
        point.date !== chart.dates[point.index] || chart.bond[point.index] === null ||
        !['down', 'up'].includes(point.direction)) throw new Error('交点与曲线日期不一致');
  }
  if (signals.some((point, index) => point.direction !== 'down' || (index && point.index <= signals[index - 1].index))) {
    throw new Error('保留下穿点的顺序不正确');
  }
  const lastCommon = chart.dates.findLastIndex((date, index) => chart.bond[index] !== null);
  if (payload.as_of !== chart.dates[lastCommon]) throw new Error('数据截止日与共同日期不一致');
  return payload;
}

function svgElement(tag, attributes, content) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (content !== undefined) element.textContent = content;
  return element;
}

function axisValue(value) { return value / 10 - 100; }

function axisText(value) {
  const percent = axisValue(value);
  return `${percent > 0 ? '+' : ''}${percent.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%`;
}

function choosePoint(index, enlarge) {
  $('#real-signal').value = String(index);
  if (enlarge) zoomed = true;
  drawReal();
  if (enlarge) $('#real-frame').scrollIntoView({ block: 'start', behavior: 'auto' });
}

function drawReal() {
  if (!data) return;
  const { chart } = data;
  const selected = Number($('#real-signal').value);
  const chosen = downPoints[selected];
  const narrow = window.innerWidth < 680;
  const width = narrow ? 400 : 1000;
  const left = 65;
  const right = width - 34;
  const first = zoomed && chosen ? Math.max(0, chosen.index - 20) : 0;
  const last = zoomed && chosen ? Math.min(chart.dates.length - 1, chosen.index + 60) : chart.dates.length - 1;
  const x = index => left + (index - first) * (right - left) / Math.max(1, last - first);
  const visible = downPoints.map((point, index) => ({ ...point, number: index + 1 }))
    .filter(point => point.index >= first && point.index <= last);
  // 横向挨得太近的编号分层排列，保留全部原始点及其引线。
  const laneEnds = [];
  for (const point of visible) {
    let lane = laneEnds.findIndex(end => x(point.index) - end >= 25);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = x(point.index);
    point.lane = lane;
  }
  const plotTop = 85 + Math.max(1, laneEnds.length) * 23;
  const plotBottom = plotTop + (narrow ? 285 : 265);
  const height = plotBottom + 45;
  let low = 1000;
  let high = 1000;
  for (let index = first; index <= last; index++) {
    for (const value of [chart.stock[index], chart.bond[index]]) {
      if (value === null) continue;
      low = Math.min(low, value);
      high = Math.max(high, value);
    }
  }
  const rawStep = Math.max(1, (high - low) / 5);
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = [1, 2, 5, 10].find(multiplier => multiplier * magnitude >= rawStep) * magnitude;
  const minimum = Math.min(1000, Math.floor(low / Math.min(step, 500)) * Math.min(step, 500));
  const maximum = 1000 + Math.max(1, Math.ceil((high - 1000) / step)) * step;
  const y = value => plotBottom - (value - minimum) / (maximum - minimum) * (plotBottom - plotTop);
  const svg = $('#real-chart');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.replaceChildren(
    svgElement('title', { id: 'real-chart-title' }, '两条指数自共同起点以来的累计收益'),
    svgElement('desc', { id: 'real-chart-desc' }, `显示 ${chart.dates[first]} 至 ${chart.dates[last]}。${visible.length}个下穿编号对应图下全部日期；原始日期待核验，不是买入指令。债基缺失日期断线，不填补。`),
    svgElement('text', { class: 'axis-heading', x: left, y: 23 }, '累计收益（%）'),
    svgElement('text', { class: 'chart-label', x: left, y: 47 }, '0%＝共同起点；编号见下方日期'),
  );
  if (chosen) svg.append(svgElement('text', { class: 'selection-label', x: right, y: narrow ? 70 : 23, 'text-anchor': 'end' }, `#${selected + 1} · ${chosen.date}`));
  const grid = svgElement('g', { id: 'real-grid' });
  const yTicks = new Set([minimum, maximum, 1000]);
  for (let value = 1000; value < maximum; value += step) yTicks.add(value);
  for (let value = 1000 - step; value > minimum; value -= step) yTicks.add(value);
  for (const value of [...yTicks].sort((a, b) => a - b)) {
    const zero = Math.abs(value - 1000) < .001;
    grid.append(
      svgElement('line', { x1: left, x2: right, y1: y(value), y2: y(value), stroke: zero ? '#9aaf9a' : '#e3e8df', 'stroke-dasharray': zero ? '4 4' : '', 'data-zero': String(zero) }),
      svgElement('text', { class: 'chart-label', x: left - 12, y: y(value) + 4, 'text-anchor': 'end' }, axisText(value)),
    );
  }
  const ticks = narrow ? 3 : 6;
  for (let tick = 0; tick < ticks; tick++) {
    const index = Math.round(first + (last - first) * tick / (ticks - 1));
    grid.append(svgElement('text', { class: 'chart-label', x: x(index), y: plotBottom + 28, 'text-anchor': 'middle', 'data-x-tick': true }, chart.dates[index].slice(0, 7)));
  }
  svg.append(grid);
  for (const [id, series, color] of [['real-stock', chart.stock, '#245b45'], ['real-bond', chart.bond, '#94714f']]) {
    let path = '';
    let connected = false;
    for (let index = first; index <= last; index++) {
      if (series[index] === null) { connected = false; continue; }
      path += `${connected ? 'L' : 'M'}${x(index).toFixed(2)},${y(series[index]).toFixed(2)} `;
      connected = true;
    }
    svg.append(svgElement('path', { id, d: path, stroke: color, 'stroke-width': 1.9, fill: 'none', 'stroke-linejoin': 'round' }));
  }
  const leaders = svgElement('g', { id: 'real-leaders' });
  const points = svgElement('g', { id: 'real-points' });
  const badges = svgElement('g', { id: 'real-badges' });
  for (const point of visible) {
    const active = point.number === selected + 1;
    const badgeY = (narrow ? 95 : 70) + point.lane * 23;
    leaders.append(svgElement('line', { x1: x(point.index), x2: x(point.index), y1: badgeY + 10, y2: y(chart.stock[point.index]), stroke: active ? '#a84836' : '#b7bfb4', 'stroke-width': active ? 1 : .55, 'stroke-dasharray': '3 4' }));
    points.append(svgElement('circle', { cx: x(point.index), cy: y(chart.stock[point.index]), r: active ? 5.5 : 3.2, fill: active ? 'white' : '#a84836', stroke: '#a84836', 'stroke-width': 1.8, 'data-point': point.number }));
    badges.append(
      svgElement('circle', { cx: x(point.index), cy: badgeY, r: 9.5, fill: active ? '#a84836' : 'white', stroke: '#a84836', 'data-badge': point.number }),
      svgElement('text', { class: active ? 'badge-number active' : 'badge-number', x: x(point.index), y: badgeY + 3.7, 'text-anchor': 'middle' }, point.number),
    );
  }
  svg.append(leaders, points, badges);
  $('#real-range').textContent = zoomed ? '查看全历史' : '放大选中点';
  $('#real-range').setAttribute('aria-pressed', String(zoomed));
  document.querySelectorAll('.date-button').forEach((button, index) => button.setAttribute('aria-pressed', String(index === selected)));
  $('#real-chart-period').textContent = `显示 ${chart.dates[first]} — ${chart.dates[last]}${chosen ? '；选中 #' + (selected + 1) + ' · ' + chosen.date : '；当前没有下穿点'}。${visible.length}个下穿点在当前范围内。债基缺失日期断线，不填补；原始日期仍待核验。`;
  if (research) renderSelectedRisk();
}

function buildDateIndex() {
  $('#dates').replaceChildren();
  let year;
  let group;
  downPoints.forEach((point, index) => {
    if (year !== point.date.slice(0, 4)) {
      year = point.date.slice(0, 4);
      const section = document.createElement('section');
      section.className = 'date-year';
      const heading = document.createElement('h3');
      heading.textContent = year + '年';
      group = document.createElement('div');
      group.className = 'date-grid';
      section.append(heading, group);
      $('#dates').append(section);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'date-button';
    button.setAttribute('aria-label', `放大第${index + 1}个下穿点，${point.date}，原始日期待核验`);
    const number = document.createElement('span');
    number.className = 'date-number';
    number.textContent = '#' + String(index + 1).padStart(2, '0');
    const date = document.createElement('span');
    date.textContent = point.date;
    button.append(number, date);
    button.addEventListener('click', () => choosePoint(index, true));
    group.append(button);
  });
  $('#date-count').textContent = `${downPoints.length}个原始下穿 · 完整日期常显`;
  $('#point-count').textContent = `原始下穿 · ${downPoints.length}次（待核验）`;
}

function renderQuality() {
  const lines = [...data.quality.warnings];
  const sensitive = data.crossings.filter(point => point.direction === 'down' && point.precision_sensitive).map(point => point.date);
  if (sensitive.length) lines.push(`公布小数精度的末位扰动可能改变${sensitive.length}个下穿点的相对高低：${sensitive.join('、')}。这只是敏感性检查，不是统计置信度。`);
  const details = data.quality.verification_details;
  if (Array.isArray(details)) lines.push(...details.filter(value => typeof value === 'string'));
  else if (typeof details === 'string') lines.push(details);
  $('#quality-warnings').replaceChildren(...lines.map(text => {
    const paragraph = document.createElement('p');
    paragraph.textContent = text;
    return paragraph;
  }));
  const dates = data.source_dates;
  const acquired = dates?.checked_at && Number.isFinite(Date.parse(dates.checked_at))
    ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short' }).format(new Date(dates.checked_at)) + '（北京时间）'
    : '未提供';
  $('#source-dates').textContent = dates
    ? `来源数据：股票截至 ${dates.stock_as_of || '未提供'}；债基截至 ${dates.bond_as_of || '未提供'}。快照取得时间：${acquired}。来源日期不等于日期口径已核验。`
    : '两侧来源日期与快照取得时间尚未提供；共同截止日不代表债基日期口径已核验。';
  $('#axis-explanation').textContent = `自 ${data.parameters.base_date} 起的累计收益。0%＝共同起点；+100%＝翻倍；+400%＝原来的5倍。`;
}

async function loadData() {
  data = undefined;
  downPoints = [];
  research = undefined;
  zoomed = false;
  $('#real-frame').hidden = true;
  $('#entry-research').hidden = true;
  $('#research-results').hidden = true;
  $('#retry').hidden = true;
  $('#load-status').hidden = false;
  $('#load-status').classList.remove('error');
  $('#load-status').textContent = '正在加载历史曲线…';
  $('#data-as-of').textContent = '加载中';
  $('#data-as-of').removeAttribute('datetime');
  $('#quality-warnings').replaceChildren();
  $('#source-dates').textContent = '';
  try {
    const response = await fetch('stock-bond.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('数据请求失败');
    data = validate(await response.json());
    downPoints = data.crossings.filter(point => point.direction === 'down');
    $('#data-as-of').textContent = data.as_of;
    $('#data-as-of').dateTime = data.as_of;
    renderQuality();
    $('#real-signal').replaceChildren(...downPoints.map((point, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = `#${index + 1} · ${point.date}`;
      return option;
    }));
    $('#real-signal').value = String(Math.max(0, downPoints.length - 1));
    $('#real-signal').disabled = !downPoints.length;
    $('#real-range').disabled = !downPoints.length;
    buildDateIndex();
    $('#real-frame').hidden = false;
    drawReal();
    $('#load-status').hidden = true;
    $('#entry-research').hidden = false;
    if ($('#entry-research').open) loadResearch();
  } catch (error) {
    data = undefined;
    $('#real-frame').hidden = true;
    $('#data-as-of').textContent = '未取得';
    $('#data-as-of').removeAttribute('datetime');
    $('#load-status').classList.add('error');
    $('#load-status').textContent = '历史曲线加载失败，暂时无法显示有效数据。请检查网络后重新加载。';
    $('#retry').hidden = false;
    console.error('股债曲线加载失败：', error);
  }
}

function validateResearch(payload) {
  if (payload.schema_version !== 1 || !payload.methodology || payload.methodology.conditional !== true ||
      typeof payload.methodology.label !== 'string' || !payload.assets || !Array.isArray(payload.limitations)) {
    throw new Error('回放数据格式不完整');
  }
  const dates = downPoints.map(point => point.date);
  const context = payload.signal_context;
  if (!context || context.as_of !== data.as_of || context.parameters_sha256 !== data.audit?.parameters_sha256 ||
      context.manifest_sha256 !== data.audit?.manifest_sha256 ||
      JSON.stringify(context.raw_downcrossing_dates) !== JSON.stringify(dates)) {
    throw new Error('回放与当前曲线版本不同，等待重新计算');
  }
  for (const asset of Object.values(payload.assets)) {
    if (!asset || typeof asset.name !== 'string' || typeof asset.type !== 'string' ||
        !Array.isArray(asset.rows) || asset.rows.length !== dates.length ||
        asset.rows.some((row, index) => row.signal_date !== dates[index] ||
          !['complete', 'partial', 'incomplete', 'excluded'].includes(row.status))) throw new Error('回放事件与当前图表不一致');
    for (const row of asset.rows) {
      for (const months of ['6', '12', '24']) {
        const value = row.horizons?.[months];
        if (value && (value.status !== 'complete' || !Number.isFinite(value.net_return) ||
            !Number.isFinite(value.max_drawdown) || value.max_drawdown > 0)) throw new Error('回放收益或回撤无效');
      }
    }
  }
  if (!Object.keys(payload.assets).length) throw new Error('尚无产品回放结果');
  return payload;
}

function showReturn(value) {
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`;
}

function researchCell(value, isPercent = false) {
  const cell = document.createElement('td');
  if (isPercent) {
    cell.textContent = Number.isFinite(value) ? showReturn(value) : '未提供';
    if (Number.isFinite(value)) cell.className = value < 0 ? 'negative' : 'positive';
  } else cell.textContent = String(value);
  return cell;
}

function renderSelectedRisk() {
  if (!research) return;
  const code = $('#research-asset').value;
  const asset = research.assets[code];
  const point = downPoints[Number($('#real-signal').value)];
  const row = asset.rows.find(item => item.signal_date === point?.date);
  $('#selected-risk-title').textContent = `当前选中下穿的逐期限风险${point ? ' · ' + point.date : ''}`;
  $('#selected-risk-rows').replaceChildren(...['6', '12', '24'].map(months => {
    const tr = document.createElement('tr');
    tr.dataset.horizon = months;
    tr.append(researchCell(months + '个月'));
    const result = row?.horizons?.[months];
    if (result) {
      tr.append(researchCell(result.net_return, true), researchCell(result.min_from_capital, true), researchCell(result.max_drawdown, true));
      const days = result.first_break_even_calendar_days;
      tr.append(researchCell(Number.isFinite(days) ? days + '天' : days === null ? '观察期内未回本' : '回本等待未提供'));
      tr.append(researchCell('条件性回放 · 信号日期待核验'));
    } else {
      for (let column = 0; column < 4; column++) tr.append(researchCell('—'));
      tr.append(researchCell(row?.horizon_reasons?.[months] || row?.reason || '没有完整可核验结果'));
    }
    return tr;
  }));
}

function renderComparisons(asset) {
  const scenarios = [
    ['正常候选', asset.summary?.raw],
    ['普通日期对照', asset.comparisons],
    ['双倍费用', asset.stress?.double_costs?.summary?.raw],
    ['额外延迟2个交易日', asset.stress?.extra_delay_2_sessions?.summary?.raw],
  ];
  const rows = [];
  for (const months of ['6', '12', '24']) for (const [name, summary] of scenarios) {
    const result = summary?.[months];
    const tr = document.createElement('tr');
    tr.dataset.horizon = months;
    tr.dataset.scenario = name;
    tr.append(researchCell(months + '个月'), researchCell(name), researchCell(Number.isInteger(result?.observations) ? result.observations : '未提供'));
    for (const key of ['mean_net_return', 'median_net_return', 'worst_net_return', 'worst_drawdown']) tr.append(researchCell(result?.[key], true));
    rows.push(tr);
  }
  $('#comparison-rows').replaceChildren(...rows);
}

function renderResearch() {
  if (!research) return;
  const code = $('#research-asset').value;
  const asset = research.assets[code];
  $('#research-basis').textContent = `${asset.name}（${code}，${asset.type}）；上市日期 ${asset.listing_date}；实际产品数据截至 ${asset.as_of}。${research.methodology.label}`;
  $('#research-rows').replaceChildren(...asset.rows.map(row => {
    const tr = document.createElement('tr');
    const date = document.createElement('td');
    date.textContent = row.signal_date;
    tr.append(date);
    for (const months of ['6', '12', '24']) {
      const cell = document.createElement('td');
      const value = row.horizons?.[months];
      if (value) {
        cell.textContent = showReturn(value.net_return);
        cell.className = value.net_return < 0 ? 'negative' : 'positive';
      } else cell.textContent = '—';
      tr.append(cell);
    }
    const drawdown = document.createElement('td');
    const value24 = row.horizons?.['24'];
    drawdown.textContent = value24 ? showReturn(value24.max_drawdown) : '—';
    const reason = document.createElement('td');
    const reasons = [];
    if (row.status === 'excluded') reasons.push('已排除');
    else if (['incomplete', 'partial'].includes(row.status)) reasons.push('部分窗口不完整');
    else reasons.push('条件性回放 · 日期待核验');
    if (row.reason) reasons.push(row.reason);
    if (row.status !== 'excluded') for (const months of ['6', '12', '24']) {
      if (!row.horizons?.[months]) reasons.push(`${months}个月：${row.horizon_reasons?.[months] || row.reason || '没有完整可核验结果'}`);
    }
    reason.textContent = [...new Set(reasons)].join('；');
    tr.append(drawdown, reason);
    return tr;
  }));
  const parameters = research.methodology.parameters;
  const costs = parameters && ['initial_capital', 'lot_size', 'commission_rate', 'minimum_commission', 'slippage_rate'].every(key => Number.isFinite(parameters[key]))
    ? `每笔${parameters.initial_capital.toLocaleString('zh-CN')}元，${parameters.lot_size}份整手；买卖各${(parameters.commission_rate * 100).toFixed(2)}%佣金、单笔最低${parameters.minimum_commission}元，各${(parameters.slippage_rate * 100).toFixed(2)}%滑点。`
    : '具体交易成本以回放数据的核算约定为准。';
  const sourceWarnings = Array.isArray(asset.quality?.warnings) ? asset.quality.warnings.filter(value => typeof value === 'string').join(' ') : '';
  $('#research-note').textContent = `${costs}${research.methodology.cash_dividends || ''}。最大回撤指24个月持有期间从高点跌下来的幅度，负数表示下跌。${sourceWarnings}有效窗口不代表每一天都已获双源或权威确认。${research.limitations.filter(value => typeof value === 'string').join(' ')}`;
  renderSelectedRisk();
  renderComparisons(asset);
}

async function loadResearch() {
  if (!data || research || researchLoading) return;
  researchLoading = true;
  $('#research-status').hidden = false;
  $('#research-status').classList.remove('error');
  $('#research-status').textContent = '正在加载实际产品回放；尚未确认计算完成。';
  $('#research-results').hidden = true;
  try {
    const response = await fetch('entry-research.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('未取得回放结果');
    research = validateResearch(await response.json());
    $('#research-asset').replaceChildren(...Object.entries(research.assets).map(([code, asset]) => {
      const option = document.createElement('option');
      option.value = code;
      option.textContent = `${asset.type} · ${code} · ${asset.name}`;
      return option;
    }));
    if (research.assets['510300']) $('#research-asset').value = '510300';
    renderResearch();
    $('#research-status').hidden = true;
    $('#research-results').hidden = false;
  } catch (error) {
    research = undefined;
    $('#research-status').classList.add('error');
    $('#research-status').textContent = error.message === '回放与当前曲线版本不同，等待重新计算'
      ? '回放与当前曲线版本不同，等待重新计算，暂不展示收益。'
      : '实际产品回放尚未完成或加载失败，暂不展示收益。可收起后重新展开重试；上方指数曲线仍可查看。';
    console.error('实际产品回放加载失败：', error);
  } finally { researchLoading = false; }
}

$('#real-signal').addEventListener('change', () => choosePoint(Number($('#real-signal').value), false));
$('#real-range').addEventListener('click', () => { zoomed = !zoomed; drawReal(); });
$('#retry').addEventListener('click', loadData);
$('#entry-research').addEventListener('toggle', () => { if ($('#entry-research').open) loadResearch(); });
$('#research-asset').addEventListener('change', renderResearch);
window.addEventListener('resize', drawReal);
loadData();
