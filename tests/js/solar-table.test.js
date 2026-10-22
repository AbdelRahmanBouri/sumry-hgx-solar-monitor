const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../../solar_monitor/static/estimate.js'), 'utf8');
function tableContext(state) {
  const bodies = {};
  const context = vm.createContext({
    state,
    timeOf: point => new Date(point.timestamp).getTime(),
    interpolate: () => 412.4,
    document: {
      querySelector: id => bodies[id] ||= {table: {}, closest() { return this.table; }, replaceChildren(...rows) { this.rows = rows; }},
      createElement: () => ({cells: [], append(cell) { this.cells.push(cell); }}),
    },
  });
  vm.runInContext(source.slice(source.indexOf('function renderSolarTables()')), context);
  return {context, bodies};
}

test('solar tables label forecasts and never present interpolated future output as captured power', () => {
  const {context, bodies} = tableContext({history: [], chartRows: [
    {timestamp: new Date(Date.now() - 60000).toISOString(), estimated_watts: 500.2},
    {timestamp: new Date(Date.now() + 3600000).toISOString(), estimated_watts: 600.1},
  ], hourlyOpportunity: [{timestamp: new Date().toISOString(), high: 0.125, low: 0.25}]});
  context.renderSolarTables();
  const rows = bodies['#potential-data'].rows.map(row => row.cells.map(cell => cell.textContent));
  assert.deepEqual(rows[0].slice(1), ['412', '500', 'Past estimate']);
  assert.deepEqual(rows[1].slice(1), ['Unavailable', '600', 'Forecast']);
  assert.deepEqual(bodies['#opportunity-data'].rows[0].cells.slice(1).map(cell => cell.textContent), ['0.125', '0.250']);
});

test('empty refreshed data clears previous table rows', () => {
  const state = {history: [], chartRows: [{timestamp: new Date().toISOString(), estimated_watts: 1}], hourlyOpportunity: []};
  const {context, bodies} = tableContext(state);
  context.renderSolarTables();
  assert.equal(bodies['#potential-data'].rows.length, 1);
  state.chartRows = [];
  context.renderSolarTables();
  assert.equal(bodies['#potential-data'].rows.length, 0);
  assert.equal(bodies['#potential-data'].table.hidden, true);
});
