const $ = selector => document.querySelector(selector);
const number = value => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const watts = value => `${Math.round(number(value) || 0).toLocaleString()} W`;
const kwh = value => `${(number(value) || 0).toFixed(2)} kWh`;
const timeOf = row => new Date(row.captured_at || row.timestamp).getTime();

// Slider ceilings until /api/estimate supplies the array nameplate and the
// inverter's charge limit. Both belong to the site profile, so the page
// itself knows nothing about the hardware.
const FALLBACK_ARRAY_WATTS = 5000;
const FALLBACK_CHARGE_LIMIT_AMPS = 100;
const SITE_HINT = "set in the config file's [site] table";

const state = {
  hours: 24,
  history: [],
  model: [],
  status: null,
  estimate: null,
  chartRows: [],
  hourlyOpportunity: [],
  // Null until /api/estimate answers, and null again if it stops reporting
  // them; the admin can change the capacity, so nothing is baked in here.
  capacityWatts: null,
  chargeLimitAmps: null,
};

const arrayCeiling = () => state.capacityWatts || FALLBACK_ARRAY_WATTS;
const chargeCeiling = () => state.chargeLimitAmps || FALLBACK_CHARGE_LIMIT_AMPS;

function applySystemFacts(estimate) {
  const capacity = number(estimate?.capacity_watts);
  const chargeLimit = number(estimate?.system?.configured_charge_limit_amps);
  state.capacityWatts = capacity && capacity > 0 ? capacity : null;
  state.chargeLimitAmps = chargeLimit && chargeLimit > 0 ? chargeLimit : null;

  const solarSlider = $('#available-solar');
  solarSlider.max = String(Math.round(arrayCeiling()));
  if (number(solarSlider.value) > arrayCeiling()) solarSlider.value = String(Math.round(arrayCeiling()));
  $('#charge-acceptance').max = String(Math.round(chargeCeiling()));

  const compass = deg => {
    const points = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
    return points[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
  };
  const array = estimate?.array || {};
  const orientation = $('#spec-orientation');
  if (orientation && array.tilt_degrees != null && array.azimuth_degrees != null) {
    const front = array.azimuth_degrees;
    const faces = array.dual_slope ? `${compass(front)} and ${compass(front + 180)}` : compass(front);
    orientation.textContent = faces.charAt(0).toUpperCase() + faces.slice(1);
    $('#spec-pitch').textContent =
      `${Math.round(array.tilt_degrees)}° pitch · ${Math.round(front)}°${array.dual_slope ? ` / ${Math.round((front + 180) % 360)}°` : ''}`;
  }
  if ($('#spec-stringing')) {
    $('#spec-stringing').textContent = array.stringing
      ? array.stringing.charAt(0).toUpperCase() + array.stringing.slice(1)
      : '—';
  }
  const kw = state.capacityWatts
    ? (state.capacityWatts / 1000).toFixed(2).replace(/\.?0+$/, '')
    : null;
  renderHardware(array, estimate?.system || {}, kw);
  renderScenarios(estimate?.scenarios);

  const nameplate = state.capacityWatts ? `${Math.round(state.capacityWatts).toLocaleString()} W` : '—';
  document.querySelectorAll('[data-array-headline]').forEach(node => {
    node.textContent = kw ? `YOUR ${kw} kW ARRAY` : 'YOUR ARRAY';
  });
  document.querySelectorAll('[data-array-watts]').forEach(node => { node.textContent = nameplate; });
  document.querySelectorAll('[data-charge-limit]').forEach(node => {
    node.textContent = state.chargeLimitAmps ? `${Math.round(state.chargeLimitAmps)} A` : '—';
  });
}

// The spec grid shows what the site profile names and "Not set" for what it
// leaves out. Every hardware fact arrives from /api/estimate; none is in
// the page.
function renderHardware(array, system, kw) {
  const text = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };
  const figure = (value, unit) => number(value) == null ? null : `${number(value)} ${unit}`;
  const listed = parts => parts.filter(Boolean).join(' · ') || '—';

  const moduleCount = number(array.module_count);
  const moduleWatts = number(array.module_watts);
  const modules = moduleCount && moduleWatts ? `${moduleCount} × ${moduleWatts} W` : null;
  text('spec-modules', array.description || modules || 'Not set');
  text('spec-modules-detail',
    array.description || modules ? (array.description && modules ? modules : '—') : SITE_HINT);

  const wiring = listed([array.wiring, kw ? `${kw} kW` : null]);
  text('spec-wiring', wiring === '—' ? 'Not set' : wiring);
  text('spec-wiring-detail', listed([
    figure(array.vmp_volts, 'Vmp'), figure(array.voc_volts, 'Voc'),
    figure(array.imp_amps, 'A Imp'), figure(array.isc_amps, 'A Isc'),
  ]));

  text('spec-inverter', system.inverter || 'Not set');
  text('spec-inverter-detail', listed([figure(system.configured_charge_limit_amps, 'A charge limit')]));

  text('spec-battery', system.battery_bank || 'Not set');
  const capacity = number(system.battery_capacity_ah);
  text('spec-battery-detail', listed([
    figure(system.battery_nominal_volts, 'V'),
    capacity ? `about ${Math.round(capacity)} Ah` : null,
  ]));
}

function renderScenarios(scenarios) {
  const panel = $('#recoverable');
  if (!panel) return;
  const built = number(scenarios?.as_built_kwh);
  const perFace = number(scenarios?.strings_per_face_kwh);
  const onePlane = number(scenarios?.all_one_slope_kwh);
  // Only shown when the array is actually split; a single-plane system has
  // nothing here to recover.
  if (built === null || perFace === null || onePlane === null) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  $('#scenario-asbuilt').textContent = kwh(built);
  $('#scenario-perface').textContent = kwh(perFace);
  $('#scenario-oneslope').textContent = kwh(onePlane);
  const gain = (value) => built > 0 ? `+${((value / built - 1) * 100).toFixed(0)}%` : '—';
  $('#scenario-perface-gain').textContent = gain(perFace);
  $('#scenario-oneslope-gain').textContent = gain(onePlane);
  $('#recoverable-total').textContent = `${(onePlane - built).toFixed(2)} kWh`;
}

function interpolate(rows, at, field) {
  if (!rows.length || at < timeOf(rows[0]) || at > timeOf(rows.at(-1))) return null;
  let low = 0;
  let high = rows.length - 1;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (timeOf(rows[mid]) <= at) low = mid;
    else high = mid - 1;
  }
  const before = rows[low];
  const after = rows[low + 1] || before;
  const a = number(before[field]);
  const b = number(after[field]);
  if (a === null || b === null) return a;
  const span = timeOf(after) - timeOf(before);
  return span ? a + (b - a) * ((at - timeOf(before)) / span) : a;
}

function curtailmentEvidence(row, potential) {
  const captured = number(row.pv_watts) || 0;
  const batteryVoltage = number(row.battery_voltage);
  const batteryCurrent = number(row.battery_current);
  const gap = Math.max(0, (potential || 0) - captured);
  const meaningful = gap >= Math.max(100, (potential || 0) * .12);
  if (!meaningful || !potential) return {level: 'none', watts: 0};

  if (batteryVoltage === null) return {level: 'none', watts: 0};
  if (batteryCurrent !== null && batteryCurrent < -0.5) return {level: 'none', watts: 0};

  // This bank works at two plateaus: 28.2 V absorption and 27.0 V float. An
  // earlier 27.8 V cutoff sat in the dead zone between them, so it credited
  // absorption but discarded float -- and float is the *more* certain
  // curtailment, since a full bank taking a trickle while a kilowatt goes
  // spare is exactly the case this page exists to find. What separates
  // curtailment from honest hard work is the current, not the voltage: at
  // the top of the charge, a bank still pulling real amps is not yet full.
  const HOLDING_VOLTS = 26.9;
  const ACCEPTING_AMPS = 10;
  if (batteryVoltage >= HOLDING_VOLTS) {
    const holding = batteryCurrent === null || batteryCurrent < ACCEPTING_AMPS;
    return {level: holding ? 'high' : 'low', watts: gap};
  }
  return {level: 'none', watts: 0};
}

function mergedHistory() {
  return state.history.map(row => {
    const potential = interpolate(state.model, timeOf(row), 'estimated_watts');
    const evidence = curtailmentEvidence(row, potential);
    return {
      ...row,
      potential_watts: potential,
      opportunity_watts: evidence.watts,
      opportunity_level: evidence.level,
      // Integrating a series that is genuinely zero outside high-confidence
      // periods; filtering by row instead would let a neighbouring pale-bar
      // value into the trapezoid at every transition.
      opportunity_high_watts: evidence.level === 'high' ? evidence.watts : 0,
    };
  });
}

function integrate(rows, field, predicate = () => true) {
  let wattHours = 0;
  for (let index = 1; index < rows.length; index += 1) {
    if (!predicate(rows[index])) continue;
    const previous = number(rows[index - 1][field]);
    const current = number(rows[index][field]);
    if (previous === null || current === null) continue;
    const hours = Math.min((timeOf(rows[index]) - timeOf(rows[index - 1])) / 3600000, 5 / 60);
    if (hours > 0) wattHours += ((previous + current) / 2) * hours;
  }
  return wattHours / 1000;
}

function startOfToday() {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

function prepareOpportunity(rows) {
  const today = rows.filter(row => timeOf(row) >= startOfToday());
  const buckets = new Map();
  for (let index = 1; index < today.length; index += 1) {
    const row = today[index];
    const previous = today[index - 1];
    const duration = Math.min((timeOf(row) - timeOf(previous)) / 3600000, 5 / 60);
    if (duration <= 0) continue;
    // History rows carry captured_at, not timestamp; timeOf() accepts either.
    // Reading row.timestamp directly yielded an invalid date, collapsing every
    // hour into a single NaN-keyed bucket.
    const hour = new Date(timeOf(row));
    hour.setMinutes(0, 0, 0);
    const key = hour.getTime();
    const bucket = buckets.get(key) || {timestamp: key, high: 0, low: 0};
    const value = ((number(row.opportunity_watts) || 0) * duration) / 1000;
    if (row.opportunity_level === 'high') bucket.high += value;
    if (row.opportunity_level === 'low') bucket.low += value;
    buckets.set(key, bucket);
  }
  // Keep the day's active span only: overnight hours are all zero and would
  // squeeze the real bars into a corner of the chart.
  const ordered = [...buckets.values()].sort((a, b) => a.timestamp - b.timestamp);
  const first = ordered.findIndex(bucket => bucket.high > 0 || bucket.low > 0);
  let last = -1;
  ordered.forEach((bucket, index) => { if (bucket.high > 0 || bucket.low > 0) last = index; });
  state.hourlyOpportunity = first === -1 ? [] : ordered.slice(first, last + 1);
  return today;
}

/* How well the model tracks reality *where the system was free to follow it*.
   Hours when the bank is full tell you nothing about the model's accuracy --
   the inverter is throttling, not the estimate failing -- so they are excluded.
   Publishing this is the honest answer to "how much should I trust the gap?" */
function modelAccuracy(rows) {
  const usable = rows.filter(row => {
    if (timeOf(row) < startOfToday()) return false;
    const volts = number(row.battery_voltage);
    const potential = number(row.potential_watts);
    const captured = number(row.pv_watts);
    return volts !== null && volts < 26.9        // bank could still accept charge
      && potential !== null && potential > 150   // enough sun to be meaningful
      && captured !== null;
  });
  if (usable.length < 12) return null;
  const mean = usable.reduce((total, row) => total + row.pv_watts / row.potential_watts, 0) / usable.length;
  return {samples: usable.length, ratio: mean};
}

function renderAccuracy(rows) {
  const node = $('#model-accuracy');
  if (!node) return;
  const accuracy = modelAccuracy(rows);
  if (!accuracy) {
    node.hidden = true;
    return;
  }
  const off = Math.abs(1 - accuracy.ratio) * 100;
  const direction = accuracy.ratio < 1 ? 'above' : 'below';
  node.hidden = false;
  // The line is an envelope of demonstrated output, so sitting a little
  // above the average unconstrained reading is by design, not drift.
  node.textContent = off < 3
    ? `Checked against today: while the battery could still accept charge, the line matched measured output within ${Math.max(1, off).toFixed(0)}% across ${accuracy.samples} readings.`
    : `Checked against today: while the battery could still accept charge, the line ran ${off.toFixed(0)}% ${direction} measured output across ${accuracy.samples} readings. The larger midday gap is the battery refusing charge, not the model drifting.`;
}

function setLiveState(sample) {
  const pill = $('#connection-pill');
  const connection = $('#connection');
  const updated = $('#updated');
  const capturedAt = sample?.captured_at || sample?.timestamp;
  if (!capturedAt) {
    pill.className = 'bar-live offline';
    connection.textContent = 'Offline';
    updated.textContent = 'No data';
    return;
  }
  const age = Math.max(0, (Date.now() - new Date(capturedAt).getTime()) / 1000);
  const online = age < 150;
  pill.className = `bar-live ${online ? 'online' : 'offline'}`;
  connection.textContent = online ? 'Live' : 'Stale';
  updated.textContent = age < 8 ? 'Now' : age < 60 ? `${Math.round(age)}s` : `${Math.round(age / 60)}m`;
}

function updateNow(rows) {
  const sample = state.status?.sample;
  setLiveState(sample);
  if (!sample) return;
  const at = Date.now();
  const potential = interpolate(state.model, at, 'estimated_watts') || 0;
  const captured = number(sample.pv_watts) || 0;
  const evidence = curtailmentEvidence(sample, potential);
  $('#potential-now').textContent = Math.round(potential).toLocaleString();
  $('#captured-now').textContent = Math.round(captured).toLocaleString();
  $('#unused-now').textContent = Math.round(evidence.watts).toLocaleString();
  const confidence = $('#confidence-now');
  confidence.className = evidence.level;
  if (!potential || evidence.watts === 0) confidence.textContent = 'No constrained output detected';
  else if (evidence.level === 'high') confidence.textContent = 'Battery-limit evidence found';
  else if (evidence.level === 'low') confidence.textContent = 'Possible · lower confidence';
  else confidence.textContent = 'Model gap · not counted';

  renderAccuracy(rows);
  const today = prepareOpportunity(rows);
  const capturedEnergy = integrate(today, 'pv_watts');
  const unusedEnergy = integrate(today, 'opportunity_high_watts');
  $('#opportunity-total').textContent = kwh(unusedEnergy);
  $('#energy-summary').textContent = energySummary(capturedEnergy, unusedEnergy);

  if (!$('#available-solar').dataset.touched) useLiveValues();
}

// One sentence instead of a captured/unused split bar: the unused share is
// usually zero or a sliver, so the bar read as a solid green progress meter.
function energySummary(captured, unused) {
  if (captured + unused <= 0) return 'No solar recorded yet today.';
  if (unused <= 0) return `No high-confidence losses today · ${kwh(captured)} captured.`;
  const percent = (unused / (captured + unused)) * 100;
  const share = percent < 1 ? 'under 1%' : `${Math.round(percent)}%`;
  return `≈ ${kwh(unused)} lost — ${share} of the ${kwh(captured + unused)} available today (${kwh(captured)} captured).`;
}

function canvasSetup(canvas) {
  const {ctx, width, height} = ChartCore.setupCanvas(canvas, {
    ratio: Math.min(window.devicePixelRatio || 1, 2),
  });
  return {context: ctx, width, height};
}

function nicePowerMax(value) {
  const step = ChartCore.niceStep(Math.max(1, value) / 4);
  return Math.ceil(value / step) * step;
}

const compactPower = value => ChartCore.compactNumber(value);

function drawPotentialChart() {
  const canvas = $('#potential-chart');
  const {context: ctx, width, height} = canvasSetup(canvas);
  const now = Date.now();
  const from = now - state.hours * 3600000;
  const to = now + 6 * 3600000;
  const model = state.model.filter(row => timeOf(row) >= from && timeOf(row) <= to);
  const history = state.history.filter(row => timeOf(row) >= from && timeOf(row) <= now);
  $('#potential-empty').hidden = Boolean(model.length || history.length);
  if (!model.length && !history.length) return;

  const pad = {left: 44, right: 12, top: 16, bottom: 30};
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  // Scale to what is actually plotted. Flooring this at the array nameplate
  // left the top 45% of the plot permanently empty on a normal day.
  const peak = Math.max(
    0,
    ...model.map(row => number(row.estimated_watts) || 0),
    ...history.map(row => number(row.pv_watts) || 0),
  );
  const maxValue = nicePowerMax(Math.max(300, peak * 1.08));
  const x = time => pad.left + ((time - from) / (to - from)) * plotW;
  const y = value => pad.top + plotH - clamp(value / maxValue, 0, 1) * plotH;

  const axis = ChartCore.cssValue('--color-quiet', '#6b7a73');
  const axisStrong = ChartCore.cssValue('--color-muted', '#4c5a53');
  const grid = ChartCore.cssValue('--color-border', '#e1e9e5');
  ctx.font = `10px ${ChartCore.FONT_STACK}`;
  ctx.textBaseline = 'middle';
  ChartCore.valueTicks(0, maxValue, 4).forEach(value => {
    const yy = ChartCore.crisp(y(value));
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillStyle = axis;
    ctx.textAlign = 'right';
    ctx.fillText(compactPower(value), pad.left - 8, yy);
  });
  ctx.textAlign = 'left';
  ctx.fillStyle = axisStrong;
  ctx.fillText('W', 2, pad.top - 3);

  // Same clock-boundary ticks the dashboard chart uses, so the two pages
  // share one reading of time.
  ctx.textBaseline = 'alphabetic';
  ChartCore.timeTicks(from, to, plotW, {minSpacing: 64}).forEach(tick => {
    const tx = ChartCore.crisp(x(tick.time));
    if (tx < pad.left - 1 || tx > width - pad.right + 1) return;
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(tx, pad.top); ctx.lineTo(tx, pad.top + plotH); ctx.stroke();
    ctx.fillStyle = tick.major ? axisStrong : axis;
    ctx.textAlign = 'center';
    ctx.fillText(tick.label, Math.min(Math.max(tx, pad.left + 14), width - pad.right - 14), height - 9);
  });
  ctx.textBaseline = 'middle';

  // A restrained green area makes actual energy readable without competing
  // with the model line.
  if (history.length > 1) {
    const capturedFill = ChartCore.cssValue('--chart-captured-fill', 'rgba(8,125,85,.2)');
    const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    gradient.addColorStop(0, capturedFill);
    gradient.addColorStop(1, ChartCore.fadeOut(capturedFill));
    ctx.beginPath();
    history.forEach((row, index) => index ? ctx.lineTo(x(timeOf(row)), y(number(row.pv_watts) || 0)) : ctx.moveTo(x(timeOf(row)), y(number(row.pv_watts) || 0)));
    ctx.lineTo(x(timeOf(history.at(-1))), y(0)); ctx.lineTo(x(timeOf(history[0])), y(0)); ctx.closePath();
    ctx.fillStyle = gradient; ctx.fill();
    ctx.beginPath();
    history.forEach((row, index) => index ? ctx.lineTo(x(timeOf(row)), y(number(row.pv_watts) || 0)) : ctx.moveTo(x(timeOf(row)), y(number(row.pv_watts) || 0)));
    ctx.strokeStyle = ChartCore.cssValue('--color-green', '#087d55'); ctx.lineWidth = 2.25; ctx.setLineDash([]); ctx.stroke();
  }

  // The whole point of this chart is the difference between the two lines, so
  // draw the difference rather than leaving the reader to estimate it between
  // two curves that sit on top of each other for most of the day.
  const past = model.filter(row => timeOf(row) <= now);
  if (past.length > 1 && history.length > 1) {
    const shortfall = [];
    for (const row of past) {
      const modelled = number(row.estimated_watts) || 0;
      const captured = interpolate(history, timeOf(row), 'pv_watts');
      if (captured === null) continue;
      shortfall.push({time: timeOf(row), modelled, captured});
    }
    // Fill each run where the model sits above what was captured.
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        ctx.beginPath();
        run.forEach((p, i) => i ? ctx.lineTo(x(p.time), y(p.modelled)) : ctx.moveTo(x(p.time), y(p.modelled)));
        for (let i = run.length - 1; i >= 0; i -= 1) ctx.lineTo(x(run[i].time), y(run[i].captured));
        ctx.closePath();
        ctx.fillStyle = ChartCore.cssValue('--chart-shortfall', 'rgba(201, 120, 10, .16)');
        ctx.fill();
      }
      run = [];
    };
    for (const point of shortfall) {
      if (point.modelled - point.captured > 30) run.push(point);
      else flush();
    }
    flush();
  }

  const drawModel = (rows, dashed) => {
    if (rows.length < 2) return;
    ctx.beginPath();
    rows.forEach((row, index) => index ? ctx.lineTo(x(timeOf(row)), y(number(row.estimated_watts) || 0)) : ctx.moveTo(x(timeOf(row)), y(number(row.estimated_watts) || 0)));
    ctx.strokeStyle = ChartCore.cssValue('--color-amber', '#c9780a'); ctx.lineWidth = 2.25; ctx.setLineDash(dashed ? [5, 5] : []); ctx.stroke(); ctx.setLineDash([]);
  };
  drawModel(model.filter(row => timeOf(row) <= now), false);
  const future = model.filter(row => timeOf(row) >= now);
  const lastPast = model.filter(row => timeOf(row) <= now).at(-1);
  drawModel(lastPast ? [lastPast, ...future] : future, true);

  const nowX = ChartCore.crisp(x(now));
  ctx.strokeStyle = axisStrong; ctx.lineWidth = 1; ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(nowX, pad.top); ctx.lineTo(nowX, pad.top + plotH); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = axisStrong; ctx.textAlign = 'center'; ctx.font = `9px ${ChartCore.FONT_STACK}`;
  ctx.fillText('now', nowX, pad.top - 3);
  ctx.font = `10px ${ChartCore.FONT_STACK}`;
  state.chartRows = model;
  state.chartGeometry = {from, to, x, y, pad, width, height};
}

// The reading goes in the strip above the plot rather than a tooltip over
// it, so the curves stay visible while they are being inspected.
function showPotentialReading(event) {
  if (!state.chartRows.length || !state.chartGeometry) return;
  const canvas = $('#potential-chart');
  const rect = canvas.getBoundingClientRect();
  const localX = clamp(event.clientX - rect.left, state.chartGeometry.pad.left, rect.width - state.chartGeometry.pad.right);
  const ratio = (localX - state.chartGeometry.pad.left) / (rect.width - state.chartGeometry.pad.left - state.chartGeometry.pad.right);
  const target = state.chartGeometry.from + ratio * (state.chartGeometry.to - state.chartGeometry.from);
  const point = state.chartRows.reduce((best, row) => Math.abs(timeOf(row) - target) < Math.abs(timeOf(best) - target) ? row : best);
  const actual = interpolate(state.history, timeOf(point), 'pv_watts');
  const when = new Date(point.timestamp).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'});
  $('#potential-reading').innerHTML = `<strong>${when}</strong>`
    + `<span><i class="potential-key"></i>${watts(point.estimated_watts)}</span>`
    + `<span><i class="captured-key"></i>${actual === null ? '—' : watts(actual)}</span>`
    + (point.forecast ? '<em>forecast</em>' : '');
  $('#potential-reading').hidden = false;
  $('#potential-hint').hidden = true;
}

function hidePotentialReading() {
  $('#potential-reading').hidden = true;
  $('#potential-hint').hidden = false;
}

/* What the line's height is based on. Shown under the chart so the reader
   knows whether they are looking at the array's own record or a default. */
function renderCalibration(calibration) {
  const node = $('#calibration-note');
  if (!node) return;
  if (!calibration || calibration.source !== 'learned') {
    node.textContent = calibration
      ? `Default efficiency ${Number(calibration.fallback_ratio).toFixed(2)} until a week of readings exists`
      : '';
    return;
  }
  const dayWord = calibration.days === 1 ? 'day' : 'days';
  node.textContent = `Efficiency learned from ${calibration.samples.toLocaleString()} unconstrained readings over ${calibration.days} ${dayWord} · peak ${Number(calibration.peak_factor).toFixed(2)}`;
}

function drawOpportunityChart() {
  const canvas = $('#opportunity-chart');
  const {context: ctx, width, height} = canvasSetup(canvas);
  const rows = state.hourlyOpportunity;
  const hasOpportunity = rows.some(row => row.high > .001 || row.low > .001);
  $('#opportunity-empty').hidden = hasOpportunity;
  ctx.clearRect(0, 0, width, height);
  if (!rows.length) return;
  const pad = {left: 32, right: 8, top: 20, bottom: 24};
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(.25, ...rows.map(row => row.high + row.low));
  const grid = ChartCore.cssValue('--chart-grid', '#e1e9e5');
  const axis = ChartCore.cssValue('--chart-axis', '#6b7a73');
  ctx.font = `9px ${ChartCore.FONT_STACK}`;
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (let index = 0; index <= 2; index += 1) {
    const value = max * index / 2;
    const yy = ChartCore.crisp(pad.top + plotH - (value / max) * plotH);
    ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillStyle = axis; ctx.fillText(value.toFixed(1), pad.left - 5, yy);
  }
  ctx.textAlign = 'left';
  ctx.fillText('kWh', 2, 8);
  const slot = plotW / Math.max(rows.length, 1);
  const barW = Math.max(3, Math.min(15, slot * .56));
  rows.forEach((row, index) => {
    const center = pad.left + slot * (index + .5);
    const highH = (row.high / max) * plotH;
    const lowH = (row.low / max) * plotH;
    ctx.fillStyle = ChartCore.cssValue('--chart-bar-soft', 'rgba(201,120,10,.22)');
    ctx.fillRect(center - barW / 2, pad.top + plotH - highH - lowH, barW, highH + lowH);
    ctx.fillStyle = ChartCore.cssValue('--color-amber', '#c9780a');
    ctx.fillRect(center - barW / 2, pad.top + plotH - highH, barW, highH);
    if (index === 0 || index === rows.length - 1 || index === Math.floor(rows.length / 2)) {
      ctx.fillStyle = axis; ctx.textAlign = index === 0 ? 'left' : index === rows.length - 1 ? 'right' : 'center';
      ctx.fillText(new Date(row.timestamp).toLocaleTimeString([], {hour:'numeric'}), center, height - 8);
    }
  });
}

function calculate() {
  const available = number($('#available-solar').value) || 0;
  const load = number($('#home-load').value) || 0;
  const acceptance = number($('#charge-acceptance').value) || 0;
  const duration = number($('#duration').value) || 1;
  const batteryVoltage = number(state.status?.sample?.battery_voltage) || 27;
  const inverterEfficiency = .92;
  const chargeEfficiency = .95;
  const solarForHome = Math.min(available, load / inverterEfficiency);
  const remaining = Math.max(0, available - solarForHome);
  const solarForBattery = Math.min(remaining, batteryVoltage * acceptance / chargeEfficiency);
  const unused = Math.max(0, available - solarForHome - solarForBattery);
  const usedShare = available ? ((available - unused) / available) * 100 : 100;

  $('#available-solar-value').textContent = watts(available);
  $('#home-load-value').textContent = watts(load);
  $('#charge-acceptance-value').textContent = `${Math.round(acceptance)} A`;
  $('#duration-value').textContent = `${duration.toFixed(duration % 1 ? 2 : 1)} h`;
  $('#calculator-unused-watts').textContent = Math.round(unused).toLocaleString();
  $('#calculator-unused-energy').textContent = kwh(unused * duration / 1000);
  $('#calculator-home-solar').textContent = watts(solarForHome);
  $('#calculator-battery-solar').textContent = watts(solarForBattery);
  $('#calculator-extra-load').textContent = watts(unused * inverterEfficiency);
  $('#calculator-used-bar').style.width = `${usedShare}%`;
  $('#calculator-unused-bar').style.width = `${100 - usedShare}%`;
}

function useLiveValues() {
  const sample = state.status?.sample;
  const potential = interpolate(state.model, Date.now(), 'estimated_watts');
  if (!sample || potential === null) return;
  $('#available-solar').value = clamp(Math.round(potential / 10) * 10, 0, arrayCeiling());
  $('#home-load').value = clamp(Math.round((number(sample.ac_load_watts) || 0) / 25) * 25, 0, 4000);
  $('#charge-acceptance').value = clamp(Math.max(0, Math.round(number(sample.battery_current) || 0)), 0, chargeCeiling());
  calculate();
}

async function refresh() {
  try {
    const [statusResponse, historyResponse, estimateResponse] = await Promise.all([
      fetch('/api/status', {cache: 'no-store'}),
      // Only the three columns this page reads; the full row is ~7x larger.
      fetch(`/api/history?hours=${Math.max(24, state.hours)}&points=3000`
            + '&fields=pv_watts,battery_voltage,battery_current', {cache: 'no-store'}),
      fetch(`/api/estimate?hours=${Math.max(24, state.hours)}`, {cache: 'no-store'}),
    ]);
    if (!statusResponse.ok || !historyResponse.ok || !estimateResponse.ok) throw new Error('Request failed');
    state.status = await statusResponse.json();
    state.history = (await historyResponse.json()).sort((a, b) => timeOf(a) - timeOf(b));
    state.estimate = await estimateResponse.json();
    applySystemFacts(state.estimate);
    renderCalibration(state.estimate.calibration);
    state.model = (state.estimate.points || []).sort((a, b) => timeOf(a) - timeOf(b));
    const configured = Boolean(state.estimate.configured);
    $('#estimator-offline').hidden = configured;
    $('#model-notice').hidden = !configured;
    const weatherLabels = {'open-meteo': 'Open-Meteo weather', simulated: 'Simulated weather'};
    $('#weather-source').textContent = !configured
      ? 'Estimate not configured'
      : weatherLabels[state.estimate.weather_source] || 'Clear-sky fallback';
    const rows = mergedHistory();
    updateNow(rows);
    drawPotentialChart();
    drawOpportunityChart();
  } catch (_error) {
    setLiveState(null);
    $('#weather-source').textContent = 'Data unavailable';
  }
}

document.querySelectorAll('.range-tabs button').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.range-tabs button').forEach(item => item.classList.toggle('active', item === button));
    state.hours = Number(button.dataset.hours);
    refresh();
  });
});

document.querySelectorAll('.slider-field input').forEach(input => {
  input.addEventListener('input', () => {
    $('#available-solar').dataset.touched = 'true';
    calculate();
  });
});
$('#use-live').addEventListener('click', () => {
  delete $('#available-solar').dataset.touched;
  useLiveValues();
});
document.querySelectorAll('[data-preset]').forEach(button => {
  button.addEventListener('click', () => {
    $('#available-solar').dataset.touched = 'true';
    if (button.dataset.preset === 'full') $('#charge-acceptance').value = 0;
    if (button.dataset.preset === 'max') $('#charge-acceptance').value = chargeCeiling();
    if (button.dataset.preset === 'quiet') $('#home-load').value = 100;
    calculate();
  });
});

const chart = $('#potential-chart');
chart.addEventListener('pointerdown', showPotentialReading);
chart.addEventListener('pointermove', event => {
  if (event.pointerType === 'mouse' || event.buttons) showPotentialReading(event);
});
// A finger lifting leaves the reading in place to be read; a mouse leaving
// the plot clears it.
chart.addEventListener('pointerleave', event => { if (event.pointerType === 'mouse') hidePotentialReading(); });

$('#theme-toggle')?.addEventListener('click', () => window.SolarTheme?.toggle());
document.addEventListener('themechange', () => { drawPotentialChart(); drawOpportunityChart(); });

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { drawPotentialChart(); drawOpportunityChart(); }, 100);
});

calculate();
refresh();
setInterval(refresh, 60000);
