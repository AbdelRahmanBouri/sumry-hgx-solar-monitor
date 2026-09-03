const metricPicker = document.querySelector('#metric-picker');
const extraMetricsGrid = document.querySelector('#extra-metrics-grid');
const availableMetrics = {
  pv_watts: ['Solar power', 'W'], pv_voltage: ['PV voltage', 'V'], pv_current: ['PV current', 'A'],
  battery_voltage: ['Battery voltage', 'V'], battery_current: ['Battery current', 'A'],
  ac_load_watts: ['Load power', 'W'], ac_load_va: ['Apparent load', 'VA'], ac_load_current: ['Load current', 'A'],
  output_voltage: ['Output voltage', 'V'], output_frequency: ['Output frequency', 'Hz'], output_current: ['Output current', 'A'],
  grid_voltage: ['Grid voltage', 'V'], grid_frequency: ['Grid frequency', 'Hz'], grid_current: ['Grid current', 'A'],
  bus_voltage: ['DC bus voltage', 'V'], inverter_va: ['Inverter output', 'VA'],
  ntc2_temperature: ['Temperature sensor 2', '°C'], ntc3_temperature: ['Temperature sensor 3', '°C'],
  ntc4_temperature: ['Temperature sensor 4', '°C'], fan1_percent: ['Fan 1', '%'], fan2_percent: ['Fan 2', '%'],
};
let labSample = {};

function loadLabMetrics() {
  try {
    const saved = JSON.parse(localStorage.getItem('solar-lab-extra-metrics-v1') || '[]');
    return Array.isArray(saved) ? saved.filter(key => availableMetrics[key]) : [];
  } catch (_) { return []; }
}
let labMetrics = loadLabMetrics();

function saveLabMetrics() {
  localStorage.setItem('solar-lab-extra-metrics-v1', JSON.stringify(labMetrics));
}

function metricValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'number' ? value.toFixed(1).replace(/\.0$/, '') : String(value);
}

function renderLabCards() {
  extraMetricsGrid.replaceChildren();
  labMetrics.forEach(key => {
    const [label, unit] = availableMetrics[key];
    const card = document.createElement('article');
    card.className = 'extra-metric card';
    const labelNode = document.createElement('span');
    labelNode.textContent = label;
    const valueNode = document.createElement('strong');
    valueNode.textContent = metricValue(labSample[key]);
    const unitNode = document.createElement('small');
    unitNode.textContent = ` ${unit}`;
    valueNode.appendChild(unitNode);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${label}`);
    remove.addEventListener('click', () => {
      labMetrics = labMetrics.filter(item => item !== key);
      saveLabMetrics(); renderLabCards(); renderLabPicker();
    });
    card.append(labelNode, valueNode, remove);
    extraMetricsGrid.appendChild(card);
  });
}

function renderLabPicker() {
  metricPicker.replaceChildren();
  Object.entries(availableMetrics).forEach(([key, [label]]) => {
    const choice = document.createElement('label');
    choice.className = 'metric-choice';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = labMetrics.includes(key);
    checkbox.addEventListener('change', () => {
      labMetrics = checkbox.checked
        ? [...labMetrics.filter(item => item !== key), key]
        : labMetrics.filter(item => item !== key);
      saveLabMetrics(); renderLabCards();
    });
    choice.append(checkbox, document.createTextNode(label));
    metricPicker.appendChild(choice);
  });
}

document.addEventListener('solar:sample', event => {
  labSample = event.detail || {};
  renderLabCards();
});
document.querySelector('#customize-open').addEventListener('click', renderLabPicker);
document.querySelector('#clear-data-cards').addEventListener('click', () => {
  labMetrics = [];
  saveLabMetrics(); renderLabCards(); renderLabPicker();
});
renderLabPicker();
renderLabCards();
