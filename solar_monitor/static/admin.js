const $ = selector => document.querySelector(selector);
const formatBytes = n => n == null ? '—' : n < 1024*1024 ? `${Math.round(n/1024)} KB` : `${(n/1024/1024).toFixed(1)} MB`;
const formatDuration = n => n == null ? '—' : `${Math.floor(n/86400)}d ${Math.floor(n%86400/3600)}h`;
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const formatTimestamp = iso => { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit', second:'2-digit', timeZoneName:'short'}); };

async function api(url, options={}) { const response=await fetch(url,{cache:'no-store',headers:{'Content-Type':'application/json'},...options}); const data=await response.json(); if(!response.ok) throw new Error(data.error||`HTTP ${response.status}`); return data; }
document.querySelectorAll('nav button').forEach(button=>button.onclick=()=>{document.querySelectorAll('nav button,.view').forEach(x=>x.classList.remove('active'));button.classList.add('active');$(`#${button.dataset.view}`).classList.add('active');if(button.dataset.view==='data')loadFrames();if(button.dataset.view==='audit')loadAudit();if(button.dataset.view==='security')loadSecurity();});

async function overview(){const data=await api('/api/admin/overview');$('#port-state').textContent=data.ports.length?data.ports.join(', '):'Not connected';$('#db-size').textContent=formatBytes(data.database_bytes);$('#disk-free').textContent=formatBytes(data.disk_free_bytes);$('#uptime').textContent=formatDuration(data.uptime_seconds);Object.entries(data.settings).forEach(([key,value])=>{const field=document.querySelector(`[name="${key}"]`);if(field)field.value=value;});renderStorageCounts(data.storage_counts);const system=data.system||{};$('#cpu-temp').textContent=system.cpu_temperature_c!=null?`${system.cpu_temperature_c.toFixed(1)}°C`:'—';$('#cpu-load').textContent=system.cpu_load_percent!=null?`${system.cpu_load_percent}%`:'—';$('#mem-used').textContent=system.memory?`${system.memory.used_mb} / ${system.memory.total_mb} MB`:'—';$('#service-state').textContent=system.service_active==null?'Unknown':(system.service_active?'Active':'Stopped');renderPiPanel(system);}

function renderStorageCounts(counts){const target=$('#storage-counts');if(!target||!counts)return;target.textContent=`${Number(counts.samples).toLocaleString()} decoded samples · ${Number(counts.raw_frames).toLocaleString()} raw frames · ${Number(counts.config_snapshots).toLocaleString()} configuration snapshots`;}

function renderPiPanel(system){
  $('#pi-net-hostname').textContent=system.network?.hostname||'—';
  $('#pi-net-ip').textContent=system.network?.ip_address||'—';
  const wifi=system.network?.wifi;
  $('#pi-net-wifi').textContent=wifi?(wifi.connected?(wifi.ssid||'Connected'):'Not connected'):'Wired / unavailable';
  $('#pi-net-signal').textContent=wifi?.signal_percent!=null?`${wifi.signal_percent}%`:'—';
  const ecoToggle=$('#eco-toggle');
  if(document.activeElement!==ecoToggle) ecoToggle.checked=system.governor==='powersave';
}

$('#eco-toggle').addEventListener('change',async event=>{
  const enabled=event.target.checked;
  $('#eco-result').textContent='Applying…';
  try{
    const data=await api('/api/admin/system/eco',{method:'POST',body:JSON.stringify({enabled})});
    $('#eco-result').textContent=data.ok?`Governor set to ${data.governor}.`:data.message;
    if(!data.ok) event.target.checked=!enabled;
  }catch(error){$('#eco-result').textContent=error.message;event.target.checked=!enabled;}
});

async function wifiScan(){
  const body=$('#wifi-networks');
  body.innerHTML='<tr><td colspan="4">Scanning…</td></tr>';
  try{
    const data=await api('/api/admin/system/wifi');
    body.innerHTML=data.networks.length?data.networks.map(n=>`<tr><td>${esc(n.ssid)}</td><td>${n.signal_percent??'—'}%</td><td>${esc(n.security)}</td><td><button type="button" class="secondary wifi-pick" data-ssid="${esc(n.ssid)}">Connect</button></td></tr>`).join(''):'<tr><td colspan="4">No networks found</td></tr>';
    document.querySelectorAll('.wifi-pick').forEach(button=>button.onclick=()=>{
      $('#wifi-form').hidden=false;$('#wifi-ssid').value=button.dataset.ssid;$('#wifi-result').textContent='';
    });
  }catch(error){body.innerHTML=`<tr><td colspan="4">${esc(error.message)}</td></tr>`;}
}
$('#wifi-scan').onclick=wifiScan;
$('#wifi-cancel').onclick=()=>{$('#wifi-form').hidden=true;};
$('#wifi-form').onsubmit=async event=>{
  event.preventDefault();
  const form=new FormData(event.target);
  $('#wifi-result').textContent='Connecting… this can take up to 30 seconds.';
  try{
    const data=await api('/api/admin/system/wifi',{method:'POST',body:JSON.stringify({ssid:form.get('ssid'),password:form.get('password')})});
    $('#wifi-result').textContent=data.ok?'Connected. If this Pi’s address changed, reload the dashboard from its new address.':data.message;
    if(data.ok){$('#wifi-form').hidden=true;overview();}
  }catch(error){$('#wifi-result').textContent=error.message;}
};

$('#update-check').onclick=async()=>{
  $('#update-result').textContent='Checking…';
  try{
    const data=await api('/api/admin/system/update-check',{method:'POST',body:'{}'});
    const parts=[];
    if(data.git){parts.push(data.git.error?`Git: ${data.git.error}`:`Git: ${data.git.behind} commit(s) behind, ${data.git.ahead} ahead.`);}
    if(data.apt){parts.push(`APT: ${data.apt.upgradable_count} package(s) upgradable.`);}
    $('#update-result').textContent=parts.join(' ')||'No update information available.';
  }catch(error){$('#update-result').textContent=error.message;}
};

$('#restart-service').onclick=async()=>{
  if(!confirm('Restart the collector service now? Data collection pauses briefly.'))return;
  $('#power-result').textContent='Restarting…';
  try{const data=await api('/api/admin/system/restart-service',{method:'POST',body:'{}'});$('#power-result').textContent=data.ok?'Restart requested.':data.message;}catch(error){$('#power-result').textContent=error.message;}
};
$('#reboot-pi').onclick=async()=>{
  if(!confirm('Reboot the Raspberry Pi now? The dashboard will be unreachable for about a minute.'))return;
  $('#power-result').textContent='Rebooting…';
  try{const data=await api('/api/admin/system/reboot',{method:'POST',body:'{}'});$('#power-result').textContent=data.ok?'Reboot requested.':data.message;}catch(error){$('#power-result').textContent=error.message;}
};
async function serial(mode){const output=$('#serial-output');output.textContent='Working…';try{const data=await api(`/api/admin/serial/${mode}`,{method:'POST',body:'{}'});output.textContent=`${formatTimestamp(data.captured_at)}\n${data.port} @ ${data.baudrate}\nTX: ${data.request_hex||'(none)'}\nRX HEX: ${data.response_hex||'(no data)'}\nRX ASCII: ${data.response_ascii||'(no data)'}`;}catch(error){output.textContent=error.message;}overview();}
$('#listen').onclick=()=>serial('listen');$('#probe').onclick=()=>serial('probe');
$('#settings-form').onsubmit=async event=>{event.preventDefault();const payload=Object.fromEntries(new FormData(event.target));try{await api('/api/admin/settings',{method:'POST',body:JSON.stringify(payload)});$('#settings-result').textContent='Saved';}catch(error){$('#settings-result').textContent=error.message;}};
async function loadFrames(){const rows=await api('/api/admin/frames?limit=100');$('#frames').innerHTML=rows.map(r=>`<tr><td>${formatTimestamp(r.captured_at)}</td><td>${esc(r.port)}</td><td>${esc(r.query_name)}</td><td title="${esc(r.response_hex)}">${esc(r.response_hex||'—')}</td><td>${esc(r.error||'—')}</td></tr>`).join('')||'<tr><td colspan="5">No captures yet</td></tr>';}
$('#cleanup-data').onclick=async()=>{const result=$('#cleanup-result');result.textContent='Cleaning old diagnostic records…';try{const data=await api('/api/admin/data/cleanup',{method:'POST',body:'{}'});renderStorageCounts(data.counts);result.textContent=`Complete · removed ${data.raw_frames_deleted} raw frame${data.raw_frames_deleted===1?'':'s'} and ${data.config_snapshots_deleted} old configuration snapshot${data.config_snapshots_deleted===1?'':'s'}.`;overview();loadFrames();}catch(error){result.textContent=error.message;}};
async function loadAudit(){const rows=await api('/api/admin/audit');$('#audit-events').innerHTML=rows.map(r=>`<tr><td>${formatTimestamp(r.occurred_at)}</td><td>${esc(r.username||'—')}</td><td>${esc(r.action)}</td><td>${esc(r.detail||'—')}</td></tr>`).join('');}
async function loadSecurity(){const data=await api('/api/admin/security');$('#pin-status').textContent=data.pin_enabled?(data.pin_locked?'PIN is locked; full-password login required.':'PIN access is enabled.'):'PIN access is not configured.';$('#pin-form').hidden=data.auth_method!=='password';$('#disable-pin').hidden=!data.pin_enabled||data.auth_method!=='password';if(data.auth_method!=='password')$('#pin-result').textContent='Sign out and use the full password to change PIN settings.';}
$('#pin-form').onsubmit=async event=>{event.preventDefault();const form=new FormData(event.target),pin=form.get('pin');if(pin!==form.get('confirmation')){$('#pin-result').textContent='PINs do not match.';return;}try{await api('/api/admin/security',{method:'POST',body:JSON.stringify({action:'set',pin})});event.target.reset();$('#pin-result').textContent='PIN saved.';loadSecurity();}catch(error){$('#pin-result').textContent=error.message;}};
$('#disable-pin').onclick=async()=>{try{await api('/api/admin/security',{method:'POST',body:JSON.stringify({action:'disable'})});$('#pin-result').textContent='PIN disabled.';loadSecurity();}catch(error){$('#pin-result').textContent=error.message;}};
overview().catch(()=>location.href='/admin/login');
