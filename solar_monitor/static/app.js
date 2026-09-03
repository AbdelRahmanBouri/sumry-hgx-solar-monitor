const metricNodes = document.querySelectorAll('[data-metric]');
const connectionPill = document.querySelector('#connection-pill');
const connectionLabel = document.querySelector('#connection');
const updatedLabel = document.querySelector('#updated');
const alertBanner = document.querySelector('#alert-banner');
const alertTitle = document.querySelector('#alert-title');
const alertCopy = document.querySelector('#alert-copy');
const faultValue = document.querySelector('#fault-value');
const batteryDirection = document.querySelector('#battery-direction');
const batteryFill = document.querySelector('#battery-fill');
const batteryPath = document.querySelector('#battery-path');
const loadBar = document.querySelector('#load-bar');
const loadPercent = document.querySelector('#load-percent');
const rawResponse = document.querySelector('#raw-response');
const chartCanvas = document.querySelector('#power-chart');
const chartWrap = chartCanvas.parentElement;
const allData = document.querySelector('#all-data');
const allDataBody = document.querySelector('#all-data-body');
const customizeDialog = document.querySelector('#customize-dialog');
const cardControls = document.querySelector('#card-controls');

let historyRows = [];
let selectedHours = 24;
let renderedChart = null;
let latestPayload = null;
let latestStatusSample = {};

const cardNames = {
  load: 'Load',
  output: 'AC output',
  temperature: 'Power stage',
  cooling: 'Cooling',
};
const defaultCardPreferences = {
  order: ['load', 'output', 'temperature', 'cooling'],
  hidden: [],
};

function loadCardPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem('solar-dashboard-cards-v2'));
    const known = defaultCardPreferences.order;
    const order = Array.isArray(saved?.order)
      ? [...saved.order.filter(id => known.includes(id)), ...known.filter(id => !saved.order.includes(id))]
      : [...known];
    const hidden = Array.isArray(saved?.hidden) ? saved.hidden.filter(id => known.includes(id)) : [];
    return {order, hidden};
  } catch (_) {
    return {order: [...defaultCardPreferences.order], hidden: []};
  }
}

let cardPreferences = loadCardPreferences();

function loadDashboardSettings() {
  const defaults = {capacityAh: '', emptyVoltage: 21, fullVoltage: 28.8, presentation: false};
  try {
    return {...defaults, ...JSON.parse(localStorage.getItem('solar-dashboard-settings-v2') || '{}')};
  } catch (_) {
    return defaults;
  }
}

let dashboardSettings = loadDashboardSettings();

function saveDashboardSettings() {
  localStorage.setItem('solar-dashboard-settings-v2', JSON.stringify(dashboardSettings));
}

function saveCardPreferences() {
  localStorage.setItem('solar-dashboard-cards-v2', JSON.stringify(cardPreferences));
}

function applyCardPreferences() {
  const grid = document.querySelector('#summary-grid');
  cardPreferences.order.forEach(id => {
    const card = grid.querySelector(`[data-card-id="${id}"]`);
    if (!card) return;
    card.hidden = cardPreferences.hidden.includes(id);
    grid.appendChild(card);
  });
}

function renderCardControls() {
  cardControls.replaceChildren();
  cardPreferences.order.forEach((id, index) => {
    const row = document.createElement('div');
    row.className = 'card-control';
    const name = document.createElement('span');
    name.textContent = cardNames[id];

    const up = document.createElement('button');
    up.type = 'button';
    up.setAttribute('aria-label', `Move ${cardNames[id]} up`);
    up.innerHTML = '<svg viewBox="0 0 24 24"><path d="m6 15 6-6 6 6"/></svg>';
    up.disabled = index === 0;
    up.addEventListener('click', () => moveCard(index, -1));

    const down = document.createElement('button');
    down.type = 'button';
    down.setAttribute('aria-label', `Move ${cardNames[id]} down`);
    down.innerHTML = '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    down.disabled = index === cardPreferences.order.length - 1;
    down.addEventListener('click', () => moveCard(index, 1));

    const toggle = document.createElement('label');
    toggle.className = 'visibility-toggle';
    toggle.setAttribute('aria-label', `Show ${cardNames[id]}`);
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = !cardPreferences.hidden.includes(id);
    checkbox.addEventListener('change', () => {
      cardPreferences.hidden = checkbox.checked
        ? cardPreferences.hidden.filter(hiddenId => hiddenId !== id)
        : [...cardPreferences.hidden, id];
      saveCardPreferences();
      applyCardPreferences();
    });
    toggle.append(checkbox, document.createElement('i'));
    row.append(name, up, down, toggle);
    cardControls.appendChild(row);
  });
}

function moveCard(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= cardPreferences.order.length) return;
  [cardPreferences.order[index], cardPreferences.order[target]] =
    [cardPreferences.order[target], cardPreferences.order[index]];
  saveCardPreferences();
  applyCardPreferences();
  renderCardControls();
}

function format(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value !== 'number') return value;
  return value.toFixed(digits).replace(/\.0$/, '');
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function relativeTime(value) {
  if (!value) return 'Waiting for update';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Update time unavailable';
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 8) return 'Updated now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

function activeFault(value) {
  if (value === null || value === undefined || value === '') return false;
  return !['none', 'no faults', 'no active faults', 'normal', '0', '—']
    .includes(String(value).trim().toLowerCase());
}

function setConnection(state, text) {
  connectionPill.classList.toggle('online', state === 'online');
  connectionPill.classList.toggle('error', state === 'error');
  connectionLabel.textContent = text;
}

function updateAlert(sample, diagnostic) {
  if (diagnostic?.error) {
    alertBanner.hidden = false;
    alertTitle.textContent = 'Inverter connection issue';
    alertCopy.textContent = diagnostic.error;
    sendNotification('Solar monitor connection issue', diagnostic.error);
    return;
  }
  if (activeFault(sample.faults)) {
    alertBanner.hidden = false;
    alertTitle.textContent = 'Inverter fault detected';
    alertCopy.textContent = String(sample.faults);
    sendNotification('Solar inverter fault', String(sample.faults));
    return;
  }
  alertBanner.hidden = true;
}

function sendNotification(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const signature = `${title}:${body}`;
  if (sessionStorage.getItem('solar-last-alert') === signature) return;
  sessionStorage.setItem('solar-last-alert', signature);
  new Notification(title, {body, icon: '/static/solar-app-icon.svg', tag: 'solar-monitor-alert'});
}

function batteryStateOfCharge(voltage) {
  const empty = Number(dashboardSettings.emptyVoltage);
  const full = Number(dashboardSettings.fullVoltage);
  if (!Number.isFinite(voltage) || !Number.isFinite(empty) || !Number.isFinite(full) || full <= empty) return null;
  return Math.max(0, Math.min(100, (voltage - empty) / (full - empty) * 100));
}

function formatRuntime(hours) {
  if (!Number.isFinite(hours) || hours <= 0) return '—';
  if (hours >= 24) return '>24 h';
  const whole = Math.floor(hours);
  const minutes = Math.round((hours - whole) * 60);
  return whole ? `~${whole}h ${minutes}m` : `~${minutes} min`;
}

function updateEnergyInsights(sample) {
  const pv = number(sample.pv_watts);
  const load = number(sample.ac_load_watts);
  const current = number(sample.battery_current);
  const voltage = number(sample.battery_voltage);
  const soc = batteryStateOfCharge(voltage);
  document.querySelector('#battery-soc').textContent = soc === null ? '—%' : `~${Math.round(soc)}%`;

  const capacity = Number(dashboardSettings.capacityAh);
  let runtime = 'Set capacity';
  if (Number.isFinite(capacity) && capacity > 0 && soc !== null && current !== null) {
    if (current < -.05) runtime = formatRuntime(capacity * soc / 100 / Math.abs(current));
    else if (current > .05) runtime = 'Charging';
    else runtime = 'Standby';
  }
  document.querySelector('#battery-runtime').textContent = runtime;

  let balance = 'Calculating flow';
  if (current !== null && current > .05) balance = 'Solar charging battery';
  else if (current !== null && current < -.05 && pv !== null && pv > 50) balance = 'Solar + battery supplying home';
  else if (current !== null && current < -.05) balance = 'Battery supplying home';
  else if (pv !== null && load !== null && pv >= load) balance = 'Solar supplying home';
  else if (pv !== null && load !== null) balance = 'Inverter balancing demand';
  document.querySelector('#energy-balance').textContent = balance;
}

function updateStatusData(sample, diagnostic) {
  latestStatusSample = sample;
  metricNodes.forEach(node => {
    const value = sample[node.dataset.metric];
    node.textContent = format(value);
  });

  document.querySelectorAll('[data-metric="operating_state"]').forEach(node => {
    if (!sample.operating_state) node.textContent = 'Not decoded';
  });
  document.querySelectorAll('[data-metric="faults"]').forEach(node => {
    if (!activeFault(sample.faults)) node.textContent = 'No active faults';
  });
  faultValue.classList.toggle('active', activeFault(sample.faults));

  const current = number(sample.battery_current);
  const direction = current === null ? 'Awaiting data'
    : current > .05 ? 'Charging'
    : current < -.05 ? 'Discharging'
    : 'Idle';
  batteryDirection.textContent = direction;
  batteryDirection.classList.toggle('discharge', direction === 'Discharging');
  batteryPath.classList.toggle('discharging', direction === 'Discharging');
  batteryPath.classList.toggle('idle', direction === 'Idle' || direction === 'Awaiting data');

  const voltage = number(sample.battery_voltage);
  const level = voltage === null ? 45 : Math.max(8, Math.min(100, ((voltage - 21) / 7.8) * 100));
  batteryFill.style.height = `${level}%`;

  const watts = number(sample.ac_load_watts);
  const load = watts === null ? null : Math.max(0, Math.min(100, watts / 4000 * 100));
  loadBar.style.width = load === null ? '0%' : `${load}%`;
  loadPercent.textContent = load === null ? '—%' : `${Math.round(load)}%`;

  const temperatures = [
    sample.inverter_temperature,
    sample.ntc2_temperature,
    sample.ntc3_temperature,
    sample.ntc4_temperature,
    sample.temperature_1,
    sample.temperature_2,
    sample.temperature_3,
  ].map(number).filter(value => value !== null);
  document.querySelector('#power-temperature').textContent =
    temperatures.length ? format(Math.max(...temperatures), 0) : '—';

  const fans = [sample.fan1_percent, sample.fan2_percent].map(number).filter(value => value !== null);
  document.querySelector('#fan-percent').textContent =
    fans.length ? format(Math.max(...fans), 0) : '—';

  updateEnergyInsights(sample);
  rawResponse.textContent = diagnostic?.response_hex || diagnostic?.error || 'No serial response captured';
  updateAlert(sample, diagnostic);
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error('Dashboard status unavailable');
    const data = await response.json();
    latestPayload = data;
    const sample = data.sample || {};
    const diagnostic = data.diagnostic;
    updateStatusData(sample, diagnostic);

    if (diagnostic?.error) setConnection('error', 'Serial issue');
    else if (diagnostic?.response_hex) setConnection('online', 'Live');
    else setConnection('waiting', 'Waiting');

    const capturedAt = sample.captured_at || diagnostic?.captured_at;
    updatedLabel.dataset.time = capturedAt || '';
    updatedLabel.textContent = relativeTime(capturedAt);
    if (allData.open) renderAllData();
  } catch (error) {
    setConnection('error', 'Offline');
    alertBanner.hidden = false;
    alertTitle.textContent = 'Dashboard unavailable';
    alertCopy.textContent = 'The latest inverter reading could not be loaded.';
  }
}

function timestamp(row) {
  return new Date(row.captured_at || row.recorded_at || row.timestamp).getTime();
}

function filteredRows() {
  const valid = historyRows.filter(row => Number.isFinite(timestamp(row)));
  if (!valid.length) return [];
  const newest = Math.max(...valid.map(timestamp));
  const cutoff = newest - selectedHours * 60 * 60 * 1000;
  return valid.filter(row => timestamp(row) >= cutoff);
}

function niceMaximum(maximum) {
  if (maximum <= 500) return 500;
  if (maximum <= 1000) return 1000;
  if (maximum <= 2000) return 2000;
  if (maximum <= 3000) return 3000;
  return Math.ceil(maximum / 1000) * 1000;
}

function todaysRows() {
  const now = new Date();
  return historyRows.filter(row => {
    const date = new Date(timestamp(row));
    return date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
  });
}

function integrateKwh(rows, key) {
  let wattHours = 0;
  for (let index = 1; index < rows.length; index += 1) {
    const hours = Math.min((timestamp(rows[index]) - timestamp(rows[index - 1])) / 3600000, 5 / 60);
    const before = number(rows[index - 1][key]);
    const after = number(rows[index][key]);
    if (hours > 0 && before !== null && after !== null) wattHours += ((before + after) / 2) * hours;
  }
  return wattHours / 1000;
}

function updateDailySummary() {
  const rows = todaysRows();
  const solar = integrateKwh(rows, 'pv_watts');
  const load = integrateKwh(rows, 'ac_load_watts');
  const batteries = rows.map(row => number(row.battery_voltage)).filter(value => value !== null);
  document.querySelector('#solar-energy').textContent = rows.length > 1 ? `${solar.toFixed(2)} kWh` : '— kWh';
  document.querySelector('#load-energy').textContent = rows.length > 1 ? `${load.toFixed(2)} kWh` : '— kWh';
  document.querySelector('#battery-minimum').textContent = batteries.length ? `${Math.min(...batteries).toFixed(1)} V` : '— V';
}

function axisPower(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)}k` : String(Math.round(value));
}

function roundedPath(ctx, points) {
  if (!points.length) return;
  ctx.moveTo(points[0].x, points[0].y);
  if (points.length === 1) return;
  for (let index = 1; index < points.length - 1; index += 1) {
    const midpointX = (points[index].x + points[index + 1].x) / 2;
    const midpointY = (points[index].y + points[index + 1].y) / 2;
    ctx.quadraticCurveTo(points[index].x, points[index].y, midpointX, midpointY);
  }
  ctx.lineTo(points.at(-1).x, points.at(-1).y);
}

function drawChart() {
  const rows = filteredRows();
  const empty = document.querySelector('#chart-empty');
  document.querySelector('#sample-count').textContent = String(rows.length);

  const solarValues = rows.map(row => number(row.pv_watts)).filter(value => value !== null);
  const loadValues = rows.map(row => number(row.ac_load_watts)).filter(value => value !== null);
  const batteryValues = rows.map(row => number(row.battery_voltage)).filter(value => value !== null);
  document.querySelector('#solar-peak').textContent = solarValues.length ? `${Math.round(Math.max(...solarValues))} W` : '— W';
  document.querySelector('#load-peak').textContent = loadValues.length ? `${Math.round(Math.max(...loadValues))} W` : '— W';
  document.querySelector('#trend-battery-min').textContent = batteryValues.length ? `${Math.min(...batteryValues).toFixed(1)} V` : '— V';

  const ctx = chartCanvas.getContext('2d');
  if (rows.length < 2) {
    empty.hidden = false;
    ctx.clearRect(0, 0, chartCanvas.width, chartCanvas.height);
    renderedChart = null;
    return;
  }
  empty.hidden = true;

  const ratio = window.devicePixelRatio || 1;
  const width = chartCanvas.clientWidth;
  const height = chartCanvas.clientHeight;
  chartCanvas.width = Math.round(width * ratio);
  chartCanvas.height = Math.round(height * ratio);
  ctx.scale(ratio, ratio);

  const padding = {left: 34, right: 7, top: 9, bottom: 23};
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const allValues = [...solarValues, ...loadValues];
  const max = niceMaximum(Math.max(...allValues, 100));
  const firstTime = timestamp(rows[0]);
  const lastTime = timestamp(rows.at(-1));
  const duration = Math.max(1, lastTime - firstTime);

  ctx.font = '8px Inter, system-ui';
  ctx.textBaseline = 'middle';
  for (let index = 0; index <= 4; index += 1) {
    const y = padding.top + plotHeight * index / 4;
    ctx.strokeStyle = '#e6ede9';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    ctx.fillStyle = '#899790';
    ctx.textAlign = 'left';
    ctx.fillText(axisPower(max * (1 - index / 4)), 1, y);
  }

  [0, .5, 1].forEach(fraction => {
    const time = new Date(firstTime + duration * fraction);
    ctx.fillStyle = '#899790';
    ctx.textAlign = fraction === 0 ? 'left' : fraction === 1 ? 'right' : 'center';
    ctx.fillText(
      time.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}),
      padding.left + plotWidth * fraction,
      height - 7
    );
  });

  function pointsFor(key) {
    return rows.map(row => ({
      x: padding.left + plotWidth * (timestamp(row) - firstTime) / duration,
      y: padding.top + plotHeight * (1 - Math.max(0, number(row[key]) || 0) / max),
    }));
  }

  function series(key, stroke, fill) {
    const points = pointsFor(key);
    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.lineTo(points.at(-1).x, padding.top + plotHeight);
    ctx.lineTo(points[0].x, padding.top + plotHeight);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotHeight);
    gradient.addColorStop(0, fill);
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 2;
    ctx.stroke();
    return points;
  }

  const solarPoints = series('pv_watts', '#c9780a', 'rgba(201,120,10,.16)');
  const loadPoints = series('ac_load_watts', '#087d55', 'rgba(8,125,85,.1)');
  renderedChart = {rows, solarPoints, loadPoints, padding, plotWidth, plotHeight, width, height};
}

function setupChartTooltip() {
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  chartWrap.appendChild(tooltip);

  function hide() { tooltip.hidden = true; }
  function show(event) {
    if (!renderedChart) return hide();
    const rect = chartCanvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const {rows, solarPoints} = renderedChart;
    let closest = 0;
    let distance = Infinity;
    solarPoints.forEach((point, index) => {
      const nextDistance = Math.abs(point.x - x);
      if (nextDistance < distance) { distance = nextDistance; closest = index; }
    });
    const row = rows[closest];
    const left = Math.max(54, Math.min(rect.width - 54, solarPoints[closest].x));
    tooltip.style.left = `${left}px`;
    tooltip.innerHTML = `<strong>${new Date(timestamp(row)).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}</strong><span><i class="solar-key"></i>${format(number(row.pv_watts), 0)} W</span><span><i class="load-key"></i>${format(number(row.ac_load_watts), 0)} W</span>`;
    tooltip.hidden = false;
  }

  chartCanvas.addEventListener('pointermove', show);
  chartCanvas.addEventListener('pointerdown', show);
  chartCanvas.addEventListener('pointerleave', hide);
}

async function refreshHistory() {
  try {
    const response = await fetch('/api/history', {cache: 'no-store'});
    if (!response.ok) throw new Error('History unavailable');
    historyRows = await response.json();
    updateDailySummary();
    drawChart();
  } catch (_) {
    historyRows = [];
    drawChart();
  }
}

function readableLabel(key) {
  const special = {
    pv: 'PV',
    ac: 'AC',
    va: 'VA',
    ntc: 'NTC',
    bts: 'BTS',
  };
  return key.split('_').map(word => special[word] || word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}

function readableValue(key, value) {
  if (value === null || value === undefined || value === '') return 'Not available';
  if (key.endsWith('_at')) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  const units = {
    pv_watts: 'W', pv_voltage: 'V', pv_current: 'A',
    ac_load_watts: 'W', ac_load_va: 'VA', ac_load_current: 'A',
    battery_voltage: 'V', battery_current: 'A', battery_temperature: '°C',
    bts_temperature: '°C', bus_voltage: 'V', inverter_va: 'VA',
    output_voltage: 'V', output_frequency: 'Hz', output_current: 'A',
    inverter_temperature: '°C', ntc2_temperature: '°C', ntc3_temperature: '°C',
    ntc4_temperature: '°C', temperature_1: '°C', temperature_2: '°C',
    temperature_3: '°C', fan1_percent: '%', fan2_percent: '%',
    interval_seconds: 's', elapsed_ms: 'ms',
  };
  return `${value}${units[key] ? ` ${units[key]}` : ''}`;
}

function appendDataGroup(title, values) {
  if (!values) return;
  const group = document.createElement('section');
  group.className = 'data-group';
  const heading = document.createElement('h3');
  heading.textContent = title;
  group.appendChild(heading);
  Object.entries(values).forEach(([key, value]) => {
    const row = document.createElement('div');
    row.className = 'data-row';
    const label = document.createElement('span');
    const labelText = document.createElement('span');
    labelText.textContent = readableLabel(key);
    label.appendChild(labelText);
    if (title === 'Decoded readings') {
      const experimental = [
        'operating_state', 'faults', 'battery_temperature', 'inverter_temperature',
        'temperature_1', 'temperature_2', 'temperature_3', 'bts_temperature',
      ].includes(key);
      const confidence = document.createElement('em');
      confidence.className = `confidence ${experimental ? 'experimental' : 'confirmed'}`;
      confidence.textContent = experimental ? 'Experimental' : 'Confirmed';
      label.appendChild(confidence);
    }
    const output = document.createElement('strong');
    output.textContent = readableValue(key, value);
    row.append(label, output);
    group.appendChild(row);
  });
  allDataBody.appendChild(group);
}

function renderAllData() {
  allDataBody.replaceChildren();
  if (!latestPayload) {
    const message = document.createElement('p');
    message.textContent = 'Waiting for the latest inverter response.';
    allDataBody.appendChild(message);
    return;
  }
  appendDataGroup('Decoded readings', latestPayload.sample);
  appendDataGroup('Collector', latestPayload.collector);
  appendDataGroup('Last serial exchange', latestPayload.diagnostic);
}

document.querySelector('#customize-open').addEventListener('click', () => {
  renderCardControls();
  customizeDialog.showModal();
});

document.querySelector('#reset-cards').addEventListener('click', () => {
  cardPreferences = {order: [...defaultCardPreferences.order], hidden: []};
  saveCardPreferences();
  applyCardPreferences();
  renderCardControls();
});

allData.addEventListener('toggle', () => {
  if (allData.open) renderAllData();
});

function initializeDashboardSettings() {
  const capacityInput = document.querySelector('#battery-capacity');
  const emptyInput = document.querySelector('#battery-empty-voltage');
  const fullInput = document.querySelector('#battery-full-voltage');
  const presentationInput = document.querySelector('#presentation-mode');
  const notificationButton = document.querySelector('#notification-button');
  const notificationStatus = document.querySelector('#notification-status');

  capacityInput.value = dashboardSettings.capacityAh;
  emptyInput.value = dashboardSettings.emptyVoltage;
  fullInput.value = dashboardSettings.fullVoltage;
  presentationInput.checked = Boolean(dashboardSettings.presentation);
  document.body.classList.toggle('presentation-mode', presentationInput.checked);

  function saveBatterySettings() {
    dashboardSettings.capacityAh = capacityInput.value;
    dashboardSettings.emptyVoltage = Number(emptyInput.value) || 21;
    dashboardSettings.fullVoltage = Number(fullInput.value) || 28.8;
    saveDashboardSettings();
    updateEnergyInsights(latestStatusSample);
  }
  [capacityInput, emptyInput, fullInput].forEach(input => input.addEventListener('change', saveBatterySettings));
  presentationInput.addEventListener('change', () => {
    dashboardSettings.presentation = presentationInput.checked;
    document.body.classList.toggle('presentation-mode', presentationInput.checked);
    saveDashboardSettings();
  });

  if (!('Notification' in window)) {
    notificationButton.disabled = true;
    notificationButton.textContent = 'Unavailable';
    notificationStatus.textContent = 'Browser notifications require supported secure access.';
  } else if (Notification.permission === 'granted') {
    notificationButton.textContent = 'Enabled';
    notificationButton.disabled = true;
    notificationStatus.textContent = 'Critical faults and connection issues are enabled.';
  } else if (Notification.permission === 'denied') {
    notificationButton.textContent = 'Blocked';
    notificationButton.disabled = true;
    notificationStatus.textContent = 'Notifications are blocked in this browser’s settings.';
  }

  notificationButton.addEventListener('click', async () => {
    const permission = await Notification.requestPermission();
    notificationButton.textContent = permission === 'granted' ? 'Enabled' : 'Blocked';
    notificationButton.disabled = true;
    notificationStatus.textContent = permission === 'granted'
      ? 'Critical faults and connection issues are enabled.'
      : 'Notifications were not enabled on this device.';
  });
}

document.querySelectorAll('.range-control button').forEach(button => {
  button.addEventListener('click', () => {
    selectedHours = Number(button.dataset.hours);
    document.querySelectorAll('.range-control button').forEach(item => item.classList.toggle('active', item === button));
    drawChart();
  });
});

document.querySelectorAll('.bottom-nav a').forEach(link => {
  link.addEventListener('click', () => {
    document.querySelectorAll('.bottom-nav a').forEach(item => item.classList.toggle('active', item === link));
  });
});

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawChart, 120);
});

setupChartTooltip();
applyCardPreferences();
initializeDashboardSettings();
refreshStatus();
refreshHistory();
setInterval(refreshStatus, 5000);
setInterval(() => {
  if (updatedLabel.dataset.time) updatedLabel.textContent = relativeTime(updatedLabel.dataset.time);
}, 15000);
setInterval(refreshHistory, 60000);
