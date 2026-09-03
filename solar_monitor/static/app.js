const metricNodes = document.querySelectorAll('[data-metric]');
const connectionPill = document.querySelector('#connection-pill');
const connectionLabel = document.querySelector('#connection');
const updatedLabel = document.querySelector('#updated');
const pullNowButton = document.querySelector('#pull-now');
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
const overlayCanvas = document.querySelector('#power-chart-overlay');
const chartWrap = chartCanvas.parentElement;
const chartHint = document.querySelector('#chart-hint');
const zoomResetButton = document.querySelector('#zoom-reset');
const allData = document.querySelector('#all-data');
const allDataBody = document.querySelector('#all-data-body');
const faultHistoryDetails = document.querySelector('#fault-history');
const customizeDialog = document.querySelector('#customize-dialog');
const cardControls = document.querySelector('#card-controls');

let historyRows = [];
let todayRows = [];
let selectedHours = 24;
let viewWindow = null;
let yViewWindow = null; // {min, max} in watts, or null for auto-scale
const Y_AXIS_ZONE_WIDTH = 44; // px from the left edge treated as the "drag the scale" zone
let renderedChart = null;
let latestPayload = null;
let latestStatusSample = {};

const cardNames = {
  load: 'Load',
  output: 'AC output',
  temperature: 'Power stage',
  cooling: 'Cooling',
  grid: 'Grid & bus',
};
const defaultCardPreferences = {
  order: ['load', 'output', 'temperature', 'cooling', 'grid'],
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

function loadSeriesVisibility() {
  const defaults = {pv: true, load: true, battery: false};
  try {
    const saved = JSON.parse(localStorage.getItem('solar-dashboard-series-v1'));
    return {
      pv: saved?.pv !== false, load: saved?.load !== false, battery: Boolean(saved?.battery),
    };
  } catch (_) {
    return defaults;
  }
}

let seriesVisible = loadSeriesVisibility();

function saveDashboardSettings() {
  localStorage.setItem('solar-dashboard-settings-v2', JSON.stringify(dashboardSettings));
}

function saveCardPreferences() {
  localStorage.setItem('solar-dashboard-cards-v2', JSON.stringify(cardPreferences));
}

function saveSeriesVisibility() {
  localStorage.setItem('solar-dashboard-series-v1', JSON.stringify(seriesVisible));
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

function formatUptime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
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

const CONFIG_LABELS = {
  output_source_priority: 'Output priority', maximum_charge_current_a: 'Max charge current',
  ac_input_range: 'AC input range', power_saving: 'Power saving', battery_type: 'Battery type',
  overload_auto_restart: 'Overload auto-restart', overtemperature_auto_restart: 'Overtemp auto-restart',
  output_voltage_v: 'Output voltage', output_frequency_mode: 'Frequency mode',
  output_frequency_hz: 'Output frequency', grid_export_enabled: 'Grid export',
  utility_charge_current_a: 'Utility charge current', charge_cutoff_current_a: 'Charge cutoff current',
  back_to_utility_voltage_v: 'Back-to-utility voltage', parallel_operation: 'Parallel operation',
  phase: 'Phase', charge_source_priority: 'Charge priority', buzzer: 'Buzzer',
  warning_buzzer: 'Warning buzzer', auto_return_display: 'Auto-return display', backlight: 'Backlight',
  power_interruption_buzzer: 'Power-interruption buzzer', fault_recording: 'Fault recording',
  low_battery_cutoff_v: 'Low-battery cutoff', bulk_charge_voltage_v: 'Bulk charge voltage',
  float_charge_voltage_v: 'Float charge voltage', equalization_enabled: 'Equalization',
  equalization_voltage_v: 'Equalization voltage', equalization_time_min: 'Equalization time',
  equalization_timeout_min: 'Equalization timeout', equalization_interval_days: 'Equalization interval',
  inverter_power_limit_w: 'Power limit',
};
const CONFIG_UNITS = {
  maximum_charge_current_a: 'A', output_voltage_v: 'V', output_frequency_hz: 'Hz',
  utility_charge_current_a: 'A', charge_cutoff_current_a: 'A', back_to_utility_voltage_v: 'V',
  low_battery_cutoff_v: 'V', bulk_charge_voltage_v: 'V', float_charge_voltage_v: 'V',
  equalization_voltage_v: 'V', equalization_time_min: 'min', equalization_timeout_min: 'min',
  equalization_interval_days: 'days', inverter_power_limit_w: 'W',
};

function renderConfig() {
  const grid = document.querySelector('#config-grid');
  const note = document.querySelector('#config-note');
  const config = latestPayload?.inverter_config;
  if (!config || !config.settings) {
    grid.hidden = true;
    note.hidden = false;
    note.textContent = "Waiting for the inverter's configuration snapshot (captured every few minutes).";
    return;
  }
  note.hidden = false;
  note.textContent = `Captured ${relativeTime(config.captured_at)} · read-only, never written back to the inverter`;
  grid.hidden = false;
  grid.replaceChildren();
  Object.entries(config.settings).forEach(([key, value]) => {
    const cell = document.createElement('div');
    const label = document.createElement('span');
    label.textContent = CONFIG_LABELS[key] || readableLabel(key);
    const output = document.createElement('strong');
    output.textContent = typeof value === 'boolean'
      ? (value ? 'On' : 'Off')
      : `${value}${CONFIG_UNITS[key] ? ` ${CONFIG_UNITS[key]}` : ''}`;
    cell.append(label, output);
    grid.appendChild(cell);
  });
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
  renderConfig();
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

function disablePullFor(seconds) {
  pullNowButton.disabled = true;
  setTimeout(() => { pullNowButton.disabled = false; }, Math.max(500, seconds * 1000));
}

async function pullNow() {
  if (pullNowButton.disabled) return;
  pullNowButton.classList.add('spinning');
  try {
    const response = await fetch('/api/refresh', {method: 'POST', cache: 'no-store'});
    const data = await response.json();
    if (response.status === 429) {
      disablePullFor(data.retry_after_seconds || 5);
      return;
    }
    if (!response.ok) {
      disablePullFor(5);
      return;
    }
    latestPayload = data;
    const sample = data.sample || {};
    updateStatusData(sample, data.diagnostic);
    if (data.diagnostic?.error) setConnection('error', 'Serial issue');
    else if (data.diagnostic?.response_hex) setConnection('online', 'Live');
    const capturedAt = sample.captured_at || data.diagnostic?.captured_at;
    updatedLabel.dataset.time = capturedAt || '';
    updatedLabel.textContent = relativeTime(capturedAt);
    if (allData.open) renderAllData();
    await Promise.all([refreshHistory(), refreshToday()]);
    disablePullFor(5);
  } catch (_) {
    disablePullFor(3);
  } finally {
    pullNowButton.classList.remove('spinning');
  }
}

pullNowButton.addEventListener('click', pullNow);

function timestamp(row) {
  return new Date(row.captured_at || row.recorded_at || row.timestamp).getTime();
}

function fullRangeWindow() {
  const valid = historyRows.filter(row => Number.isFinite(timestamp(row)));
  if (!valid.length) return null;
  const newest = Math.max(...valid.map(timestamp));
  const oldest = selectedHours > 24 ? Math.min(...valid.map(timestamp)) : newest - selectedHours * 3600000;
  return {start: oldest, end: newest};
}

function clampWindow(win) {
  const full = fullRangeWindow();
  if (!full) return win;
  const maxSpan = Math.max(full.end - full.start, 60000);
  const span = Math.min(Math.max(win.end - win.start, 5 * 60000), maxSpan);
  let start = Math.min(Math.max(win.start, full.start), full.end - span);
  return {start, end: start + span};
}

function updateZoomResetVisibility() {
  zoomResetButton.hidden = !viewWindow && !yViewWindow;
}

function clampYWindow(win) {
  const minSpan = 20;
  const ceiling = 20000;
  const span = Math.max(win.max - win.min, minSpan);
  let min = Math.max(0, Math.min(win.min, ceiling - span));
  return {min, max: min + span};
}

function filteredRows() {
  const valid = historyRows.filter(row => Number.isFinite(timestamp(row)));
  if (!valid.length) return [];
  if (viewWindow) {
    return valid.filter(row => timestamp(row) >= viewWindow.start && timestamp(row) <= viewWindow.end);
  }
  const newest = Math.max(...valid.map(timestamp));
  const cutoff = newest - selectedHours * 60 * 60 * 1000;
  return valid.filter(row => timestamp(row) >= cutoff);
}

function downsample(rows, maxPoints = 600) {
  if (rows.length <= maxPoints) return rows;
  const stride = Math.ceil(rows.length / maxPoints);
  const sampled = rows.filter((_, index) => index % stride === 0);
  if (sampled.at(-1) !== rows.at(-1)) sampled.push(rows.at(-1));
  return sampled;
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
  return todayRows.filter(row => {
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
  const rows = downsample(filteredRows());
  const empty = document.querySelector('#chart-empty');
  document.querySelector('#sample-count').textContent = String(rows.length);

  const solarValues = rows.map(row => number(row.pv_watts)).filter(value => value !== null);
  const loadValues = rows.map(row => number(row.ac_load_watts)).filter(value => value !== null);
  const batteryValues = rows.map(row => number(row.battery_voltage)).filter(value => value !== null);
  document.querySelector('#solar-peak').textContent = solarValues.length ? `${Math.round(Math.max(...solarValues))} W` : '— W';
  document.querySelector('#load-peak').textContent = loadValues.length ? `${Math.round(Math.max(...loadValues))} W` : '— W';
  document.querySelector('#trend-battery-min').textContent = batteryValues.length ? `${Math.min(...batteryValues).toFixed(1)} V` : '— V';

  const ctx = chartCanvas.getContext('2d');
  const octx = overlayCanvas.getContext('2d');
  if (rows.length < 2) {
    empty.hidden = false;
    ctx.clearRect(0, 0, chartCanvas.width, chartCanvas.height);
    octx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    renderedChart = null;
    return;
  }
  empty.hidden = true;

  const ratio = window.devicePixelRatio || 1;
  const width = chartCanvas.clientWidth;
  const height = chartCanvas.clientHeight;
  chartCanvas.width = Math.round(width * ratio);
  chartCanvas.height = Math.round(height * ratio);
  overlayCanvas.width = chartCanvas.width;
  overlayCanvas.height = chartCanvas.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(ratio, ratio);
  octx.setTransform(1, 0, 0, 1, 0, 0);
  octx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

  const showBattery = seriesVisible.battery && batteryValues.length > 1;
  const padding = {left: 34, right: showBattery ? 32 : 7, top: 9, bottom: 23};
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const visibleValues = [
    ...(seriesVisible.pv ? solarValues : []),
    ...(seriesVisible.load ? loadValues : []),
  ];
  const autoMax = niceMaximum(Math.max(...visibleValues, 100));
  const yWindow = yViewWindow || {min: 0, max: autoMax};
  const yMin = yWindow.min;
  const yMax = yWindow.max;
  const firstTime = timestamp(rows[0]);
  const lastTime = timestamp(rows.at(-1));
  const duration = Math.max(1, lastTime - firstTime);
  const formatAxisDate = time => selectedHours > 24
    ? time.toLocaleDateString([], {month: 'short', day: 'numeric'})
    : time.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});

  ctx.font = '9px Inter, system-ui';
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
    ctx.fillText(axisPower(yMax - (yMax - yMin) * index / 4), 1, y);
  }

  let batteryMin = 0;
  let batteryMax = 1;
  if (showBattery) {
    batteryMin = Math.floor(Math.min(...batteryValues) * 2) / 2 - .5;
    batteryMax = Math.ceil(Math.max(...batteryValues) * 2) / 2 + .5;
    ctx.textAlign = 'right';
    for (let index = 0; index <= 4; index += 1) {
      const y = padding.top + plotHeight * index / 4;
      const value = batteryMax - (batteryMax - batteryMin) * index / 4;
      ctx.fillStyle = '#5a86ab';
      ctx.fillText(value.toFixed(1), width - 2, y);
    }
  }

  [0, .5, 1].forEach(fraction => {
    const time = new Date(firstTime + duration * fraction);
    ctx.fillStyle = '#899790';
    ctx.textAlign = fraction === 0 ? 'left' : fraction === 1 ? 'right' : 'center';
    ctx.fillText(formatAxisDate(time), padding.left + plotWidth * fraction, height - 7);
  });

  function pointsFor(key, min, max) {
    return rows.map(row => {
      const raw = number(row[key]);
      const clamped = raw === null ? min : Math.max(min, Math.min(max, raw));
      return {
        x: padding.left + plotWidth * (timestamp(row) - firstTime) / duration,
        y: padding.top + plotHeight * (1 - (clamped - min) / ((max - min) || 1)),
      };
    });
  }

  function areaSeries(key, stroke, fill) {
    const points = pointsFor(key, yMin, yMax);
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

  function dashedLineSeries(key, stroke, min, max) {
    const points = pointsFor(key, min, max);
    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.6;
    ctx.setLineDash([5, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
    return points;
  }

  const solarPoints = seriesVisible.pv
    ? areaSeries('pv_watts', '#c9780a', 'rgba(201,120,10,.16)')
    : pointsFor('pv_watts', yMin, yMax);
  const loadPoints = seriesVisible.load
    ? areaSeries('ac_load_watts', '#087d55', 'rgba(8,125,85,.1)')
    : pointsFor('ac_load_watts', yMin, yMax);
  const batteryPoints = showBattery
    ? dashedLineSeries('battery_voltage', '#5a86ab', batteryMin, batteryMax)
    : pointsFor('battery_voltage', batteryMin, batteryMax);

  renderedChart = {
    rows, solarPoints, loadPoints, batteryPoints, padding, plotWidth, plotHeight, width, height,
    yMin, yMax,
  };
}

function resetZoom() {
  viewWindow = null;
  yViewWindow = null;
  updateZoomResetVisibility();
  drawChart();
}

function setupChartInteractions() {
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  chartWrap.appendChild(tooltip);

  function nearestIndex(clientX) {
    if (!renderedChart) return null;
    const rect = chartCanvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const {solarPoints} = renderedChart;
    let closest = 0;
    let distance = Infinity;
    solarPoints.forEach((point, index) => {
      const nextDistance = Math.abs(point.x - x);
      if (nextDistance < distance) { distance = nextDistance; closest = index; }
    });
    return closest;
  }

  function drawCrosshair(index) {
    octx.setTransform(1, 0, 0, 1, 0, 0);
    octx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    if (index === null || !renderedChart) return;
    const ratio = window.devicePixelRatio || 1;
    octx.scale(ratio, ratio);
    const {solarPoints, loadPoints, batteryPoints, padding, plotHeight} = renderedChart;
    const x = solarPoints[index].x;
    octx.beginPath();
    octx.moveTo(x, padding.top);
    octx.lineTo(x, padding.top + plotHeight);
    octx.strokeStyle = 'rgba(19,33,28,.3)';
    octx.lineWidth = 1;
    octx.setLineDash([3, 3]);
    octx.stroke();
    octx.setLineDash([]);

    function dot(point, color) {
      octx.beginPath();
      octx.arc(point.x, point.y, 3.2, 0, Math.PI * 2);
      octx.fillStyle = color;
      octx.fill();
      octx.lineWidth = 1.4;
      octx.strokeStyle = '#fff';
      octx.stroke();
    }
    if (seriesVisible.pv) dot(solarPoints[index], '#c9780a');
    if (seriesVisible.load) dot(loadPoints[index], '#087d55');
    if (seriesVisible.battery) dot(batteryPoints[index], '#5a86ab');
  }
  const octx = overlayCanvas.getContext('2d');

  function hideOverlay() {
    tooltip.hidden = true;
    drawCrosshair(null);
  }

  function showOverlay(clientX) {
    const index = nearestIndex(clientX);
    if (index === null) return hideOverlay();
    const rect = chartCanvas.getBoundingClientRect();
    const {rows, solarPoints} = renderedChart;
    const row = rows[index];
    const left = Math.max(54, Math.min(rect.width - 54, solarPoints[index].x));
    tooltip.style.left = `${left}px`;
    const when = selectedHours > 24
      ? new Date(timestamp(row)).toLocaleDateString([], {month: 'short', day: 'numeric'})
      : new Date(timestamp(row)).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    const lines = [`<strong>${when}</strong>`];
    if (seriesVisible.pv) lines.push(`<span><i class="solar-key"></i>${format(number(row.pv_watts), 0)} W</span>`);
    if (seriesVisible.load) lines.push(`<span><i class="load-key"></i>${format(number(row.ac_load_watts), 0)} W</span>`);
    if (seriesVisible.battery) lines.push(`<span><i class="battery-key"></i>${format(number(row.battery_voltage), 1)} V</span>`);
    tooltip.innerHTML = lines.join('');
    tooltip.hidden = false;
    drawCrosshair(index);
  }

  const pointers = new Map();
  let panState = null;
  let yPanState = null;
  let pinchState = null;
  let gestureMoved = false;
  let lastTap = {time: 0, x: 0, y: 0};

  function dismissHint() { chartHint.classList.add('dismissed'); }

  function inYAxisZone(clientX) {
    const rect = chartCanvas.getBoundingClientRect();
    return (clientX - rect.left) <= Y_AXIS_ZONE_WIDTH;
  }

  function currentYWindow() {
    return yViewWindow || {min: renderedChart?.yMin ?? 0, max: renderedChart?.yMax ?? 100};
  }

  function armPan(startX) {
    panState = {startX, startWindow: viewWindow || fullRangeWindow()};
  }

  chartCanvas.addEventListener('pointerdown', event => {
    dismissHint();
    try { chartCanvas.setPointerCapture(event.pointerId); } catch (_) { /* older browsers */ }
    pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pointers.size === 1) {
      gestureMoved = false;
      if (inYAxisZone(event.clientX)) {
        yPanState = {startY: event.clientY, startWindow: currentYWindow()};
        panState = null;
        hideOverlay();
      } else {
        yPanState = null;
        armPan(event.clientX);
        showOverlay(event.clientX);
      }
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchState = {
        startDistance: Math.abs(a.x - b.x) || 1,
        startDistanceY: Math.abs(a.y - b.y) || 1,
        startWindow: viewWindow || fullRangeWindow(),
        startYWindow: currentYWindow(),
      };
      yPanState = null;
      hideOverlay();
    }
  });

  chartCanvas.addEventListener('pointermove', event => {
    if (!pointers.has(event.pointerId)) {
      if (event.pointerType === 'mouse') {
        const zone = inYAxisZone(event.clientX);
        chartCanvas.classList.toggle('y-zone-active', zone);
        if (zone) hideOverlay();
        else showOverlay(event.clientX);
      }
      return;
    }
    pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});

    if (pointers.size === 2 && pinchState?.startWindow) {
      gestureMoved = true;
      const [a, b] = [...pointers.values()];
      const rect = chartCanvas.getBoundingClientRect();

      const distance = Math.abs(a.x - b.x) || 1;
      const ratio = pinchState.startDistance / distance;
      const win = pinchState.startWindow;
      const anchorFraction = Math.min(1, Math.max(0, ((a.x + b.x) / 2 - rect.left) / rect.width));
      const anchorTime = win.start + (win.end - win.start) * anchorFraction;
      const span = (win.end - win.start) * ratio;
      viewWindow = clampWindow({start: anchorTime - anchorFraction * span, end: anchorTime + (1 - anchorFraction) * span});

      const distanceY = Math.abs(a.y - b.y) || 1;
      const ratioY = pinchState.startDistanceY / distanceY;
      const winY = pinchState.startYWindow;
      const anchorFractionY = Math.min(1, Math.max(0, 1 - ((a.y + b.y) / 2 - rect.top) / rect.height));
      const anchorValue = winY.min + (winY.max - winY.min) * anchorFractionY;
      const spanY = (winY.max - winY.min) * ratioY;
      yViewWindow = clampYWindow({
        min: anchorValue - anchorFractionY * spanY,
        max: anchorValue + (1 - anchorFractionY) * spanY,
      });

      updateZoomResetVisibility();
      drawChart();
      return;
    }

    if (pointers.size === 1 && yPanState) {
      const dy = event.clientY - yPanState.startY;
      if (Math.abs(dy) < 4) return;
      gestureMoved = true;
      const rect = chartCanvas.getBoundingClientRect();
      if (!rect.height) return;
      const span = yPanState.startWindow.max - yPanState.startWindow.min;
      const shift = -(dy / rect.height) * span;
      yViewWindow = clampYWindow({
        min: yPanState.startWindow.min + shift, max: yPanState.startWindow.max + shift,
      });
      updateZoomResetVisibility();
      drawChart();
      return;
    }

    if (pointers.size === 1 && panState?.startWindow) {
      const dx = event.clientX - panState.startX;
      if (Math.abs(dx) < 4) { showOverlay(event.clientX); return; }
      gestureMoved = true;
      hideOverlay();
      const rect = chartCanvas.getBoundingClientRect();
      if (!rect.width) return;
      const win = panState.startWindow;
      const span = win.end - win.start;
      const shift = -(dx / rect.width) * span;
      viewWindow = clampWindow({start: win.start + shift, end: win.end + shift});
      updateZoomResetVisibility();
      drawChart();
    }
  });

  function endPointer(event) {
    const wasTap = pointers.size === 1 && !gestureMoved && event.pointerType !== 'mouse' && !yPanState;
    pointers.delete(event.pointerId);
    pinchState = null;
    if (pointers.size === 1) {
      // Dropped from a pinch (or a stale pan reference) back to one finger:
      // re-arm panning from the remaining finger's current position so the
      // next move doesn't jump using a stale start point.
      const [remaining] = [...pointers.values()];
      if (yPanState) yPanState = {startY: remaining.y, startWindow: currentYWindow()};
      else armPan(remaining.x);
      gestureMoved = false;
    } else if (pointers.size === 0) {
      panState = null;
      yPanState = null;
      hideOverlay();
    }
    if (wasTap) {
      const now = Date.now();
      const sameSpot = Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) < 24;
      if (now - lastTap.time < 350 && sameSpot) {
        resetZoom();
        lastTap.time = 0;
        return;
      }
      lastTap = {time: now, x: event.clientX, y: event.clientY};
    }
  }
  chartCanvas.addEventListener('pointerup', endPointer);
  chartCanvas.addEventListener('pointercancel', endPointer);
  chartCanvas.addEventListener('pointerleave', () => { if (!pointers.size) hideOverlay(); });

  chartCanvas.addEventListener('wheel', event => {
    event.preventDefault();
    dismissHint();
    if (!renderedChart) return;
    const rect = chartCanvas.getBoundingClientRect();
    const factor = event.deltaY > 0 ? 1.25 : 0.8;

    if (inYAxisZone(event.clientX)) {
      const winY = currentYWindow();
      const anchorFractionY = Math.min(1, Math.max(0, 1 - (event.clientY - rect.top) / rect.height));
      const anchorValue = winY.min + (winY.max - winY.min) * anchorFractionY;
      const spanY = (winY.max - winY.min) * factor;
      yViewWindow = clampYWindow({
        min: anchorValue - anchorFractionY * spanY, max: anchorValue + (1 - anchorFractionY) * spanY,
      });
      updateZoomResetVisibility();
      drawChart();
      return;
    }

    const win = viewWindow || fullRangeWindow();
    if (!win) return;
    const anchorFraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const anchorTime = win.start + (win.end - win.start) * anchorFraction;
    const span = (win.end - win.start) * factor;
    viewWindow = clampWindow({start: anchorTime - anchorFraction * span, end: anchorTime + (1 - anchorFraction) * span});
    updateZoomResetVisibility();
    drawChart();
  }, {passive: false});

  chartCanvas.addEventListener('dblclick', resetZoom);
}

zoomResetButton.addEventListener('click', resetZoom);

document.querySelectorAll('.legend-toggle').forEach(button => {
  const key = button.dataset.series;
  button.classList.toggle('active', seriesVisible[key]);
  button.addEventListener('click', () => {
    seriesVisible[key] = !seriesVisible[key];
    button.classList.toggle('active', seriesVisible[key]);
    saveSeriesVisibility();
    drawChart();
  });
});

function drawSparkline(canvas, values) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 100;
  const height = canvas.clientHeight || 28;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  const finite = values.filter(value => value !== null && Number.isFinite(value));
  if (finite.length < 2) return;
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min || 1;
  const points = values
    .map((value, index) => (value === null ? null : {
      x: (index / (values.length - 1)) * width,
      y: height - ((value - min) / span) * (height - 4) - 2,
    }))
    .filter(Boolean);
  ctx.beginPath();
  points.forEach((point, index) => (index === 0 ? ctx.moveTo(point.x, point.y) : ctx.lineTo(point.x, point.y)));
  ctx.strokeStyle = '#96a39c';
  ctx.lineWidth = 1.4;
  ctx.stroke();
}

function updateSparklines() {
  const rows = todayRows.slice(-60);
  drawSparkline(document.querySelector('#temperature-sparkline'), rows.map(row => {
    const values = [row.ntc2_temperature, row.ntc3_temperature, row.ntc4_temperature].map(number).filter(v => v !== null);
    return values.length ? Math.max(...values) : null;
  }));
  drawSparkline(document.querySelector('#fan-sparkline'), rows.map(row => {
    const values = [row.fan1_percent, row.fan2_percent].map(number).filter(v => v !== null);
    return values.length ? Math.max(...values) : null;
  }));
}

async function refreshHistory() {
  try {
    if (selectedHours > 24) {
      const days = Math.round(selectedHours / 24);
      const response = await fetch(`/api/history/daily?days=${days}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('History unavailable');
      const daily = await response.json();
      historyRows = daily.map(row => ({
        captured_at: `${row.day}T12:00:00+00:00`,
        pv_watts: row.avg_pv_watts,
        ac_load_watts: row.avg_load_watts,
        battery_voltage: row.min_battery_voltage,
      }));
    } else {
      const response = await fetch(`/api/history?hours=${Math.max(selectedHours, 24)}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('History unavailable');
      historyRows = await response.json();
    }
    drawChart();
  } catch (_) {
    historyRows = [];
    drawChart();
  }
}

async function refreshToday() {
  try {
    const response = await fetch('/api/history?hours=24', {cache: 'no-store'});
    if (!response.ok) throw new Error('History unavailable');
    todayRows = await response.json();
  } catch (_) {
    todayRows = [];
  }
  updateDailySummary();
  updateSparklines();
}

async function refreshPeriodTotals() {
  try {
    const response = await fetch('/api/history/daily?days=30', {cache: 'no-store'});
    if (!response.ok) throw new Error('History unavailable');
    const daily = await response.json();
    const kwh = (rows, key) => rows.reduce((total, row) => total + (number(row[key]) || 0) * 24 / 1000, 0);
    const last7 = daily.slice(-7);
    const set = (id, value) => {
      document.querySelector(id).textContent = daily.length ? `${value.toFixed(1)} kWh` : '— kWh';
    };
    set('#solar-7d', kwh(last7, 'avg_pv_watts'));
    set('#load-7d', kwh(last7, 'avg_load_watts'));
    set('#solar-30d', kwh(daily, 'avg_pv_watts'));
    set('#load-30d', kwh(daily, 'avg_load_watts'));
  } catch (_) {
    // leave placeholders
  }
}

async function renderFaultHistory() {
  const body = document.querySelector('#fault-history-body');
  body.replaceChildren();
  try {
    const response = await fetch('/api/history/faults?days=30', {cache: 'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const rows = await response.json();
    if (!rows.length) {
      const message = document.createElement('p');
      message.textContent = 'No faults recorded in the last 30 days.';
      body.appendChild(message);
      return;
    }
    rows.forEach(row => {
      const item = document.createElement('div');
      item.className = 'data-row';
      const label = document.createElement('span');
      label.textContent = new Date(row.captured_at).toLocaleString();
      const output = document.createElement('strong');
      output.textContent = row.faults;
      item.append(label, output);
      body.appendChild(item);
    });
  } catch (_) {
    const message = document.createElement('p');
    message.textContent = 'Fault history unavailable.';
    body.appendChild(message);
  }
}

faultHistoryDetails.addEventListener('toggle', () => {
  if (faultHistoryDetails.open) renderFaultHistory();
});

async function refreshSystemInfo() {
  try {
    const response = await fetch('/api/system', {cache: 'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    document.querySelector('#pi-uptime').textContent = formatUptime(data.uptime_seconds);
    document.querySelector('#pi-hostname').textContent = data.network?.hostname || '—';
    document.querySelector('#pi-ip').textContent = data.network?.ip_address || '—';
    const wifi = data.network?.wifi;
    document.querySelector('#pi-wifi').textContent = wifi
      ? (wifi.connected ? `${wifi.ssid || 'Connected'}${wifi.signal_percent != null ? ` · ${wifi.signal_percent}%` : ''}` : 'Not connected')
      : 'Wired / unavailable';
    document.querySelector('#pi-cpu-temp').textContent = data.cpu_temperature_c != null ? `${data.cpu_temperature_c.toFixed(1)}°C` : '—';
    document.querySelector('#pi-eco').textContent = data.eco_mode === null || data.eco_mode === undefined
      ? '—' : (data.eco_mode ? 'Eco (power saving)' : 'Standard');
    const throttleNote = document.querySelector('#pi-throttle-note');
    const flags = data.throttled?.now || [];
    if (flags.length) {
      throttleNote.hidden = false;
      throttleNote.textContent = `⚠ ${flags.join(', ').replace(/_/g, ' ')} — check the power supply.`;
    } else {
      throttleNote.hidden = true;
    }
  } catch (_) {
    // leave placeholders
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
    viewWindow = null;
    updateZoomResetVisibility();
    refreshHistory();
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
  resizeTimer = setTimeout(() => { drawChart(); updateSparklines(); }, 120);
});

setupChartInteractions();
applyCardPreferences();
initializeDashboardSettings();
refreshStatus();
refreshHistory();
refreshToday();
refreshPeriodTotals();
refreshSystemInfo();
setInterval(refreshStatus, 5000);
setInterval(() => {
  if (updatedLabel.dataset.time) updatedLabel.textContent = relativeTime(updatedLabel.dataset.time);
}, 15000);
setInterval(refreshHistory, 60000);
setInterval(refreshToday, 60000);
setInterval(refreshPeriodTotals, 600000);
setInterval(refreshSystemInfo, 60000);
