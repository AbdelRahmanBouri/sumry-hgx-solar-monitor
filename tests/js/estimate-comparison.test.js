const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the page's pure comparison function without starting its DOM loop.
const source = fs.readFileSync(path.join(__dirname, '../../solar_monitor/static/estimate.js'), 'utf8');
const context = vm.createContext({
  startOfToday: () => 1000,
  timeOf: row => row.at,
});
vm.runInContext(source.slice(source.indexOf('const number ='), source.indexOf('const clamp =')), context);
vm.runInContext(source.slice(source.indexOf('function modelAccuracy('), source.indexOf('function renderAccuracy(')), context);
const compare = rows => context.modelAccuracy(rows);
const reading = watts => ({at: 2000, battery_voltage: 25, potential_watts: 1000, pv_watts: watts});

test('opposing errors do not cancel into a perfect accuracy claim', () => {
  const rows = Array.from({length: 12}, (_, i) => reading(i % 2 ? 1500 : 500));
  assert.equal(compare(rows).absolutePercent, 50);
  assert.equal(compare(rows).samples, 12);
});

test('comparison uses modelled output as its stated denominator', () => {
  assert.equal(compare(Array.from({length: 12}, () => reading(800))).absolutePercent.toFixed(1), '20.0');
});

test('missing, old and high-voltage readings cannot support the comparison', () => {
  const rows = Array.from({length: 11}, () => reading(1000));
  rows.push({...reading(1000), at: 0}, {...reading(1000), battery_voltage: 27.2}, reading(null), reading(NaN));
  assert.equal(compare(rows), null);
});
