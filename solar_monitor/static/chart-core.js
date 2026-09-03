/* Shared canvas charting primitives.
 *
 * Both the dashboard's power history and the solar potential page draw onto
 * raw canvas, and each had grown its own axis maths, tick spacing, fonts and
 * colours. This module is the one implementation of those, so the two charts
 * read as the same product and a fix to either lands in both.
 *
 * Everything here is deliberately plain: no dependencies, no build step, and
 * no smoothing. It draws the numbers it is given.
 */
const ChartCore = (() => {
  const FONT_STACK = 'Inter, system-ui, -apple-system, sans-serif';

  /* Canvas sized for the device's pixel ratio, so text and hairlines stay
     sharp on a phone. Returns CSS-pixel dimensions; the context is already
     scaled, so all drawing code works in CSS pixels. */
  function setupCanvas(canvas, {ratio = window.devicePixelRatio || 1} = {}) {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return {ctx, width, height, ratio};
  }

  function cssValue(name, fallback) {
    const value = getComputedStyle(document.body).getPropertyValue(name).trim();
    return value || fallback;
  }

  /* An rgb/rgba colour with its alpha forced to zero, for gradient stops.
     Fading to a literal white breaks the moment the surface is not white. */
  function fadeOut(color) {
    const match = color.match(/rgba?\(([^)]+)\)/);
    if (match) {
      const [r, g, b] = match[1].split(',').map(part => part.trim());
      return `rgba(${r}, ${g}, ${b}, 0)`;
    }
    return 'rgba(0, 0, 0, 0)';
  }

  /* A round step at a 1/2/5 x 10^n cadence, so axis labels land on numbers a
     person would choose. */
  function niceStep(rough) {
    if (!(rough > 0)) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    const normalised = rough / magnitude;
    const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function valueTicks(min, max, targetCount = 5) {
    if (!(max > min)) return [min];
    const step = niceStep((max - min) / Math.max(1, targetCount));
    const ticks = [];
    for (let value = Math.ceil(min / step) * step; value <= max + step * 1e-6; value += step) {
      ticks.push(Math.abs(value) < step * 1e-6 ? 0 : value);
    }
    return ticks;
  }

  const MINUTE = 60000;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;
  const TIME_STEPS = [
    MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
    HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY,
  ];

  /* Ticks on natural clock boundaries -- the top of an hour, local midnight --
     rather than evenly slicing the window, which is what makes a time axis
     readable. `minSpacing` keeps labels from colliding on a narrow phone. */
  function timeTicks(from, to, pixelWidth, {minSpacing = 68} = {}) {
    const span = Math.max(1, to - from);
    const maxTicks = Math.max(2, Math.floor(pixelWidth / minSpacing));
    const step = TIME_STEPS.find(candidate => span / candidate <= maxTicks)
      || TIME_STEPS[TIME_STEPS.length - 1];
    const ticks = [];

    if (step >= DAY) {
      const cursor = new Date(from);
      cursor.setHours(0, 0, 0, 0);
      const days = Math.round(step / DAY);
      while (cursor.getTime() <= to) {
        if (cursor.getTime() >= from) ticks.push(cursor.getTime());
        cursor.setDate(cursor.getDate() + days);
      }
    } else {
      // Shift into "local epoch" so alignment lands on local clock boundaries
      // rather than UTC ones.
      const offset = new Date(from).getTimezoneOffset() * MINUTE;
      let cursor = Math.ceil((from - offset) / step) * step + offset;
      while (cursor <= to) {
        ticks.push(cursor);
        cursor += step;
      }
    }
    return ticks.map(time => ({time, ...labelFor(time, step)}));
  }

  function labelFor(time, step) {
    const date = new Date(time);
    const midnight = date.getHours() === 0 && date.getMinutes() === 0;
    if (step >= DAY || midnight) {
      return {
        label: date.toLocaleDateString([], {month: 'short', day: 'numeric'}),
        major: true,
      };
    }
    return {
      label: date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}),
      major: false,
    };
  }

  /* Compact axis numbers: 1500 -> "1.5k". */
  function compactNumber(value) {
    const magnitude = Math.abs(value);
    if (magnitude >= 1000) {
      const thousands = value / 1000;
      return `${Number(thousands.toFixed(magnitude % 1000 ? 1 : 0))}k`;
    }
    return String(Math.round(value));
  }

  /* Hairlines land on a half-pixel so they render as one crisp line rather
     than a two-pixel smear. */
  const crisp = position => Math.round(position) + 0.5;

  /* Straight segments through every point. No curve fitting: a smoothed line
     passes through interpolated midpoints instead of the readings, which
     visibly clips peaks and makes the chart disagree with the summary tiles. */
  function linePath(ctx, points) {
    if (!points.length) return;
    ctx.moveTo(points[0].x, points[0].y);
    for (let index = 1; index < points.length; index += 1) {
      ctx.lineTo(points[index].x, points[index].y);
    }
  }

  return {
    FONT_STACK, setupCanvas, cssValue, fadeOut,
    niceStep, valueTicks, timeTicks, compactNumber, crisp, linePath,
  };
})();
