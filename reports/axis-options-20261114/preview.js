'use strict';

const snapshot = JSON.parse(document.querySelector('#snapshot').textContent);
const mode = document.body.dataset.axis;
const percent = mode === 'percent';
const chart = snapshot.chart;
const points = snapshot.crossings.filter(point => point.direction === 'down');
const select = document.querySelector('#point-select');
const svg = document.querySelector('#chart');
let selected = points.length - 1;
let zoomed = false;

function node(tag, attributes, text) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}

function axisValue(value) {
  return percent ? value / 10 - 100 : value;
}

function axisText(value) {
  const shown = axisValue(value);
  return percent ? (shown > 0 ? '+' : '') + shown.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) + '%' : value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

function choose(index, focusChart) {
  selected = index;
  select.value = String(index);
  if (focusChart) zoomed = true;
  draw();
  if (focusChart) document.querySelector('#chart-panel').scrollIntoView({ block: 'start', behavior: 'auto' });
}

function draw() {
  const chosen = points[selected];
  const narrow = innerWidth < 680;
  const width = narrow ? 420 : 1100;
  const left = narrow ? 72 : 82;
  const right = width - 32;
  const first = zoomed ? Math.max(0, chosen.index - 20) : 0;
  const last = zoomed ? Math.min(chart.dates.length - 1, chosen.index + 60) : chart.dates.length - 1;
  const x = index => left + (index - first) / Math.max(1, last - first) * (right - left);
  const visible = points.map((point, index) => ({ ...point, number: index + 1 })).filter(point => first <= point.index && point.index <= last);
  // 密集点按横向碰撞分层；编号和日期索引一一对应，不删除任何原始事件。
  const ends = [];
  for (const point of visible) {
    let lane = ends.findIndex(end => x(point.index) - end >= 25);
    if (lane === -1) lane = ends.length;
    ends[lane] = x(point.index);
    point.lane = lane;
  }
  const plotTop = 80 + Math.max(1, ends.length) * 23;
  const plotBottom = plotTop + 310;
  const height = plotBottom + 46;
  const values = chart.stock.slice(first, last + 1).concat(chart.bond.slice(first, last + 1).filter(value => value !== null));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const step = high - low > 3000 ? 1000 : high - low > 1000 ? 500 : 100;
  const lower = Math.floor(low / (step / 2)) * (step / 2);
  const upper = Math.ceil(high / step) * step + (high % step === 0 ? step : 0);
  const y = value => plotBottom - (value - lower) / (upper - lower) * (plotBottom - plotTop);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.replaceChildren();
  svg.append(node('title', { id: 'chart-title' }, `同一份公开指数曲线，以${percent ? '累计收益百分比' : '共同起点1000的累计表现指数'}显示`));
  svg.append(node('desc', { id: 'chart-description' }, '编号对应下方全部历史下穿日期。债基缺失日期断线。历史下穿待核验，不是ETF买入收益。'));
  svg.append(node('text', { x: left, y: 23, class: 'axis-heading' }, percent ? '累计收益（%）' : narrow ? '累计指数（起点1000）' : '累计表现指数（起点＝1000）'));
  svg.append(node('text', { x: left, y: 48, class: 'chart-label' }, narrow ? '编号 → 日期索引' : '下穿点编号 · 完整日期见下方索引'));
  svg.append(node('text', { x: right, y: narrow ? 48 : 23, 'text-anchor': 'end', class: 'selected-label' }, `#${selected + 1} · ${chosen.date}`));
  const ticks = new Set([lower, upper]);
  for (let value = Math.ceil(lower / step) * step; value < upper; value += step) ticks.add(value);
  if (lower <= 1000 && 1000 <= upper) ticks.add(1000);
  for (const value of [...ticks].sort((a, b) => a - b)) {
    svg.append(node('line', { x1: left, x2: right, y1: y(value), y2: y(value), stroke: value === 1000 ? '#a6b8a7' : '#e1e7dd', 'stroke-dasharray': value === 1000 ? '4 4' : '' }));
    svg.append(node('text', { x: left - 12, y: y(value) + 4, 'text-anchor': 'end', class: 'chart-label' }, axisText(value)));
  }
  for (const [series, color] of [[chart.stock, '#245b45'], [chart.bond, '#94714f']]) {
    let path = '';
    let connected = false;
    for (let index = first; index <= last; index++) {
      if (series[index] === null) { connected = false; continue; }
      path += `${connected ? 'L' : 'M'}${x(index).toFixed(2)},${y(series[index]).toFixed(2)} `;
      connected = true;
    }
    svg.append(node('path', { d: path, stroke: color, 'stroke-width': 1.9, fill: 'none', 'stroke-linejoin': 'round', 'data-series': series === chart.stock ? 'stock' : 'bond' }));
  }
  for (const point of visible) {
    const labelY = 69 + point.lane * 23;
    const active = point.number === selected + 1;
    svg.append(node('line', { x1: x(point.index), x2: x(point.index), y1: labelY + 10, y2: y(chart.stock[point.index]), stroke: active ? '#a84836' : '#b7bfb4', 'stroke-width': active ? 1 : .55, 'stroke-dasharray': '3 4' }));
    svg.append(node('circle', { cx: x(point.index), cy: y(chart.stock[point.index]), r: active ? 5.5 : 3.2, fill: active ? '#fff' : '#a84836', stroke: '#a84836', 'stroke-width': 1.8, 'data-point': point.number }));
    svg.append(node('circle', { cx: x(point.index), cy: labelY, r: 9.5, fill: active ? '#a84836' : '#fff', stroke: '#a84836', 'stroke-width': 1 }));
    svg.append(node('text', { x: x(point.index), y: labelY + 3.7, 'text-anchor': 'middle', class: active ? 'badge-number active' : 'badge-number', 'data-number': point.number }, point.number));
  }
  const count = narrow ? 3 : 6;
  for (let tick = 0; tick < count; tick++) {
    const index = Math.round(first + (last - first) * tick / (count - 1));
    svg.append(node('text', { x: x(index), y: plotBottom + 28, 'text-anchor': 'middle', class: 'chart-label' }, chart.dates[index].slice(0, 7)));
  }
  document.querySelector('#period').textContent = `${chart.dates[first]}—${chart.dates[last]}｜选中 #${selected + 1}：${chosen.date}｜${visible.length} 个下穿点在当前范围内`;
  document.querySelector('#zoom').textContent = zoomed ? '返回全历史' : '放大选中点';
  document.querySelector('#zoom').setAttribute('aria-pressed', String(zoomed));
  document.querySelectorAll('.date-button').forEach((button, index) => button.setAttribute('aria-pressed', String(index === selected)));
  document.querySelector('#selected-note').textContent = `当前选中 #${selected + 1} · ${chosen.date}。日期属于公开曲线复算，仍待核验。`;
}

document.querySelector('#as-of').textContent = snapshot.as_of;
document.querySelector('#point-count').textContent = `${points.length} 个原始下穿 · 全部日期常显`;
document.querySelector('#preview-basis').textContent = percent ? '0%＝共同起点；+100%＝翻倍；+400%＝原来的5倍。' : '1000＝共同起点；2000＝累计上涨100%；5000＝累计上涨400%。';
document.querySelector('#quality-detail').textContent = snapshot.quality.warnings.join(' ');
const dates = document.querySelector('#dates');
let year;
let group;
points.forEach((point, index) => {
  const option = document.createElement('option');
  option.value = String(index);
  option.textContent = `#${index + 1} · ${point.date}`;
  select.append(option);
  if (year !== point.date.slice(0, 4)) {
    year = point.date.slice(0, 4);
    const section = document.createElement('section');
    section.className = 'date-year';
    const heading = document.createElement('h3');
    heading.textContent = year + ' 年';
    group = document.createElement('div');
    group.className = 'date-grid';
    section.append(heading, group);
    dates.append(section);
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'date-button';
  button.setAttribute('aria-label', `查看第${index + 1}个下穿点，${point.date}，并放大`);
  const number = document.createElement('span');
  number.className = 'date-number';
  number.textContent = String(index + 1).padStart(2, '0');
  const label = document.createElement('span');
  label.textContent = point.date;
  button.append(number, label);
  button.addEventListener('click', () => choose(index, true));
  group.append(button);
});
select.value = String(selected);
select.addEventListener('change', () => choose(Number(select.value), false));
document.querySelector('#zoom').addEventListener('click', () => { zoomed = !zoomed; draw(); });
addEventListener('resize', draw);
console.assert(points.length === snapshot.crossings.filter(point => point.direction === 'down').length, '下穿事件不能丢失');
console.assert(document.querySelectorAll('.date-button').length === points.length, '全部日期必须常显');
console.assert(axisValue(2000) === (percent ? 100 : 2000), '坐标换算不能把翻倍误作涨200%');
draw();
