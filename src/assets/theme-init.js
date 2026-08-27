/* Runs synchronously in <head> so the stored theme applies before first
   paint — no flash of the wrong theme. Storage access is guarded: on
   file:// or locked-down configurations it silently falls back to the
   defaults baked into the <html> element. */
(function () {
  'use strict';
  var root = document.documentElement;
  var theme = null;
  var density = null;
  try {
    theme = window.localStorage.getItem('ai-gallery:theme');
    density = window.localStorage.getItem('ai-gallery:density');
  } catch (err) { /* storage unavailable: keep defaults */ }
  if (theme === 'light' || theme === 'dark' || theme === 'geocities' || theme === 'system') {
    root.setAttribute('data-theme', theme);
  }
  if (density === 'comfortable' || density === 'default' || density === 'compact') {
    root.setAttribute('data-density', density);
  }
})();
