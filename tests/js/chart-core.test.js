/* Executable tests for the shared charting maths.
 *
 * Run with `node --test tests/js/` (Node 18+). Deliberately dependency-free:
 * the runner and assertions are both built into Node, so the project still has
 * no node_modules, no lockfile and no build step. The Python suite shells out
 * to this and skips it when Node is absent.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

const ChartCore = require('../../solar_monitor/static/chart-core.js');

const MINUTE = 60000;
const HOUR = 60 * MINUTE;

test('niceStep lands on a 1/2/5 cadence', () => {
  assert.equal(ChartCore.niceStep(1), 1);
  assert.equal(ChartCore.niceStep(1.5), 2);
  assert.equal(ChartCore.niceStep(3), 5);
  assert.equal(ChartCore.niceStep(7), 10);
  assert.equal(ChartCore.niceStep(430), 500);
  assert.equal(ChartCore.niceStep(0), 1, 'degenerate input must not divide by zero');
  assert.equal(ChartCore.niceStep(-5), 1);
});

test('valueTicks produces round, evenly spaced, in-range values', () => {
  const ticks = ChartCore.valueTicks(0, 2000, 4);
  assert.deepEqual(ticks, [0, 500, 1000, 1500, 2000]);
  for (const tick of ticks) assert.ok(tick >= 0 && tick <= 2000);

  const gaps = new Set();
  ChartCore.valueTicks(24.5, 28.5, 4).forEach((value, index, all) => {
    if (index) gaps.add(Number((value - all[index - 1]).toFixed(6)));
  });
  assert.equal(gaps.size, 1, 'spacing must be uniform');
});

test('valueTicks survives a degenerate range', () => {
  assert.deepEqual(ChartCore.valueTicks(5, 5), [5]);
  assert.deepEqual(ChartCore.valueTicks(10, 0), [10]);
});

test('timeTicks lands on clock boundaries, not arbitrary offsets', () => {
  // A window starting at an awkward 14:37:23.
  const from = new Date(2026, 8, 4, 14, 37, 23).getTime();
  for (const hours of [1, 6, 24]) {
    const ticks = ChartCore.timeTicks(from, from + hours * HOUR, 320);
    assert.ok(ticks.length >= 2, `${hours}h window produced ${ticks.length} ticks`);
    for (const tick of ticks) {
      const date = new Date(tick.time);
      assert.equal(date.getSeconds(), 0, `${hours}h: tick has stray seconds`);
      assert.equal(date.getMinutes() % 5, 0, `${hours}h: tick is off a 5-minute boundary`);
      assert.ok(tick.time >= from, 'tick precedes the window');
      assert.ok(tick.time <= from + hours * HOUR, 'tick overruns the window');
    }
  }
});

test('timeTicks respects the space available', () => {
  const from = new Date(2026, 8, 4, 0, 0, 0).getTime();
  const narrow = ChartCore.timeTicks(from, from + 24 * HOUR, 320, {minSpacing: 68});
  const wide = ChartCore.timeTicks(from, from + 24 * HOUR, 1200, {minSpacing: 68});
  assert.ok(narrow.length <= Math.floor(320 / 68) + 1, 'too many labels for a phone');
  assert.ok(wide.length > narrow.length, 'a wider chart should show more detail');
});

test('timeTicks marks local midnight as a major tick and dates it', () => {
  const from = new Date(2026, 8, 3, 12, 0, 0).getTime();
  const ticks = ChartCore.timeTicks(from, from + 36 * HOUR, 900);
  const midnights = ticks.filter(tick => tick.major);
  assert.ok(midnights.length >= 1, 'a window spanning midnight must mark it');
  for (const tick of midnights) {
    const date = new Date(tick.time);
    assert.equal(date.getHours(), 0);
    assert.equal(date.getMinutes(), 0);
    assert.match(tick.label, /[A-Za-z]{3}/, 'a major tick should carry the date');
  }
});

test('timeTicks steps whole local days on multi-day windows', () => {
  const from = new Date(2026, 7, 5, 9, 30, 0).getTime();
  const ticks = ChartCore.timeTicks(from, from + 30 * 24 * HOUR, 360);
  assert.ok(ticks.length >= 2);
  for (const tick of ticks) {
    const date = new Date(tick.time);
    assert.equal(date.getHours(), 0, 'day ticks must sit at local midnight');
    assert.equal(date.getMinutes(), 0);
    assert.ok(tick.major);
  }
});

test('timeTicks terminates on a zero-width or inverted window', () => {
  const now = Date.now();
  assert.doesNotThrow(() => ChartCore.timeTicks(now, now, 320));
  assert.doesNotThrow(() => ChartCore.timeTicks(now, now - HOUR, 320));
  assert.deepEqual(ChartCore.timeTicks(now, now - HOUR, 320), []);
});

test('compactNumber abbreviates thousands without lying', () => {
  assert.equal(ChartCore.compactNumber(0), '0');
  assert.equal(ChartCore.compactNumber(750), '750');
  assert.equal(ChartCore.compactNumber(1000), '1k');
  assert.equal(ChartCore.compactNumber(1500), '1.5k');
  assert.equal(ChartCore.compactNumber(2640), '2.6k');
});

test('fadeOut keeps the colour and drops only the alpha', () => {
  assert.equal(ChartCore.fadeOut('rgba(201,120,10,.16)'), 'rgba(201, 120, 10, 0)');
  assert.equal(ChartCore.fadeOut('rgb(8, 125, 85)'), 'rgba(8, 125, 85, 0)');
  // A non-rgb colour has no channels to preserve; transparent black is the
  // safe fallback and still never paints white.
  assert.equal(ChartCore.fadeOut('#c9780a'), 'rgba(0, 0, 0, 0)');
});

test('crisp snaps hairlines to a half pixel', () => {
  assert.equal(ChartCore.crisp(10), 10.5);
  assert.equal(ChartCore.crisp(10.4), 10.5);
  assert.equal(ChartCore.crisp(10.6), 11.5);
});

test('linePath draws straight segments and never a curve', () => {
  // The regression this guards: series were drawn with quadraticCurveTo using
  // the readings as control points, so the rendered line passed through
  // midpoints and clipped peaks by up to 10%.
  const calls = [];
  const ctx = new Proxy({}, {
    get: (_target, name) => (...args) => calls.push([name, ...args]),
  });
  const points = [{x: 0, y: 10}, {x: 5, y: 0}, {x: 10, y: 10}];
  ChartCore.linePath(ctx, points);

  assert.deepEqual(calls[0], ['moveTo', 0, 10]);
  assert.deepEqual(calls.slice(1), [['lineTo', 5, 0], ['lineTo', 10, 10]]);
  assert.ok(!calls.some(([name]) => /curve|arc|bezier/i.test(name)),
    'no curve command may appear: the chart must plot the readings it was given');

  // Every input point must appear as a vertex, peaks included.
  const plotted = calls.map(([, x, y]) => `${x},${y}`);
  for (const point of points) assert.ok(plotted.includes(`${point.x},${point.y}`));
});

test('linePath handles empty and single-point series', () => {
  const calls = [];
  const ctx = new Proxy({}, {get: (_t, name) => (...args) => calls.push([name, ...args])});
  ChartCore.linePath(ctx, []);
  assert.equal(calls.length, 0);
  ChartCore.linePath(ctx, [{x: 1, y: 2}]);
  assert.deepEqual(calls, [['moveTo', 1, 2]]);
});
