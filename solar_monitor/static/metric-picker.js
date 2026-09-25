const metricPicker = document.querySelector('#metric-picker');
const extraMetricsGrid = document.querySelector('#extra-metrics-grid');
const availableMetrics = {
  pv_watts: ['Solar power', 'W'], pv_voltage: ['PV voltage', 'V'], pv_current: ['PV current', 'A'],
  battery_voltage: ['Battery voltage', 'V'], battery_current: ['Battery current', 'A'],
  ac_load_watts: ['Load power', 'W'], ac_load_va: ['Apparent load', 'VA'], ac_load_current: ['Load current', 'A'],
  output_voltage: ['Output voltage', 'V'], output_frequency: ['Output frequency', 'Hz'], output_current: ['Output current', 'A'],
  grid_voltage: ['Grid voltage', 'V'], grid_frequency: ['Grid frequency', 'Hz'],
  bus_voltage: ['DC bus voltage', 'V'], inverter_va: ['Inverter output', 'VA'],
  ntc2_temperature: ['Temperature sensor 2', '°C'], ntc3_temperature: ['Temperature sensor 3', '°C'],
  ntc4_temperature: ['Temperature sensor 4', '°C'], fan1_percent: ['Fan 1', '%'], fan2_percent: ['Fan 2', '%'],
};
let latestSample = {};
let previousSample = {};
const extraMetricStorageKey = 'solar-dashboard-extra-metrics-v1';

function finiteNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function updateFlowState(sample) {
  const solar = finiteNumber(sample.pv_watts);
  const load = finiteNumber(sample.ac_load_watts);
  const battery = finiteNumber(sample.battery_current);
  const solarIdle = solar === null || Math.abs(solar) < 1;
  const loadIdle = load === null || Math.abs(load) < 1;
  const batteryIdle = battery === null || Math.abs(battery) < .05;
  document.querySelector('.flow-connector.inbound')?.classList.toggle('is-idle', solarIdle);
  document.querySelector('.flow-connector.outbound')?.classList.toggle('is-idle', loadIdle);
  document.querySelector('.flow-card')?.classList.toggle('system-idle', solarIdle && loadIdle && batteryIdle);
}

function animateChangedValues(sample) {
  document.querySelectorAll('[data-metric]').forEach(node => {
    const key = node.dataset.metric;
    if (!(key in previousSample) || sample[key] === previousSample[key]) return;
    node.classList.remove('metric-updated');
    requestAnimationFrame(() => node.classList.add('metric-updated'));
  });
}

function loadExtraMetrics() {
  try {
    const saved = JSON.parse(localStorage.getItem(extraMetricStorageKey) || '[]');
    return Array.isArray(saved) ? saved.filter(key => availableMetrics[key]) : [];
  } catch (_) { return []; }
}
let extraMetrics = loadExtraMetrics();

function saveExtraMetrics() {
  localStorage.setItem(extraMetricStorageKey, JSON.stringify(extraMetrics));
}

function metricValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'number' ? value.toFixed(1).replace(/\.0$/, '') : String(value);
}

function renderExtraCards() {
  extraMetricsGrid.replaceChildren();
  extraMetrics.forEach(key => {
    const [label, unit] = availableMetrics[key];
    const card = document.createElement('article');
    card.className = 'extra-metric card';
    const labelNode = document.createElement('span');
    labelNode.textContent = label;
    const valueNode = document.createElement('strong');
    valueNode.textContent = metricValue(latestSample[key]);
    const unitNode = document.createElement('small');
    unitNode.textContent = ` ${unit}`;
    valueNode.appendChild(unitNode);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${label}`);
    remove.addEventListener('click', () => {
      extraMetrics = extraMetrics.filter(item => item !== key);
      saveExtraMetrics(); renderExtraCards(); renderMetricPicker();
    });
    card.append(labelNode, valueNode, remove);
    extraMetricsGrid.appendChild(card);
  });
}

function renderMetricPicker() {
  metricPicker.replaceChildren();
  Object.entries(availableMetrics).forEach(([key, [label]]) => {
    const choice = document.createElement('label');
    choice.className = 'metric-choice';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = extraMetrics.includes(key);
    checkbox.addEventListener('change', () => {
      extraMetrics = checkbox.checked
        ? [...extraMetrics.filter(item => item !== key), key]
        : extraMetrics.filter(item => item !== key);
      saveExtraMetrics(); renderExtraCards();
    });
    choice.append(checkbox, document.createTextNode(label));
    metricPicker.appendChild(choice);
  });
}

document.addEventListener('solar:sample', event => {
  const nextSample = event.detail || {};
  previousSample = latestSample;
  updateFlowState(nextSample);
  animateChangedValues(nextSample);
  latestSample = nextSample;
  renderExtraCards();
});
document.querySelector('#customize-open').addEventListener('click', renderMetricPicker);
document.querySelector('#clear-data-cards').addEventListener('click', () => {
  extraMetrics = [];
  saveExtraMetrics(); renderExtraCards(); renderMetricPicker();
});
renderMetricPicker();
renderExtraCards();

const stickyHeader = document.querySelector('.app-bar');
const updateHeaderElevation = () => stickyHeader?.classList.toggle('scrolled', window.scrollY > 4);
window.addEventListener('scroll', updateHeaderElevation, {passive: true});
updateHeaderElevation();

// Only same-page anchors name a section. The nav also links to other pages
// ("/estimate"), and passing that to querySelector throws a SyntaxError that
// aborted the rest of this file -- taking the scroll-spy with it.
const sectionLinks = [...document.querySelectorAll('.bottom-nav a')]
  .filter(link => (link.getAttribute('href') || '').startsWith('#'));
const observedSections = sectionLinks
  .map(link => document.querySelector(link.getAttribute('href')))
  .filter(Boolean);
if ('IntersectionObserver' in window) {
  const sectionObserver = new IntersectionObserver(entries => {
    const visible = entries
      .filter(entry => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    sectionLinks.forEach(link => {
      link.classList.toggle('active', link.getAttribute('href') === `#${visible.target.id}`);
    });
  }, {rootMargin: '-22% 0px -58% 0px', threshold: [0, .2, .5]});
  observedSections.forEach(section => sectionObserver.observe(section));
}
