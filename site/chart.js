'use strict';

const $ = selector => document.querySelector(selector);
let data;
let zoomed = false;

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

function drawReal() {
  if (!data) return;
  const { chart } = data;
  const chosen = data.signals[Number($('#real-signal').value)];
  const narrow = window.innerWidth < 680;
  const width = narrow ? 400 : 1000;
  const right = width - 34;
  let first = 0;
  let last = chart.dates.length - 1;
  if (zoomed && chosen) {
    first = Math.max(0, chosen.index - data.parameters.cooldown_sessions);
    last = Math.min(last, chosen.index + data.parameters.cooldown_sessions * 4);
  }
  let minimum = Infinity;
  let maximum = -Infinity;
  for (let index = first; index <= last; index++) {
    for (const value of [chart.stock[index], chart.bond[index]]) {
      if (value === null) continue;
      minimum = Math.min(minimum, value);
      maximum = Math.max(maximum, value);
    }
  }
  minimum = Math.floor(minimum * .94 / 100) * 100;
  maximum = Math.ceil(maximum * 1.05 / 100) * 100;
  const x = index => 65 + (index - first) * (right - 65) / Math.max(1, last - first);
  const y = value => 300 - (value - minimum) / (maximum - minimum) * 265;
  $('#real-chart').setAttribute('viewBox', `0 0 ${width} 350`);
  const grid = [];
  for (let tick = 0; tick < 5; tick++) {
    const value = minimum + (maximum - minimum) * tick / 4;
    grid.push(svgElement('line', { x1: 65, x2: right, y1: y(value), y2: y(value), stroke: '#e3e8df' }));
    grid.push(svgElement('text', { class: 'chart-label', x: 52, y: y(value) + 4, 'text-anchor': 'end' }, Math.round(value).toLocaleString('en-US')));
  }
  const ticks = narrow ? 3 : 6;
  for (let tick = 0; tick < ticks; tick++) {
    const index = Math.round(first + (last - first) * tick / (ticks - 1));
    grid.push(svgElement('text', { class: 'chart-label', x: x(index), y: 327, 'text-anchor': 'middle' }, chart.dates[index].slice(0, 7)));
  }
  $('#real-grid').replaceChildren(...grid);
  for (const [selector, series] of [['#real-stock', chart.stock], ['#real-bond', chart.bond]]) {
    let path = '';
    let connected = false;
    for (let index = first; index <= last; index++) {
      // 缺失日期必须断线，不能把空值当成零或跨过空档连线。
      if (series[index] === null) { connected = false; continue; }
      path += `${connected ? 'L' : 'M'}${x(index).toFixed(2)},${y(series[index]).toFixed(2)} `;
      connected = true;
    }
    $(selector).setAttribute('d', path);
  }
  $('#real-points').replaceChildren(...data.crossings
    .filter(point => point.direction === 'down' && point.index >= first && point.index <= last)
    .map(point => svgElement('circle', { cx: x(point.index), cy: y(chart.stock[point.index]), r: 3, fill: '#a84836' })));
  const selection = [];
  if (chosen) {
    selection.push(svgElement('line', { x1: x(chosen.index), x2: x(chosen.index), y1: 28, y2: 300, stroke: '#a84836', 'stroke-dasharray': '4 4' }));
    selection.push(svgElement('circle', { cx: x(chosen.index), cy: y(chart.stock[chosen.index]), r: 6, fill: 'white', stroke: '#a84836', 'stroke-width': 2 }));
    const anchor = x(chosen.index) > width / 2 ? 'end' : 'start';
    selection.push(svgElement('text', { class: 'selection-label', x: x(chosen.index) + (anchor === 'end' ? -8 : 8), y: 22, 'text-anchor': anchor }, chosen.date));
  }
  $('#real-selection').replaceChildren(...selection);
  $('#real-range').textContent = zoomed ? '查看全历史' : '放大信号前后';
  $('#real-range').setAttribute('aria-pressed', String(zoomed));
  const base = data.parameters.base_date.replaceAll('-', '.');
  $('#real-chart-period').textContent = `显示 ${chart.dates[first]} — ${chart.dates[last]}${chosen ? '；选中 ' + chosen.date : '；当前没有保留下穿点'}。两线按 ${base}＝1000 换算；债基缺失日期断线，不填补。保留点间隔至少 ${data.parameters.cooldown_sessions} 个交易日。`;
  $('#real-chart-desc').textContent = `显示 ${chart.dates[first]} 至 ${chart.dates[last]}，股票为绿线，债基为棕线，红点表示原始下穿${chosen ? '，虚线标记选中的 ' + chosen.date : ''}。债基缺失日期断线，不填补。`;
}

async function loadData() {
  data = undefined;
  zoomed = false;
  $('#real-frame').hidden = true;
  $('#retry').hidden = true;
  $('#load-status').hidden = false;
  $('#load-status').classList.remove('error');
  $('#load-status').textContent = '正在加载历史曲线…';
  $('#data-as-of').textContent = '加载中';
  $('#data-as-of').removeAttribute('datetime');
  $('#quality-warnings').replaceChildren();
  try {
    const response = await fetch('stock-bond.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('数据请求失败');
    data = validate(await response.json());
    $('#data-as-of').textContent = data.as_of;
    $('#data-as-of').dateTime = data.as_of;
    $('#quality-warnings').replaceChildren(...data.quality.warnings.map(text => {
      const paragraph = document.createElement('p');
      paragraph.textContent = text;
      return paragraph;
    }));
    $('#real-signal').replaceChildren(...data.signals.map((point, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = `${point.date} · 股票下穿债基线`;
      return option;
    }));
    $('#real-signal').value = String(Math.max(0, data.signals.length - 1));
    $('#real-signal').disabled = !data.signals.length;
    $('#real-range').disabled = !data.signals.length;
    $('#real-frame').hidden = false;
    drawReal();
    $('#load-status').hidden = true;
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

$('#real-signal').addEventListener('change', drawReal);
$('#real-range').addEventListener('click', () => { zoomed = !zoomed; drawReal(); });
$('#retry').addEventListener('click', loadData);
window.addEventListener('resize', drawReal);
loadData();
