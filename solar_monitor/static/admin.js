const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => (
  {'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[c]
));

function formatBytes(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function formatDuration(seconds) {
  if (seconds == null) return '—';
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}

function formatTimestamp(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  });
}

async function api(url, options = {}) {
  const response = await fetch(url, {cache: 'no-store', headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

// The same pair of calls every form below makes: POST it, then say how it went.
async function submitSettings(form, result) {
  const payload = Object.fromEntries(new FormData(form));
  try {
    await api('/api/admin/settings', {method: 'POST', body: JSON.stringify(payload)});
    result.textContent = 'Saved';
  } catch (error) {
    result.textContent = error.message;
  }
}

document.querySelectorAll('nav button').forEach(button => {
  button.onclick = () => {
    document.querySelectorAll('nav button, .view').forEach(node => node.classList.remove('active'));
    button.classList.add('active');
    $(`#${button.dataset.view}`).classList.add('active');
    const loaders = {data: loadFrames, audit: loadAudit, security: loadSecurity, inverter: loadInverter};
    loaders[button.dataset.view]?.();
  };
});

$('#theme-toggle')?.addEventListener('click', () => window.SolarTheme?.toggle());

async function overview() {
  const data = await api('/api/admin/overview');
  renderPort(data.serial_link, data.ports);
  $('#db-size').textContent = formatBytes(data.database_bytes);
  $('#disk-free').textContent = formatBytes(data.disk_free_bytes);
  $('#uptime').textContent = formatDuration(data.uptime_seconds);
  Object.entries(data.settings).forEach(([key, value]) => {
    const field = document.querySelector(`[name="${key}"]`);
    if (field) field.value = value;
  });
  renderStorageCounts(data.storage_counts);
  renderSerialLink(data.serial_link);
  const system = data.system || {};
  $('#cpu-temp').textContent = system.cpu_temperature_c != null ? `${system.cpu_temperature_c.toFixed(1)}°C` : '—';
  $('#cpu-load').textContent = system.cpu_load_percent != null ? `${system.cpu_load_percent}%` : '—';
  $('#mem-used').textContent = system.memory ? `${system.memory.used_mb} / ${system.memory.total_mb} MB` : '—';
  $('#service-state').textContent = system.service_active == null ? 'Unknown' : (system.service_active ? 'Active' : 'Stopped');
  renderPiPanel(system);
}

// The adapter the collector is actually using, by its short name; the full
// by-id path is on hover. Listing every serial device on the machine said
// nothing about which one was being polled.
// A device path by its last component; anything else (sim://) as it is.
const shortPort = port => (port.startsWith('/') ? port.split('/').pop() : port);

function renderPort(link, detected) {
  const inUse = link?.port_in_use || (link?.configured_port !== 'auto' ? link?.configured_port : null);
  $('#port-state').textContent = inUse ? shortPort(inUse) : (detected.length ? 'Found, not polled yet' : 'Not connected');
  $('#port-state').title = inUse || detected.join('\n');
  $('#port-detail').textContent = `${detected.length} USB serial adapter${detected.length === 1 ? '' : 's'} detected`;
}

// The collector's own link, from the config file. Shown, not edited: a copy
// the portal could change would only disagree with what is running.
function renderSerialLink(link) {
  if (!link) return;
  const port = link.configured_port === 'auto'
    ? `auto${link.port_in_use ? ` → ${shortPort(link.port_in_use)}` : ' · no adapter found yet'}`
    : link.configured_port;
  $('#link-port').textContent = port;
  $('#link-port').title = link.port_in_use || link.configured_port;
  $('#link-line').textContent = `${link.baudrate} baud · 8N1`;
  $('#link-timeout').textContent = `${link.timeout_seconds} s`;
  $('#link-protocol').textContent = link.protocol;
}

function renderStorageCounts(counts) {
  const target = $('#storage-counts');
  if (!target || !counts) return;
  target.textContent = `${Number(counts.samples).toLocaleString()} decoded samples · `
    + `${Number(counts.raw_frames).toLocaleString()} raw frames · `
    + `${Number(counts.config_snapshots).toLocaleString()} configuration snapshots`;
}

function renderPiPanel(system) {
  $('#pi-net-hostname').textContent = system.network?.hostname || '—';
  $('#pi-net-ip').textContent = system.network?.ip_address || '—';
  const wifi = system.network?.wifi;
  $('#pi-net-wifi').textContent = wifi ? (wifi.connected ? (wifi.ssid || 'Connected') : 'Not connected') : 'Wired / unavailable';
  $('#pi-net-signal').textContent = wifi?.signal_percent != null ? `${wifi.signal_percent}%` : '—';
  const ecoToggle = $('#eco-toggle');
  if (document.activeElement !== ecoToggle) ecoToggle.checked = system.governor === 'powersave';
}

$('#eco-toggle').addEventListener('change', async event => {
  const enabled = event.target.checked;
  $('#eco-result').textContent = 'Applying…';
  try {
    const data = await api('/api/admin/system/eco', {method: 'POST', body: JSON.stringify({enabled})});
    $('#eco-result').textContent = data.ok ? `Governor set to ${data.governor}.` : data.message;
    if (!data.ok) event.target.checked = !enabled;
  } catch (error) {
    $('#eco-result').textContent = error.message;
    event.target.checked = !enabled;
  }
});

async function wifiScan() {
  const body = $('#wifi-networks');
  body.innerHTML = '<tr><td colspan="4">Scanning…</td></tr>';
  try {
    const data = await api('/api/admin/system/wifi');
    body.innerHTML = data.networks.length
      ? data.networks.map(network => `<tr><td>${esc(network.ssid)}</td><td>${network.signal_percent ?? '—'}%</td>`
        + `<td>${esc(network.security)}</td><td><button type="button" class="secondary wifi-pick" `
        + `data-ssid="${esc(network.ssid)}">Connect</button></td></tr>`).join('')
      : '<tr><td colspan="4">No networks found</td></tr>';
    document.querySelectorAll('.wifi-pick').forEach(button => {
      button.onclick = () => {
        $('#wifi-form').hidden = false;
        $('#wifi-ssid').value = button.dataset.ssid;
        $('#wifi-result').textContent = '';
      };
    });
  } catch (error) {
    body.innerHTML = `<tr><td colspan="4">${esc(error.message)}</td></tr>`;
  }
}
$('#wifi-scan').onclick = wifiScan;
$('#wifi-cancel').onclick = () => { $('#wifi-form').hidden = true; };
$('#wifi-form').onsubmit = async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  $('#wifi-result').textContent = 'Connecting… this can take up to 30 seconds.';
  try {
    const data = await api('/api/admin/system/wifi', {
      method: 'POST', body: JSON.stringify({ssid: form.get('ssid'), password: form.get('password')}),
    });
    $('#wifi-result').textContent = data.ok
      ? 'Connected. If this Pi’s address changed, reload the dashboard from its new address.'
      : data.message;
    if (data.ok) {
      $('#wifi-form').hidden = true;
      overview();
    }
  } catch (error) {
    $('#wifi-result').textContent = error.message;
  }
};

$('#update-check').onclick = async () => {
  $('#update-result').textContent = 'Checking…';
  try {
    const data = await api('/api/admin/system/update-check', {method: 'POST', body: '{}'});
    const parts = [];
    if (data.git) {
      parts.push(data.git.error ? `Git: ${data.git.error}` : `Git: ${data.git.behind} commit(s) behind, ${data.git.ahead} ahead.`);
    }
    if (data.apt) parts.push(`APT: ${data.apt.upgradable_count} package(s) upgradable.`);
    $('#update-result').textContent = parts.join(' ') || 'No update information available.';
  } catch (error) {
    $('#update-result').textContent = error.message;
  }
};

async function powerAction(path, question, working, done) {
  if (!confirm(question)) return;
  $('#power-result').textContent = working;
  try {
    const data = await api(path, {method: 'POST', body: '{}'});
    $('#power-result').textContent = data.ok ? done : data.message;
  } catch (error) {
    $('#power-result').textContent = error.message;
  }
}
$('#restart-service').onclick = () => powerAction(
  '/api/admin/system/restart-service', 'Restart the collector service now? Data collection pauses briefly.',
  'Restarting…', 'Restart requested.',
);
$('#reboot-pi').onclick = () => powerAction(
  '/api/admin/system/reboot', 'Reboot the Raspberry Pi now? The dashboard will be unreachable for about a minute.',
  'Rebooting…', 'Reboot requested.',
);

async function serial(mode) {
  const output = $('#serial-output');
  output.textContent = 'Working…';
  try {
    const data = await api(`/api/admin/serial/${mode}`, {method: 'POST', body: '{}'});
    output.textContent = [
      formatTimestamp(data.captured_at),
      `${data.port} @ ${data.baudrate}`,
      `TX: ${data.request_hex || '(none)'}`,
      `RX HEX: ${data.response_hex || '(no data)'}`,
      `RX ASCII: ${data.response_ascii || '(no data)'}`,
    ].join('\n');
  } catch (error) {
    output.textContent = error.message;
  }
  overview();
}
$('#listen').onclick = () => serial('listen');
$('#probe').onclick = () => serial('probe');

$('#settings-form').onsubmit = event => {
  event.preventDefault();
  submitSettings(event.target, $('#settings-result'));
};
$('#estimator-form').onsubmit = event => {
  event.preventDefault();
  submitSettings(event.target, $('#estimator-result'));
};
$('#estimator-locate').onclick = () => {
  const result = $('#estimator-result');
  if (!navigator.geolocation) {
    result.textContent = 'This browser cannot share its location.';
    return;
  }
  result.textContent = 'Locating…';
  navigator.geolocation.getCurrentPosition(position => {
    $('[name="estimator_latitude"]').value = position.coords.latitude.toFixed(5);
    $('[name="estimator_longitude"]').value = position.coords.longitude.toFixed(5);
    result.textContent = 'Location filled in — review and save.';
  }, error => {
    result.textContent = `Could not get location: ${error.message}`;
  }, {enableHighAccuracy: false, timeout: 10000});
};

async function loadFrames() {
  const rows = await api('/api/admin/frames?limit=100');
  $('#frames').innerHTML = rows.map(row => `<tr><td>${formatTimestamp(row.captured_at)}</td><td>${esc(row.port)}</td>`
    + `<td>${esc(row.query_name)}</td><td title="${esc(row.response_hex)}">${esc(row.response_hex || '—')}</td>`
    + `<td>${esc(row.error || '—')}</td></tr>`).join('') || '<tr><td colspan="5">No captures yet</td></tr>';
}

$('#cleanup-data').onclick = async () => {
  const result = $('#cleanup-result');
  result.textContent = 'Cleaning old diagnostic records…';
  try {
    const data = await api('/api/admin/data/cleanup', {method: 'POST', body: '{}'});
    renderStorageCounts(data.counts);
    const plural = count => (count === 1 ? '' : 's');
    result.textContent = `Complete · removed ${data.raw_frames_deleted} raw frame${plural(data.raw_frames_deleted)} `
      + `and ${data.config_snapshots_deleted} old configuration snapshot${plural(data.config_snapshots_deleted)}.`;
    overview();
    loadFrames();
  } catch (error) {
    result.textContent = error.message;
  }
};

async function loadAudit() {
  const rows = await api('/api/admin/audit');
  $('#audit-events').innerHTML = rows.map(row => `<tr><td>${formatTimestamp(row.occurred_at)}</td>`
    + `<td>${esc(row.username || '—')}</td><td>${esc(row.action)}</td><td>${esc(row.detail || '—')}</td></tr>`).join('');
}

async function loadSecurity() {
  const data = await api('/api/admin/security');
  $('#pin-status').textContent = !data.pin_enabled ? 'PIN access is not configured.'
    : data.pin_locked ? 'PIN is locked; full-password login required.' : 'PIN access is enabled.';
  const byPassword = data.auth_method === 'password';
  $('#pin-form').hidden = !byPassword;
  $('#disable-pin').hidden = !data.pin_enabled || !byPassword;
  if (!byPassword) $('#pin-result').textContent = 'Sign out and use the full password to change PIN settings.';
}

$('#pin-form').onsubmit = async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  const pin = form.get('pin');
  if (pin !== form.get('confirmation')) {
    $('#pin-result').textContent = 'PINs do not match.';
    return;
  }
  try {
    await api('/api/admin/security', {method: 'POST', body: JSON.stringify({action: 'set', pin})});
    event.target.reset();
    $('#pin-result').textContent = 'PIN saved.';
    loadSecurity();
  } catch (error) {
    $('#pin-result').textContent = error.message;
  }
};
$('#disable-pin').onclick = async () => {
  try {
    await api('/api/admin/security', {method: 'POST', body: JSON.stringify({action: 'disable'})});
    $('#pin-result').textContent = 'PIN disabled.';
    loadSecurity();
  } catch (error) {
    $('#pin-result').textContent = error.message;
  }
};

overview().catch(() => { location.href = '/admin/login'; });

// --- Inverter settings -------------------------------------------------------
// The only place the monitor writes to the inverter. The form is filled from
// the inverter's own latest reply; "Review" shows exactly which fields differ;
// the write is confirmed only by what the inverter reports afterwards.
const inverterState = {current: null, fields: {}, pending: null};
const INVERTER_LABELS = {
  unknown_flag_byte8_bits34: 'Unidentified flag (byte 8 bits 3-4)',
  maximum_charge_current_a: 'Maximum charge current',
  bulk_charge_voltage_v: 'Bulk / absorption voltage', float_charge_voltage_v: 'Float voltage',
  charge_cutoff_current_a: 'Charge cut-off current', low_battery_cutoff_v: 'Low-battery cut-off',
  overload_auto_restart: 'Overload auto-restart', overtemperature_auto_restart: 'Over-temperature auto-restart',
  buzzer: 'Button buzzer', warning_buzzer: 'Warning buzzer',
  unknown_u16_offset82: 'Unknown register 82', equalization_voltage_v: 'Boost voltage',
  equalization_time_min: 'Boost time', equalization_timeout_min: 'Boost timeout',
  equalization_interval_days: 'Boost interval',
};
const INVERTER_UNITS = {
  maximum_charge_current_a: ' A', bulk_charge_voltage_v: ' V', float_charge_voltage_v: ' V',
  charge_cutoff_current_a: ' A', low_battery_cutoff_v: ' V', equalization_voltage_v: ' V',
  equalization_time_min: ' min', equalization_timeout_min: ' min', equalization_interval_days: ' days',
};

function inverterDisplay(name, value) {
  if (value === undefined || value === null) return '—';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value === 'number') return `${value}${INVERTER_UNITS[name] || ''}`;
  return String(value);
}

// The form's own value for a field, typed the way the API compares it.
function inverterFormValue(name) {
  const field = $(`#inverter-form [name="${name}"]`);
  if (!field || field.value === '') return undefined;
  const spec = inverterState.fields[name];
  if (spec?.kind === 'bits' && spec.choices.includes('true')) return field.value === 'true';
  if (spec?.kind === 'bits') return field.value;
  return Number(field.value);
}

function inverterSame(name, a, b) {
  if (typeof a === 'boolean' || typeof b === 'boolean') return Boolean(a) === Boolean(b);
  if (typeof a === 'number' || typeof b === 'number') return Math.abs(Number(a) - Number(b)) < 0.051;
  return String(a) === String(b);
}

function fillInverterForm(settings) {
  Object.keys(INVERTER_LABELS).forEach(name => {
    const field = $(`#inverter-form [name="${name}"]`);
    if (!field || settings[name] === undefined) return;
    const value = settings[name];
    if (typeof value === 'boolean') field.value = value ? 'true' : 'false';
    else field.value = String(value);
  });
}

function renderInverterSnapshots(snapshots) {
  const body = $('#inverter-snapshots');
  if (!snapshots.length) { body.innerHTML = '<tr><td colspan="7">No configuration snapshots yet.</td></tr>'; return; }
  body.innerHTML = snapshots.map(snap => {
    const s = snap.settings;
    return `<tr><td>${formatTimestamp(snap.captured_at)}</td><td>${esc(s.bulk_charge_voltage_v)} V</td><td>${esc(s.float_charge_voltage_v)} V</td><td>${esc(s.charge_cutoff_current_a)} A</td><td>${esc(s.low_battery_cutoff_v)} V</td><td>${esc(s.equalization_voltage_v)} V</td><td><button type="button" class="secondary" data-restore="${snap.id}">Restore</button></td></tr>`;
  }).join('');
  body.querySelectorAll('[data-restore]').forEach(button => {
    button.onclick = async () => {
      if (!confirm('Write this snapshot\'s charging, protection and boost values back to the inverter?')) return;
      await runInverterWrite('/api/admin/inverter/restore', {snapshot_id: Number(button.dataset.restore)});
    };
  });
}

async function loadInverter() {
  const result = $('#inverter-result');
  try {
    const data = await api('/api/admin/inverter');
    inverterState.current = data.current?.settings || null;
    inverterState.fields = data.fields || {};
    $('#inverter-writes-toggle').checked = data.writes_enabled;
    $('#inverter-write-state').textContent = !data.collector_available
      ? 'The collector is disabled on this instance, so nothing can be written.'
      : data.writes_enabled ? 'Writes are on. Changes below go to the inverter after review.' : 'Writes are off. Switch on to enable the form below.';
    $('#writes-badge').textContent = `Inverter writes: ${data.writes_enabled ? 'on' : 'off'}`;
    $('#inverter-form').querySelectorAll('input,select,button').forEach(el => { el.disabled = !data.writes_enabled || !data.collector_available; });
    $('#inverter-preset-agm').disabled = !data.writes_enabled;
    if (inverterState.current) {
      fillInverterForm(inverterState.current);
      $('#inverter-current-stamp').textContent = `Inverter reported these at ${formatTimestamp(data.current.captured_at)}.`;
    } else {
      $('#inverter-current-stamp').textContent = 'No configuration snapshot yet — the collector reads one every few minutes.';
    }
    renderInverterSnapshots(data.snapshots || []);
    renderChangeRequests(data.requests || []);
    $('#auto-apply-toggle').checked = Boolean(data.auto_apply);
    if (document.activeElement !== $('#auto-apply-policy')) $('#auto-apply-policy').value = data.auto_apply_policy || '';
  } catch (error) {
    result.textContent = error.message;
  }
}

$('#inverter-writes-toggle').addEventListener('change', async event => {
  const enabled = event.target.checked;
  try {
    await api('/api/admin/inverter/writes', {method: 'POST', body: JSON.stringify({enabled})});
  } catch (error) {
    $('#inverter-result').textContent = error.message;
    event.target.checked = !enabled;
  }
  loadInverter();
});

$('#inverter-reload').onclick = () => { $('#inverter-review').hidden = true; loadInverter(); };

// A common 24 V AGM profile at room temperature: 14.6 V/12 V absorption (the
// menu's 29.2 V ceiling), 13.6 V float and absorption held until 2 A. The floor
// is 11.5 V/12 V: 11.0 V, the menu's lowest, is a near-total discharge.
$('#inverter-preset-agm').onclick = () => {
  const set = (name, value) => { const f = $(`#inverter-form [name="${name}"]`); if (f) f.value = String(value); };
  set('bulk_charge_voltage_v', 29.2);
  set('float_charge_voltage_v', 27.2);
  set('charge_cutoff_current_a', 2);
  set('low_battery_cutoff_v', 23.0);
  set('overload_auto_restart', 'true');
  $('#inverter-result').textContent = 'Preset filled in — nothing is written until you review and confirm.';
};

$('#inverter-form').addEventListener('submit', event => {
  event.preventDefault();
  const current = inverterState.current;
  if (!current) { $('#inverter-result').textContent = 'No configuration snapshot to compare against yet.'; return; }
  const changes = {};
  Object.keys(INVERTER_LABELS).forEach(name => {
    const wanted = inverterFormValue(name);
    if (wanted === undefined || !(name in current)) return;
    if (!inverterSame(name, wanted, current[name])) changes[name] = wanted;
  });
  const note = $('#inverter-review-note');
  note.textContent = '';
  // Bulk and float are only honoured when the inverter is on its User-defined
  // battery type, which can only be selected on the panel (menu 05) -- the
  // protocol bits that appeared to set it were disproven on 2026-09-05.
  const wantsVoltages = 'bulk_charge_voltage_v' in changes || 'float_charge_voltage_v' in changes;
  if (wantsVoltages) {
    note.textContent = 'Charge voltages are only accepted while the panel (menu 05) shows User-defined.';
  }
  if (!Object.keys(changes).length) {
    $('#inverter-review').hidden = true;
    $('#inverter-result').textContent = 'Nothing differs from what the inverter reports.';
    return;
  }
  inverterState.pending = changes;
  $('#inverter-diff').innerHTML = Object.entries(changes).map(([name, value]) =>
    `<tr><td>${esc(INVERTER_LABELS[name] || name)}</td><td>${esc(inverterDisplay(name, current[name]))}</td><td class="diff-changed">${esc(inverterDisplay(name, value))}</td></tr>`
  ).join('');
  $('#inverter-review').hidden = false;
  $('#inverter-verify-wrap').hidden = true;
  $('#inverter-result').textContent = '';
});

$('#inverter-cancel').onclick = () => { $('#inverter-review').hidden = true; inverterState.pending = null; };
$('#inverter-write').onclick = async () => {
  if (!inverterState.pending) return;
  await runInverterWrite('/api/admin/inverter/settings', {changes: inverterState.pending});
};

async function runInverterWrite(url, body) {
  const result = $('#inverter-result');
  const button = $('#inverter-write');
  button.disabled = true;
  result.textContent = 'Writing… reading the inverter, sending, reading back.';
  try {
    const data = await api(url, {method: 'POST', body: JSON.stringify(body)});
    $('#inverter-review').hidden = true;
    inverterState.pending = null;
    result.textContent = data.verified
      ? `Verified: the inverter now reports every requested value (${formatTimestamp(data.written_at)}).`
      : `Sent, but the inverter does not report ${Object.keys(data.mismatches).length} of the requested values. Check the table and the front panel.`;
    $('#inverter-verify').innerHTML = Object.entries(data.requested).map(([name, value]) => {
      const bad = data.mismatches[name];
      return `<tr><td>${esc(INVERTER_LABELS[name] || name)}</td><td>${esc(inverterDisplay(name, value))}</td><td>${esc(inverterDisplay(name, data.after[name]))}</td><td class="${bad ? 'verify-bad' : 'verify-ok'}">${bad ? 'Differs' : 'OK'}</td></tr>`;
    }).join('');
    $('#inverter-verify-wrap').hidden = false;
  } catch (error) {
    result.textContent = `Not applied: ${error.message}`;
  } finally {
    button.disabled = false;
    loadInverter();
  }
}

api('/api/admin/inverter').then(data => { $('#writes-badge').textContent = `Inverter writes: ${data.writes_enabled ? 'on' : 'off'}`; }).catch(() => {});

// --- Proposed changes --------------------------------------------------------
// A proposal is an intent, not a write. It reaches the inverter only when
// approved here or released by the automatic policy, and the collector -- the
// process that owns the serial port -- is what actually sends it.
const REQUEST_STATUS = {
  pending: 'Waiting for you', approved: 'Approved · queued', applying: 'Writing…',
  applied: 'Applied · verified', unverified: 'Sent · not verified',
  failed: 'Failed', rejected: 'Rejected', expired: 'Expired',
};

function renderChangeRequests(requests) {
  const body = $('#change-requests');
  if (!requests || !requests.length) {
    body.innerHTML = '<tr><td colspan="6">No proposals.</td></tr>';
    return;
  }
  body.innerHTML = requests.map(request => {
    const change = Object.entries(request.changes)
      .map(([name, value]) => `${esc(INVERTER_LABELS[name] || name)} → ${esc(inverterDisplay(name, value))}`).join('<br>');
    const result = request.result || {};
    let status = REQUEST_STATUS[request.status] || request.status;
    if (request.status === 'unverified' && result.mismatches) {
      status += ` (${Object.keys(result.mismatches).join(', ')})`;
    } else if (request.status === 'failed' && result.error) {
      status += ` — ${esc(result.error)}`;
    }
    const actions = request.status === 'pending'
      ? `<button type="button" data-approve="${request.id}">Approve</button> <button type="button" class="secondary" data-reject="${request.id}">Reject</button>`
      : '';
    return `<tr><td>${formatTimestamp(request.created_at)}</td><td>${esc(request.requested_by)}</td>`
      + `<td>${change}</td><td>${esc(request.reason)}</td><td>${status}</td><td>${actions}</td></tr>`;
  }).join('');

  body.querySelectorAll('[data-approve],[data-reject]').forEach(button => {
    const approve = button.hasAttribute('data-approve');
    const id = button.dataset.approve || button.dataset.reject;
    button.onclick = async () => {
      if (approve && !confirm('Approve this change? It will be written to the inverter and read back.')) return;
      button.disabled = true;
      try {
        const data = await api(`/api/admin/inverter/requests/${id}/${approve ? 'approve' : 'reject'}`, {method: 'POST', body: '{}'});
        const applied = data.applied;
        $('#inverter-result').textContent = !approve ? 'Rejected.'
          : applied ? (applied.status === 'applied'
              ? 'Verified: the inverter now reports every requested value.'
              : `Not verified: ${applied.result?.error || 'the inverter did not take every value'}.`)
          : 'Approved; the collector will apply it on its next cycle.';
      } catch (error) {
        $('#inverter-result').textContent = error.message;
      }
      loadInverter();
    };
  });
}

$('#auto-apply-toggle').addEventListener('change', async event => {
  try {
    await api('/api/admin/inverter/auto-apply', {method: 'POST', body: JSON.stringify({enabled: event.target.checked})});
    $('#auto-apply-result').textContent = event.target.checked
      ? 'On — proposals fully covered by the policy apply without asking.'
      : 'Off — every proposal waits for approval.';
  } catch (error) {
    $('#auto-apply-result').textContent = error.message;
    event.target.checked = !event.target.checked;
  }
});

$('#auto-apply-form').onsubmit = async event => {
  event.preventDefault();
  $('#auto-apply-result').textContent = 'Saving…';
  try {
    const data = await api('/api/admin/inverter/auto-apply', {
      method: 'POST',
      body: JSON.stringify({enabled: $('#auto-apply-toggle').checked, policy: $('#auto-apply-policy').value}),
    });
    $('#auto-apply-policy').value = data.auto_apply_policy;
    $('#auto-apply-result').textContent = 'Policy saved.';
  } catch (error) {
    $('#auto-apply-result').textContent = error.message;
  }
};
