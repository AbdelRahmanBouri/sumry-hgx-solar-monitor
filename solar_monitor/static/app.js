const metricNodes = document.querySelectorAll('[data-metric]');
const statusBox = document.querySelector('.status');

function show(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'number' ? value.toFixed(digits).replace(/\.0$/, '') : value;
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    const sample = data.sample || {};
    metricNodes.forEach(node => node.textContent = show(sample[node.dataset.metric]));
    const diagnostic = data.diagnostic;
    if (diagnostic) {
      const hasReply = Boolean(diagnostic.response_hex);
      statusBox.classList.toggle('online', hasReply && !diagnostic.error);
      document.querySelector('#connection').textContent = diagnostic.error ? 'Serial error' : hasReply ? 'Inverter responding' : 'No reply yet';
      document.querySelector('#raw-response').textContent = diagnostic.response_hex || diagnostic.error || 'No reply bytes';
      document.querySelector('#updated').textContent = `Capture ${new Date(diagnostic.captured_at).toLocaleString()}`;
    }
    const current = sample.battery_current;
    document.querySelector('#battery-direction').textContent = current > 0 ? 'charging' : current < 0 ? 'discharging' : '';
  } catch (_) {
    document.querySelector('#connection').textContent = 'Dashboard offline';
    statusBox.classList.remove('online');
  }
}

function drawChart(rows) {
  const canvas = document.querySelector('#history-chart');
  const empty = document.querySelector('#chart-empty');
  if (!rows.length) { empty.hidden = false; return; }
  empty.hidden = true;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = 220;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  const pad = {left: 42, right: 10, top: 12, bottom: 26};
  const values = rows.flatMap(r => [r.pv_watts, r.ac_load_watts]).filter(Number.isFinite);
  const max = Math.max(...values, 100);
  ctx.strokeStyle = '#27352c'; ctx.fillStyle = '#91a097'; ctx.font = '11px system-ui';
  for (let i = 0; i <= 4; i++) { const y = pad.top + (height-pad.top-pad.bottom)*i/4; ctx.beginPath(); ctx.moveTo(pad.left,y); ctx.lineTo(width-pad.right,y); ctx.stroke(); ctx.fillText(Math.round(max*(1-i/4)),2,y+4); }
  [['pv_watts','#d9f66f'],['ac_load_watts','#74a7ff']].forEach(([key,color]) => { ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath(); rows.forEach((r,i) => { const x=pad.left+(width-pad.left-pad.right)*i/Math.max(rows.length-1,1); const y=pad.top+(height-pad.top-pad.bottom)*(1-(r[key]||0)/max); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.stroke(); });
}

async function refreshHistory() {
  try { drawChart(await (await fetch('/api/history', {cache:'no-store'})).json()); } catch (_) {}
}

refreshStatus(); refreshHistory();
setInterval(refreshStatus, 5000); setInterval(refreshHistory, 60000);

