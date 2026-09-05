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
const chartReading = document.querySelector('#chart-reading');
const zoomResetButton = document.querySelector('#zoom-reset');
const chartInspectButton = document.querySelector('#chart-inspect');
const chartPanButton = document.querySelector('#chart-pan');
const chartTableBody = document.querySelector('#chart-table-body');
const chartWindowLabel = document.querySelector('#chart-window-label');
const chartYScale = document.querySelector('#chart-y-scale');
const allData = document.querySelector('#all-data');
const allDataBody = document.querySelector('#all-data-body');
const faultHistoryDetails = document.querySelector('#fault-history');
const customizeDialog = document.querySelector('#customize-dialog');
const cardControls = document.querySelector('#card-controls');
// Prefix kept as "dashboard" so preferences saved before the lab and v2
// variants were retired still resolve.
const storageKey = name => `solar-dashboard-${name}`;

// The only columns the chart and today's tiles read. Asking for these instead
// of the whole 29-field row cuts the 24-hour response from ~870 KB to ~190 KB,
// which on a phone is the difference between the chart appearing and the page
// looking stuck while it parses.
const HISTORY_FIELDS = ['pv_watts', 'ac_load_watts', 'battery_voltage', 'battery_current'].join(',');
// The two sparklines plot only their last 60 readings, so they fetch one hour
// of their own columns rather than dragging them through the whole day.
const SPARKLINE_FIELDS = [
  'ntc2_temperature', 'ntc3_temperature', 'ntc4_temperature', 'fan1_percent', 'fan2_percent',
].join(',');

let historyRows = [];
let sparklineRows = [];
let potentialRows = []; // modelled output from /api/estimate, past points only
let todayRows = [];
let selectedHours = 24;
let viewWindow = null;
let yViewWindow = null; // {min, max} in watts, or null for auto-scale
const Y_AXIS_ZONE_WIDTH = 44; // px from the left edge treated as the "drag the scale" zone
let renderedChart = null;
let latestPayload = null;
let latestStatusSample = {};
let chartMode = 'inspect';

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
    const saved = JSON.parse(localStorage.getItem(storageKey('cards-v2')));
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
    return {...defaults, ...JSON.parse(localStorage.getItem(storageKey('settings-v2')) || '{}')};
  } catch (_) {
    return defaults;
  }
}

let dashboardSettings = loadDashboardSettings();

// `current` (battery amps) and `potential` (the learned model) are the
// advanced series: hidden behind "More" so the default chart stays simple.
function loadSeriesVisibility() {
  const defaults = {pv: true, load: true, battery: false, current: false, potential: false, advanced: false};
  // A link can name the series to show (?series=pv,load,battery,current,potential).
  // It is not saved, so it never overrides what this device chose for itself.
  const requested = new URLSearchParams(location.search).get('series');
  if (requested) {
    const wanted = new Set(requested.split(',').map(name => name.trim()));
    return {
      pv: wanted.has('pv'), load: wanted.has('load'), battery: wanted.has('battery'),
      current: wanted.has('current'), potential: wanted.has('potential'),
      advanced: wanted.has('current') || wanted.has('potential'),
    };
  }
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey('series-v1')));
    return {
      pv: saved?.pv !== false, load: saved?.load !== false, battery: Boolean(saved?.battery),
      current: Boolean(saved?.current), potential: Boolean(saved?.potential),
      advanced: Boolean(saved?.advanced),
    };
  } catch (_) {
    return defaults;
  }
}

let seriesVisible = loadSeriesVisibility();

function saveDashboardSettings() {
  localStorage.setItem(storageKey('settings-v2'), JSON.stringify(dashboardSettings));
}

function saveCardPreferences() {
  localStorage.setItem(storageKey('cards-v2'), JSON.stringify(cardPreferences));
}

function saveSeriesVisibility() {
  localStorage.setItem(storageKey('series-v1'), JSON.stringify(seriesVisible));
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

// The server's charge count, when it has one. It replaces the voltage guess
// on the battery row and the health card, and lends its measured capacity
// to the runtime estimate.
let batteryState = null;
const countedSoc = () => (batteryState?.state?.method === 'coulomb' ? number(batteryState.state.soc) : null);

function updateEnergyInsights(sample) {
  const current = number(sample.battery_current);
  const voltage = number(sample.battery_voltage);
  const counted = countedSoc();
  const soc = counted ?? batteryStateOfCharge(voltage);
  document.querySelector('#battery-soc').textContent = soc === null ? '—%'
    : counted !== null ? `${Math.round(soc)}%` : `~${Math.round(soc)}%`;

  // Runtime only means something while discharging, and only once a bank
  // capacity is known -- measured by the counter, or entered by hand.
  // Rather than printing a two-word prompt into a column too narrow to hold
  // it, the tile simply stays out of the way until it has a figure.
  const capacity = number(batteryState?.state?.capacity_ah) ?? Number(dashboardSettings.capacityAh);
  const configured = Number.isFinite(capacity) && capacity > 0;
  const discharging = current !== null && current < -.05;
  const runtimeTile = document.querySelector('.battery-runtime-estimate');
  if (runtimeTile) runtimeTile.hidden = !(configured && discharging);
  if (configured && discharging && soc !== null) {
    document.querySelector('#battery-runtime').textContent =
      formatRuntime(capacity * soc / 100 / Math.abs(current));
  }

}

const CONFIG_LABELS = {
  output_source_priority: 'Output priority', maximum_charge_current_a: 'Max charge current',
  ac_input_range: 'AC input range', power_saving: 'Power saving', unknown_flag_byte8_bits34: 'Unidentified flag (b8.3-4)',
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
  document.dispatchEvent(new CustomEvent('solar:sample', {detail: sample}));
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

// Thinned to the plot's own width, keeping each column's extremes, so the
// drawn peaks are the real peaks and match the summary tiles at every range.
function downsample(rows) {
  const plotWidth = Math.max(100, chartCanvas.clientWidth - 90);
  return ChartCore.downsample(rows, plotWidth, timestamp, ['pv_watts', 'ac_load_watts', 'battery_voltage', 'battery_current']);
}

// Linear interpolation of a series at a time, for reading the model where
// the readings are: the two are sampled on different clocks.
function valueAt(rows, at, key) {
  if (!rows.length || at < timestamp(rows[0]) || at > timestamp(rows.at(-1))) return null;
  let low = 0;
  let high = rows.length - 1;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (timestamp(rows[mid]) <= at) low = mid;
    else high = mid - 1;
  }
  const before = rows[low];
  const after = rows[low + 1] || before;
  const a = number(before[key]);
  const b = number(after[key]);
  if (a === null || b === null) return a;
  const span = timestamp(after) - timestamp(before);
  return span ? a + (b - a) * ((at - timestamp(before)) / span) : a;
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

// Kept as a thin alias so the drawing code reads the same; the smoothing it
// used to apply treated readings as Bezier control points, so the line ran
// through midpoints and clipped every peak by up to 10%.
const roundedPath = (ctx, points) => ChartCore.linePath(ctx, points);

function renderChartTable(rows) {
  const displayRows = rows.slice(-120).reverse();
  chartTableBody.replaceChildren();
  displayRows.forEach(row => {
    const tr = document.createElement('tr');
    const values = [
      new Date(timestamp(row)).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}),
      `${format(number(row.pv_watts), 0)} W`,
      `${format(number(row.ac_load_watts), 0)} W`,
      `${format(number(row.battery_voltage), 1)} V`,
    ];
    values.forEach(value => { const td = document.createElement('td'); td.textContent = value; tr.appendChild(td); });
    chartTableBody.appendChild(tr);
  });
  chartWindowLabel.textContent = `${rows.length} samples · newest first`;
}

// Chart colours are read from the theme tokens at paint time, so the canvas
// follows the scheme in force. The fallbacks are the light values and only
// matter if theme.css failed to load.
const chartColor = (name, fallback) => ChartCore.cssValue(name, fallback);

function chartPalette() {
  return {
    grid:       chartColor('--chart-grid', '#e6ede9'),
    gridStrong: chartColor('--chart-grid-strong', '#cfdbd5'),
    axis:       chartColor('--chart-axis', '#6b7a73'),
    axisStrong: chartColor('--chart-axis-strong', '#4c5a53'),
    solar:   chartColor('--chart-solar', '#c9780a'),
    solarFill: chartColor('--chart-solar-fill', 'rgba(201,120,10,.16)'),
    load:    chartColor('--chart-load', '#087d55'),
    loadFill: chartColor('--chart-load-fill', 'rgba(8,125,85,.1)'),
    battery: chartColor('--chart-battery', '#5a86ab'),
    current: chartColor('--chart-current', '#7b61c9'),
    crosshair: chartColor('--chart-crosshair', 'rgba(19,33,28,.3)'),
    dotEdge: chartColor('--chart-dot-edge', '#ffffff'),
  };
}

// The potential line only exists at chart resolution for the ranges the
// estimate covers; the daily-average views have nothing to compare it to.
const potentialAvailable = () => selectedHours <= 24;

function drawChart() {
  const palette = chartPalette();
  const rows = downsample(filteredRows());
  const empty = document.querySelector('#chart-empty');
  renderChartTable(rows);

  const solarValues = rows.map(row => number(row.pv_watts)).filter(value => value !== null);
  const loadValues = rows.map(row => number(row.ac_load_watts)).filter(value => value !== null);
  const batteryValues = rows.map(row => number(row.battery_voltage)).filter(value => value !== null);
  document.querySelector('#solar-average').textContent = solarValues.length ? `${Math.round(solarValues.reduce((sum, value) => sum + value, 0) / solarValues.length)} W` : '— W';
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

  const {width, height, ratio} = ChartCore.setupCanvas(chartCanvas);
  overlayCanvas.width = chartCanvas.width;
  overlayCanvas.height = chartCanvas.height;
  octx.setTransform(1, 0, 0, 1, 0, 0);
  octx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

  const showBattery = seriesVisible.battery && batteryValues.length > 1;
  const currentValues = rows.map(row => number(row.battery_current)).filter(value => value !== null);
  const showCurrent = seriesVisible.current && currentValues.length > 1;
  const potential = seriesVisible.potential && potentialAvailable() ? potentialRows : [];
  const showPotential = potential.length > 1;
  const compact = width < 500;
  // Each right-hand axis takes a column of labels: volts, then amps.
  const rightAxes = (showBattery ? 1 : 0) + (showCurrent ? 1 : 0);
  const padding = {left: compact ? 46 : 44, right: rightAxes ? 12 + rightAxes * (compact ? 32 : 30) : 12, top: 20, bottom: compact ? 30 : 26};
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const visibleValues = [
    ...(seriesVisible.pv ? solarValues : []),
    ...(seriesVisible.load ? loadValues : []),
    ...(showPotential ? potential.map(row => number(row.estimated_watts) || 0) : []),
  ];
  const autoMax = niceMaximum(Math.max(...visibleValues, 100));
  const yWindow = yViewWindow || {min: 0, max: autoMax};
  const yMin = yWindow.min;
  const yMax = yWindow.max;
  const firstTime = timestamp(rows[0]);
  const lastTime = timestamp(rows.at(-1));
  const duration = Math.max(1, lastTime - firstTime);

  ctx.font = `${compact ? 11 : 10}px ${ChartCore.FONT_STACK}`;
  ctx.textBaseline = 'middle';

  // Horizontal gridlines on round wattages rather than fixed quarters, so the
  // labels are numbers a reader recognises at a glance.
  const powerTicks = ChartCore.valueTicks(yMin, yMax, 4);
  const yFor = value => padding.top + plotHeight * (1 - (value - yMin) / ((yMax - yMin) || 1));
  powerTicks.forEach(value => {
    const y = ChartCore.crisp(yFor(value));
    ctx.strokeStyle = palette.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    ctx.fillStyle = palette.axis;
    ctx.textAlign = 'right';
    ctx.fillText(ChartCore.compactNumber(value), padding.left - 7, y);
  });
  ctx.textAlign = 'left';
  ctx.fillStyle = palette.axisStrong;
  ctx.fillText('W', 2, padding.top - 11);

  let batteryMin = 0;
  let batteryMax = 1;
  let axisX = width - padding.right + 6;
  if (showBattery) {
    batteryMin = Math.floor(Math.min(...batteryValues) * 2) / 2 - .5;
    batteryMax = Math.ceil(Math.max(...batteryValues) * 2) / 2 + .5;
    ctx.textAlign = 'left';
    ChartCore.valueTicks(batteryMin, batteryMax, 4).forEach(value => {
      const y = padding.top + plotHeight * (1 - (value - batteryMin) / ((batteryMax - batteryMin) || 1));
      ctx.fillStyle = palette.battery;
      ctx.fillText(value.toFixed(1), axisX, y);
    });
    ctx.fillText('V', axisX, padding.top - 11);
    axisX += compact ? 32 : 30;
  }

  // Battery current is signed: charging above zero, the home drawing from the
  // bank below it. The scale always includes zero and a faint zero line is
  // drawn so the direction can be read without the numbers.
  let currentMin = -1;
  let currentMax = 1;
  if (showCurrent) {
    const step = ChartCore.niceStep((Math.max(...currentValues) - Math.min(0, ...currentValues)) / 4 || 1);
    currentMin = Math.floor(Math.min(0, ...currentValues) / step) * step;
    currentMax = Math.ceil(Math.max(0, ...currentValues) / step) * step;
    if (currentMax === currentMin) currentMax = currentMin + step;
    ctx.textAlign = 'left';
    ChartCore.valueTicks(currentMin, currentMax, 4).forEach(value => {
      const y = padding.top + plotHeight * (1 - (value - currentMin) / ((currentMax - currentMin) || 1));
      ctx.fillStyle = palette.current;
      ctx.fillText(String(Math.round(value)), axisX, y);
    });
    ctx.fillText('A', axisX, padding.top - 11);
    const zeroY = ChartCore.crisp(padding.top + plotHeight * (1 - (0 - currentMin) / ((currentMax - currentMin) || 1)));
    ctx.save();
    ctx.globalAlpha = .45;
    ctx.strokeStyle = palette.current;
    ctx.setLineDash([2, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, zeroY);
    ctx.lineTo(width - padding.right, zeroY);
    ctx.stroke();
    ctx.restore();
  }

  // Vertical gridlines on clock boundaries -- the top of an hour, local
  // midnight -- instead of slicing the window into equal thirds.
  const xFor = time => padding.left + plotWidth * (time - firstTime) / duration;
  ctx.textBaseline = 'alphabetic';
  ChartCore.timeTicks(firstTime, lastTime, plotWidth, {minSpacing: compact ? 62 : 78})
    .forEach(tick => {
      const x = ChartCore.crisp(xFor(tick.time));
      if (x < padding.left - 1 || x > width - padding.right + 1) return;
      ctx.strokeStyle = tick.major ? palette.gridStrong : palette.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, padding.top);
      ctx.lineTo(x, padding.top + plotHeight);
      ctx.stroke();
      ctx.fillStyle = tick.major ? palette.axisStrong : palette.axis;
      ctx.textAlign = 'center';
      ctx.fillText(tick.label, Math.min(Math.max(x, padding.left + 16), width - padding.right - 16), height - 8);
    });
  ctx.textBaseline = 'middle';

  function project(time, value, min, max) {
    const clamped = value === null ? min : Math.max(min, Math.min(max, value));
    return {
      x: padding.left + plotWidth * (time - firstTime) / duration,
      y: padding.top + plotHeight * (1 - (clamped - min) / ((max - min) || 1)),
    };
  }

  function pointsFor(key, min, max) {
    return rows.map(row => project(timestamp(row), number(row[key]), min, max));
  }

  function areaSeries(key, stroke, fill, lineWidth = 2, alpha = 1) {
    const points = pointsFor(key, yMin, yMax);
    ctx.globalAlpha = alpha;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.lineTo(points.at(-1).x, padding.top + plotHeight);
    ctx.lineTo(points[0].x, padding.top + plotHeight);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotHeight);
    gradient.addColorStop(0, fill);
    // Fade the series' own colour to zero alpha. Fading to white assumed a
    // white surface and would band against any other background.
    gradient.addColorStop(1, ChartCore.fadeOut(fill));
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = lineWidth;
    ctx.stroke();
    ctx.globalAlpha = 1;
    return points;
  }

  // Battery voltage sits on its own axis over the two power series, so it is
  // cased: a wider stroke of the surface colour underneath separates it from
  // whatever it crosses, the way a road is drawn over a map. Solid rather
  // than dashed -- dashes on a dense series read as gaps in the data.
  function casedLineSeries(key, stroke, min, max) {
    const points = pointsFor(key, min, max);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    roundedPath(ctx, points);
    ctx.globalAlpha = .55;
    ctx.strokeStyle = palette.dotEdge;
    ctx.lineWidth = 4.5;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.9;
    ctx.stroke();
    return points;
  }

  // A deliberate hierarchy. Load is spiky (compressors and heaters cycling)
  // and at one reading per pixel it draws as a comb straight through the
  // solar curve, so it goes underneath, thin and translucent. Solar is the
  // smooth series people actually read: bold, opaque, on top.
  const loadPoints = seriesVisible.load
    ? areaSeries('ac_load_watts', palette.load, palette.loadFill, 1.25, .72)
    : pointsFor('ac_load_watts', yMin, yMax);
  const solarPoints = seriesVisible.pv
    ? areaSeries('pv_watts', palette.solar, palette.solarFill, 2.4)
    : pointsFor('pv_watts', yMin, yMax);
  const batteryPoints = showBattery
    ? casedLineSeries('battery_voltage', palette.battery, batteryMin, batteryMax)
    : pointsFor('battery_voltage', batteryMin, batteryMax);
  const currentPoints = showCurrent
    ? casedLineSeries('battery_current', palette.current, currentMin, currentMax)
    : pointsFor('battery_current', currentMin, currentMax);

  // The learned potential, dashed like the forecast on the estimate page and
  // clipped to the readings' own window so it never extends past the data.
  let potentialPoints = [];
  if (showPotential) {
    potentialPoints = potential
      .filter(row => timestamp(row) >= firstTime && timestamp(row) <= lastTime)
      .map(row => project(timestamp(row), number(row.estimated_watts), yMin, yMax));
    if (potentialPoints.length > 1) {
      ctx.beginPath();
      roundedPath(ctx, potentialPoints);
      ctx.strokeStyle = palette.solar;
      ctx.lineWidth = 1.75;
      ctx.setLineDash([5, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
  renderedChart = {
    rows, solarPoints, loadPoints, batteryPoints, currentPoints, potentialPoints,
    padding, plotWidth, plotHeight, width, height, yMin, yMax, showCurrent, showPotential,
    yFor: value => project(firstTime, value, yMin, yMax).y,
  };
}

function resetZoom() {
  viewWindow = null;
  yViewWindow = null;
  chartYScale.value = 'auto';
  updateZoomResetVisibility();
  drawChart();
}

function setupChartInteractions() {
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
    const palette = chartPalette();
    octx.setTransform(1, 0, 0, 1, 0, 0);
    octx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    if (index === null || !renderedChart) return;
    const ratio = window.devicePixelRatio || 1;
    octx.scale(ratio, ratio);
    const {solarPoints, loadPoints, batteryPoints, currentPoints, padding, plotHeight, rows} = renderedChart;
    const x = solarPoints[index].x;
    octx.beginPath();
    octx.moveTo(x, padding.top);
    octx.lineTo(x, padding.top + plotHeight);
    octx.strokeStyle = palette.crosshair;
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
      octx.strokeStyle = palette.dotEdge;
      octx.stroke();
    }
    if (seriesVisible.pv) dot(solarPoints[index], palette.solar);
    if (seriesVisible.load) dot(loadPoints[index], palette.load);
    if (seriesVisible.battery) dot(batteryPoints[index], palette.battery);
    if (renderedChart.showCurrent) dot(currentPoints[index], palette.current);
    if (renderedChart.showPotential) {
      const modelled = valueAt(potentialRows, timestamp(rows[index]), 'estimated_watts');
      if (modelled !== null) dot({x, y: renderedChart.yFor(Math.max(renderedChart.yMin, Math.min(renderedChart.yMax, modelled)))}, palette.solar);
    }
  }
  const octx = overlayCanvas.getContext('2d');

  // The reading is shown in the strip above the plot, never over it, so the
  // peaks stay visible while they are being inspected.
  function hideOverlay() {
    chartReading.hidden = true;
    chartHint.hidden = false;
    chartReading.parentElement.classList.remove('is-reading');
    drawCrosshair(null);
  }

  function showOverlay(clientX) {
    const index = nearestIndex(clientX);
    if (index === null) return hideOverlay();
    const {rows} = renderedChart;
    const row = rows[index];
    const at = new Date(timestamp(row));
    const when = selectedHours > 24
      ? at.toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'})
      : at.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    const lines = [`<strong>${when}</strong>`];
    if (seriesVisible.pv) lines.push(`<span><i class="solar-key"></i>${format(number(row.pv_watts), 0)} W</span>`);
    if (seriesVisible.load) lines.push(`<span><i class="load-key"></i>${format(number(row.ac_load_watts), 0)} W</span>`);
    if (seriesVisible.battery) lines.push(`<span><i class="battery-key"></i>${format(number(row.battery_voltage), 1)} V</span>`);
    if (renderedChart.showCurrent) lines.push(`<span><i class="current-key"></i>${format(number(row.battery_current), 1)} A</span>`);
    if (renderedChart.showPotential) {
      const modelled = valueAt(potentialRows, timestamp(row), 'estimated_watts');
      lines.push(`<span><i class="potential-key"></i>${modelled === null ? '—' : `${format(modelled, 0)} W`}</span>`);
    }
    chartReading.innerHTML = lines.join('');
    chartReading.hidden = false;
    chartHint.hidden = true;
    chartReading.parentElement.classList.add('is-reading');
    drawCrosshair(index);
  }

  const pointers = new Map();
  let panState = null;
  let yPanState = null;
  let pinchState = null;
  let gestureMoved = false;
  let lastTap = {time: 0, x: 0, y: 0};

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
    try { chartCanvas.setPointerCapture(event.pointerId); } catch (_) { /* older browsers */ }
    pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pointers.size === 1) {
      gestureMoved = false;
      if (chartMode === 'inspect') {
        yPanState = null;
        panState = null;
        showOverlay(event.clientX);
      } else if (inYAxisZone(event.clientX)) {
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

    if (pointers.size === 1 && chartMode === 'inspect') {
      showOverlay(event.clientX);
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
      if (chartMode === 'pan' || event.pointerType === 'mouse') hideOverlay();
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
  chartCanvas.addEventListener('pointerleave', event => { if (!pointers.size && event.pointerType === 'mouse') hideOverlay(); });

  chartCanvas.addEventListener('wheel', event => {
    event.preventDefault();
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

function setChartMode(mode) {
  chartMode = mode === 'pan' ? 'pan' : 'inspect';
  chartInspectButton.classList.toggle('active', chartMode === 'inspect');
  chartPanButton.classList.toggle('active', chartMode === 'pan');
  chartInspectButton.setAttribute('aria-pressed', String(chartMode === 'inspect'));
  chartPanButton.setAttribute('aria-pressed', String(chartMode === 'pan'));
  chartCanvas.classList.toggle('pan-mode', chartMode === 'pan');
  chartHint.textContent = chartMode === 'pan'
    ? 'Drag left or right to move through time · pinch to zoom'
    : 'Drag to inspect exact values · pinch to zoom';
}

function zoomChart(factor) {
  const full = fullRangeWindow();
  const win = viewWindow || full;
  if (!win || !full) return;
  const center = (win.start + win.end) / 2;
  const span = (win.end - win.start) * factor;
  viewWindow = clampWindow({start: center - span / 2, end: center + span / 2});
  updateZoomResetVisibility();
  drawChart();
}

chartInspectButton.addEventListener('click', () => setChartMode('inspect'));
chartPanButton.addEventListener('click', () => setChartMode('pan'));
document.querySelector('#chart-zoom-in').addEventListener('click', () => zoomChart(.6));
document.querySelector('#chart-zoom-out').addEventListener('click', () => zoomChart(1.6));
document.querySelector('#chart-reset').addEventListener('click', resetZoom);
chartYScale.addEventListener('change', () => {
  yViewWindow = chartYScale.value === 'auto'
    ? null
    : {min: 0, max: Number(chartYScale.value)};
  updateZoomResetVisibility();
  drawChart();
});
document.querySelector('#chart-export').addEventListener('click', event => {
  event.preventDefault();
  event.stopPropagation();
  const rows = filteredRows();
  const columns = ['captured_at', 'pv_watts', 'ac_load_watts', 'battery_voltage'];
  const csv = [columns.join(','), ...rows.map(row => columns.map(key => JSON.stringify(row[key] ?? '')).join(','))].join('\n');
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  link.download = `solar-readings-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});

document.querySelectorAll('.legend-toggle').forEach(button => {
  const key = button.dataset.series;
  button.classList.toggle('active', seriesVisible[key]);
  button.addEventListener('click', () => {
    seriesVisible[key] = !seriesVisible[key];
    button.classList.toggle('active', seriesVisible[key]);
    saveSeriesVisibility();
    // The model is fetched only when someone asks to see it.
    if (key === 'potential' && seriesVisible.potential && !potentialRows.length) refreshHistory();
    else drawChart();
  });
});

// "More" reveals the advanced series; the potential toggle is disabled on
// the daily-average ranges, where there is no comparable model.
const legendMore = document.querySelector('#legend-more');
const legendAdvanced = document.querySelector('#legend-advanced');
function syncAdvancedLegend() {
  legendAdvanced.hidden = !seriesVisible.advanced;
  legendMore.setAttribute('aria-expanded', String(seriesVisible.advanced));
  legendMore.textContent = seriesVisible.advanced ? 'Less' : 'More';
  const potentialToggle = legendAdvanced.querySelector('[data-series="potential"]');
  potentialToggle.disabled = !potentialAvailable();
}
legendMore.addEventListener('click', () => {
  seriesVisible.advanced = !seriesVisible.advanced;
  saveSeriesVisibility();
  syncAdvancedLegend();
});
syncAdvancedLegend();

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
  const rows = sparklineRows.slice(-60);
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
      const response = await fetch(
        `/api/history?hours=${Math.max(selectedHours, 24)}&fields=${HISTORY_FIELDS}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('History unavailable');
      historyRows = await response.json();
      // The 24-hour fetch already covers today, so today's tiles reuse it
      // rather than pulling the same payload a second time.
      todayRows = historyRows;
      updateDailySummary();
    }
    potentialRows = seriesVisible.potential && potentialAvailable() ? await fetchPotential() : [];
    drawChart();
  } catch (_) {
    historyRows = [];
    potentialRows = [];
    drawChart();
  }
}

async function fetchPotential() {
  try {
    const response = await fetch('/api/estimate?hours=24', {cache: 'no-store'});
    if (!response.ok) return [];
    const payload = await response.json();
    return (payload.points || [])
      .filter(point => !point.forecast)
      .map(point => ({captured_at: point.timestamp, estimated_watts: point.estimated_watts}));
  } catch (_) {
    return [];
  }
}

function formatHoursAgo(iso) {
  if (!iso) return '—';
  const hours = (Date.now() - new Date(iso).getTime()) / 3600000;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 48) return `${hours.toFixed(hours < 10 ? 1 : 0)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function renderBatteryHealth() {
  const card = document.querySelector('#battery-health');
  if (!card) return;
  const state = batteryState?.state;
  const days = batteryState?.days || [];
  const set = (id, text) => { document.querySelector(id).textContent = text; };
  if (!state) return;

  const counted = state.method === 'coulomb';
  // A charger set below what the chemistry wants stops short of full, so its
  // "100%" is not 100% and every capacity measured against it reads light.
  const limited = Boolean(state.charger_limited);
  set('#health-method', counted ? (limited ? 'Counting · charger-limited' : 'Counting charge') : 'Waiting for an anchor');
  document.querySelector('#health-method').classList.toggle('is-limited', counted && limited);
  set('#health-soc', counted ? `${Math.round(state.soc)}%` : '—%');
  set('#health-soc-note', !counted
    ? 'needs a completed absorption or a 30-minute rest first'
    : limited
      ? `as full as a ${state.bulk_setpoint_v.toFixed(1)} V charger gets it — the chemistry wants ${state.recommended_absorption_v.toFixed(1)} V, so true charge is higher`
      : `${state.ah_out_since_full} Ah out · ${state.ah_in_since_full} Ah in since ${state.last_full_at ? 'full' : 'rest'} ${formatHoursAgo(state.last_anchor_at)}`);
  const cycles = state.capacity_measurements.length;
  set('#health-capacity', `${state.capacity_is_lower_bound ? '≥ ' : ''}${Math.round(state.capacity_ah)} Ah`);
  set('#health-capacity-note', state.capacity_source !== 'measured'
    ? 'nameplate · not yet measured by a full-to-rest cycle'
    : state.capacity_is_lower_bound
      ? `lower bound from ${cycles} cycle${cycles === 1 ? '' : 's'} · reads light while absorption is capped low · nameplate ${Math.round(batteryState.nominal_ah)} Ah`
      : `measured over ${cycles} cycle${cycles === 1 ? '' : 's'} · nameplate ${Math.round(batteryState.nominal_ah)} Ah`);

  const today = days.at(-1);
  const withPredawn = days.filter(day => day.predawn_volts !== null);
  const latest = withPredawn.at(-1);
  set('#health-predawn', latest ? `${latest.predawn_volts.toFixed(2)} V` : '— V');
  if (withPredawn.length >= 2) {
    const delta = latest.predawn_volts - withPredawn[0].predawn_volts;
    set('#health-predawn-note', `${delta >= 0 ? '+' : ''}${delta.toFixed(2)} V over ${withPredawn.length} mornings · load ${Math.abs(latest.predawn_amps ?? 0).toFixed(1)} A`);
  } else {
    set('#health-predawn-note', latest ? `under ${Math.abs(latest.predawn_amps ?? 0).toFixed(1)} A of load` : 'the recovery gauge');
  }

  if (today?.absorption_reached) {
    const minutes = today.absorption_minutes ?? 0;
    set('#health-absorption', `${Math.floor(minutes / 60)}h ${minutes % 60}m${today.absorption_ended ? '' : ' so far'}`);
    set('#health-tail', today.tail_amps === null ? 'tail current —' : `tail current ${today.tail_amps.toFixed(1)} A`);
  } else {
    set('#health-absorption', 'Not reached');
    set('#health-tail', `bulk setpoint ${batteryState.bulk_setpoint_v.toFixed(1)} V`);
  }
  set('#health-ah', today ? `${today.ah_in.toFixed(0)} in · ${today.ah_out.toFixed(0)} out` : '— in · — out');
  set('#health-full', `last full ${formatHoursAgo(state.last_full_at)}`);

  const series = days.map(day => day.predawn_volts);
  drawSparkline(document.querySelector('#predawn-sparkline'), series);
  const first = withPredawn[0];
  set('#predawn-first', first ? `${new Date(`${first.day}T12:00`).toLocaleDateString([], {month: 'short', day: 'numeric'})} · ${first.predawn_volts.toFixed(2)} V` : '—');
  set('#predawn-last', latest && latest !== first ? `${new Date(`${latest.day}T12:00`).toLocaleDateString([], {month: 'short', day: 'numeric'})} · ${latest.predawn_volts.toFixed(2)} V` : '');
}

async function refreshBattery() {
  try {
    const response = await fetch('/api/battery', {cache: 'no-store'});
    if (!response.ok) throw new Error('Battery state unavailable');
    batteryState = await response.json();
  } catch (_) {
    batteryState = null;
  }
  renderBatteryHealth();
  if (latestStatusSample && Object.keys(latestStatusSample).length) updateEnergyInsights(latestStatusSample);
}

// Only needed on the multi-day ranges, where refreshHistory fetches daily
// averages instead and so has nothing today's tiles can reuse.
async function refreshToday() {
  try {
    const response = await fetch(`/api/history?hours=1&fields=${SPARKLINE_FIELDS}`, {cache: 'no-store'});
    if (response.ok) sparklineRows = await response.json();
  } catch (_) {
    sparklineRows = [];
  }
  updateSparklines();
  // On the multi-day ranges refreshHistory fetches daily averages, so today's
  // tiles have nothing to reuse and need their own day of readings.
  if (selectedHours > 24 || !historyRows.length) {
    try {
      const response = await fetch(`/api/history?hours=24&fields=${HISTORY_FIELDS}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('History unavailable');
      todayRows = await response.json();
    } catch (_) {
      todayRows = [];
    }
    updateDailySummary();
  }
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
    syncAdvancedLegend();
    refreshHistory();
  });
});

document.querySelectorAll('.bottom-nav a').forEach(link => {
  link.addEventListener('click', () => {
    document.querySelectorAll('.bottom-nav a').forEach(item => item.classList.toggle('active', item === link));
  });
});

// Colour scheme. theme.js owns the choice; this wires the header toggle and
// the three-way control in the customize dialog to it, and repaints the
// canvases, which cannot follow a token change on their own.
function setupThemeControls() {
  const control = document.querySelector('#theme-control');
  const reflect = () => {
    if (!control || !window.SolarTheme) return;
    const choice = SolarTheme.preference();
    control.querySelectorAll('button').forEach(button => {
      button.classList.toggle('active', button.dataset.themeChoice === choice);
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === choice));
    });
  };
  document.querySelector('#theme-toggle')?.addEventListener('click', () => SolarTheme.toggle());
  control?.querySelectorAll('button').forEach(button => {
    button.addEventListener('click', () => SolarTheme.set(button.dataset.themeChoice));
  });
  document.addEventListener('themechange', () => {
    reflect();
    drawChart();
    updateSparklines();
  });
  reflect();
}

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { drawChart(); updateSparklines(); }, 120);
});

setupChartInteractions();
setupThemeControls();
applyCardPreferences();
initializeDashboardSettings();
refreshStatus();
refreshHistory();
refreshToday();
refreshPeriodTotals();
refreshSystemInfo();
refreshBattery();
setInterval(refreshStatus, 5000);
setInterval(refreshBattery, 60000);
setInterval(() => {
  if (updatedLabel.dataset.time) updatedLabel.textContent = relativeTime(updatedLabel.dataset.time);
}, 15000);
setInterval(refreshHistory, 60000);
setInterval(refreshToday, 60000);
setInterval(refreshPeriodTotals, 600000);
setInterval(refreshSystemInfo, 60000);
