'use strict';

const $ = selector => document.querySelector(selector);
let data;
let zoomed = false;
let downPoints = [];

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
  $('#data-checked-at').textContent = dates?.checked_at && Number.isFinite(Date.parse(dates.checked_at))
    ? new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(dates.checked_at))
    : '未提供';
  if (dates?.checked_at) $('#data-checked-at').dateTime = dates.checked_at;
  $('#source-dates').textContent = dates
    ? `来源数据：股票截至 ${dates.stock_as_of || '未提供'}；债基截至 ${dates.bond_as_of || '未提供'}。最近成功检查：${acquired}。行情未增加时，截至日期不变；检查成功不等于债基日期已核验。`
    : '两侧来源日期与成功检查时间尚未提供；共同截止日不代表债基日期口径已核验。';
  $('#axis-explanation').textContent = `自 ${data.parameters.base_date} 起的累计收益。0%＝共同起点；+100%＝翻倍；+400%＝原来的5倍。`;
}

async function loadData() {
  data = undefined;
  downPoints = [];
  zoomed = false;
  $('#real-frame').hidden = true;
  $('#retry').hidden = true;
  $('#load-status').hidden = false;
  $('#load-status').classList.remove('error');
  $('#load-status').textContent = '正在加载历史曲线…';
  $('#data-as-of').textContent = '加载中';
  $('#data-as-of').removeAttribute('datetime');
  $('#data-checked-at').textContent = '加载中';
  $('#data-checked-at').removeAttribute('datetime');
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
  } catch (error) {
    data = undefined;
    $('#real-frame').hidden = true;
    $('#data-as-of').textContent = '未取得';
    $('#data-as-of').removeAttribute('datetime');
    $('#data-checked-at').textContent = '未取得';
    $('#data-checked-at').removeAttribute('datetime');
    $('#load-status').classList.add('error');
    $('#load-status').textContent = '历史曲线加载失败，暂时无法显示有效数据。请检查网络后重新加载。';
    $('#retry').hidden = false;
    console.error('股债曲线加载失败：', error);
  }
}

$('#real-signal').addEventListener('change', () => choosePoint(Number($('#real-signal').value), false));
$('#real-range').addEventListener('click', () => { zoomed = !zoomed; drawReal(); });
$('#retry').addEventListener('click', loadData);
window.addEventListener('resize', drawReal);
loadData();
