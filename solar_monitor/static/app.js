const metricNodes = document.querySelectorAll('[data-metric]');
const connectionPill = document.querySelector('#connection-pill');
const connectionLabel = document.querySelector('#connection');
const rawResponse = document.querySelector('#raw-response');
const updatedLabel = document.querySelector('#updated');
const batteryDirection = document.querySelector('#battery-direction');
const batteryFill = document.querySelector('#battery-fill');
const loadMeter = document.querySelector('#load-meter');
const loadPercent = document.querySelector('#load-percent');
const statePanel = document.querySelector('#state-panel');
let latestHistory = [];

function show(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'number' ? value.toFixed(digits).replace(/\.0$/, '') : value;
}

function relativeTime(value) {
  if (!value) return 'Waiting for first update';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Update time unavailable';
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 10) return 'Updated just now';
  if (seconds < 60) return `Updated ${seconds}s ago`;
  if (seconds < 3600) return `Updated ${Math.floor(seconds / 60)}m ago`;
  return `Updated ${date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}`;
}

function isActiveFault(value) {
  if (value === null || value === undefined || value === '') return false;
  return !['none', 'no faults', 'no active faults', '0', 'normal', '—']
    .includes(String(value).trim().toLowerCase());
}

function setConnection(state, label) {
  connectionPill.classList.toggle('online', state === 'online');
  connectionPill.classList.toggle('error', state === 'error');
  connectionLabel.textContent = label;
}

function updateDerivedMetrics(sample) {
  const current = Number(sample.battery_current);
  batteryDirection.textContent = Number.isFinite(current)
    ? current > .05 ? 'Charging' : current < -.05 ? 'Discharging' : 'Idle'
    : 'Awaiting data';

  const voltage = Number(sample.battery_voltage);
  const batteryLevel = Number.isFinite(voltage)
    ? Math.max(8, Math.min(100, ((voltage - 21) / 7.8) * 100))
    : 45;
  batteryFill.style.height = `${batteryLevel}%`;

  const watts = Number(sample.ac_load_watts);
  const percentage = Number.isFinite(watts)
    ? Math.max(0, Math.min(100, watts / 4000 * 100))
    : null;
  loadMeter.style.width = percentage === null ? '0%' : `${percentage}%`;
  loadPercent.textContent = percentage === null ? '—%' : `${Math.round(percentage)}%`;

  const faulting = isActiveFault(sample.faults);
  statePanel.classList.toggle('has-fault', faulting);
  const faultNode = statePanel.querySelector('[data-metric="faults"]');
  if (!faulting && (sample.faults === null || sample.faults === undefined || sample.faults === '')) {
    faultNode.textContent = 'No active faults';
  }
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error('Status unavailable');
    const data = await response.json();
    const sample = data.sample || {};
    metricNodes.forEach(node => {
      node.textContent = show(sample[node.dataset.metric]);
    });
    updateDerivedMetrics(sample);

    const diagnostic = data.diagnostic;
    if (!diagnostic) {
      setConnection('waiting', 'Waiting for inverter');
      return;
    }
    const hasReply = Boolean(diagnostic.response_hex);
    if (diagnostic.error) setConnection('error', 'Serial issue');
    else if (hasReply) setConnection('online', 'Live');
    else setConnection('waiting', 'Waiting for inverter');
    rawResponse.textContent = diagnostic.response_hex || diagnostic.error || 'No reply bytes';
    updatedLabel.dataset.capturedAt = diagnostic.captured_at || '';
    updatedLabel.textContent = relativeTime(diagnostic.captured_at);
  } catch (_) {
    setConnection('error', 'Dashboard offline');
  }
}

function numericValue(row, key) {
  const value = Number(row[key]);
  return Number.isFinite(value) ? value : null;
}

function drawChart(rows) {
  latestHistory = rows;
  const canvas = document.querySelector('#history-chart');
  const empty = document.querySelector('#chart-empty');
  document.querySelector('#history-count').textContent =
    `${rows.length} sample${rows.length === 1 ? '' : 's'}`;

  const ctx = canvas.getContext('2d');
  if (!rows.length) {
    empty.hidden = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  empty.hidden = true;

  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight || 240;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.scale(ratio, ratio);

  const compact = width < 520;
  const pad = {left: compact ? 34 : 43, right: 8, top: 12, bottom: 27};
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const values = rows
    .flatMap(row => [numericValue(row, 'pv_watts'), numericValue(row, 'ac_load_watts')])
    .filter(Number.isFinite);
  const maximum = Math.max(...values, 100);
  const roundedMax = Math.max(500, Math.ceil(maximum / 500) * 500);

  ctx.font = `${compact ? 9 : 10}px Inter, system-ui`;
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 3; i += 1) {
    const y = pad.top + chartHeight * i / 3;
    ctx.strokeStyle = '#e8eeeb';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillStyle = '#8a9891';
    ctx.fillText(String(Math.round(roundedMax * (1 - i / 3))), 1, y);
  }

  const times = rows.map(row => new Date(row.captured_at || row.recorded_at || row.timestamp));
  [0, .5, 1].forEach(fraction => {
    const index = Math.min(rows.length - 1, Math.round((rows.length - 1) * fraction));
    const date = times[index];
    if (!date || Number.isNaN(date.getTime())) return;
    const label = date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    ctx.fillStyle = '#8a9891';
    ctx.textAlign = fraction === 0 ? 'left' : fraction === 1 ? 'right' : 'center';
    ctx.fillText(label, pad.left + chartWidth * fraction, height - 8);
  });
  ctx.textAlign = 'left';

  function renderSeries(key, strokeColor, fillStart) {
    const points = rows.map((row, index) => ({
      x: pad.left + chartWidth * index / Math.max(rows.length - 1, 1),
      y: pad.top + chartHeight * (1 - Math.max(0, numericValue(row, key) || 0) / roundedMax),
    }));
    if (!points.length) return;

    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
    ctx.lineTo(points.at(-1).x, pad.top + chartHeight);
    ctx.lineTo(points[0].x, pad.top + chartHeight);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartHeight);
    gradient.addColorStop(0, fillStart);
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
    ctx.strokeStyle = strokeColor;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();
  }

  renderSeries('pv_watts', '#d98a16', 'rgba(217, 138, 22, .17)');
  renderSeries('ac_load_watts', '#16855f', 'rgba(22, 133, 95, .11)');
}

async function refreshHistory() {
  try {
    const response = await fetch('/api/history', {cache: 'no-store'});
    if (!response.ok) throw new Error('History unavailable');
    drawChart(await response.json());
  } catch (_) {
    drawChart([]);
  }
}

function updateRelativeTimestamp() {
  if (updatedLabel.dataset.capturedAt) {
    updatedLabel.textContent = relativeTime(updatedLabel.dataset.capturedAt);
  }
}

document.querySelectorAll('.mobile-nav a[href^="#"]').forEach(link => {
  link.addEventListener('click', () => {
    document.querySelectorAll('.mobile-nav a').forEach(item => item.classList.remove('active'));
    link.classList.add('active');
  });
});

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => drawChart(latestHistory), 150);
});

refreshStatus();
refreshHistory();
setInterval(refreshStatus, 5000);
setInterval(updateRelativeTimestamp, 15000);
setInterval(refreshHistory, 60000);
