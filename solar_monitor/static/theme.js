/* Colour scheme selection, shared by every page.
 *
 * Loaded synchronously in <head> so the first paint is already in the right
 * scheme; a page that flashed light before going dark would be worse than
 * either. The choice is stored per device: "light", "dark", or nothing, which
 * means follow the operating system.
 *
 * The stylesheets key everything off `data-theme` on <html>, so this is the
 * only place that decides which scheme is in force.
 */
(() => {
  const KEY = 'solar-theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const CANVAS = {light: '#f4f7f5', dark: '#0e1613'};

  function preference() {
    try {
      const saved = localStorage.getItem(KEY);
      return saved === 'light' || saved === 'dark' ? saved : 'system';
    } catch (_) {
      return 'system';
    }
  }

  const resolve = choice => (choice === 'system' ? (media.matches ? 'dark' : 'light') : choice);

  function apply() {
    const choice = preference();
    const theme = resolve(choice);
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.dataset.themePreference = choice;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = CANVAS[theme];
    document.dispatchEvent(new CustomEvent('themechange', {detail: {theme, preference: choice}}));
  }

  function set(choice) {
    try {
      if (choice === 'system') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch (_) { /* private mode: the choice still applies for this page */ }
    apply();
  }

  media.addEventListener('change', apply);
  apply();

  window.SolarTheme = {
    preference,
    current: () => resolve(preference()),
    set,
    toggle: () => set(resolve(preference()) === 'dark' ? 'light' : 'dark'),
  };
})();
