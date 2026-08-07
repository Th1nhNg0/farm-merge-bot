// Pause protection for the Discord activity build — keeps the game running
// while the tab is in the background.
//
// The game freezes when the tab is hidden (document.visibilityState): its
// pageVisibility service pauses systems, and the bundled Pixi Ticker drives
// the loop via bare `requestAnimationFrame(...)` calls — which Chrome never
// fires for hidden tabs, so entity behavior queues stop draining.
//
// This patch (idempotent, safe to run at any time):
//   1. fakes document.visibilityState/hidden/hasFocus so the game never
//      believes it is in the background (its own pause logic never triggers)
//   2. swallows `visibilitychange` listeners, so it can't learn otherwise
//   3. bridges requestAnimationFrame with a timer watchdog: the callback is
//      invoked via setTimeout if the real rAF stalls (hidden tab) — the game
//      keeps ticking, and later re-runs pick up the bridge because the ticker
//      resolves `requestAnimationFrame` from the global scope at call time
//   4. late-repair: if FMV is already installed, flips the pageVisibility
//      service back to focused (fires onPageFocused so paused systems resume)
//
// Note: background tabs throttle setTimeout to ~1/s by default, so without
// Chrome flags (`--disable-background-timer-throttling`) the game runs at
// ~1 fps while hidden — enough for direct bot ops; the flags restore speed.

export const PAUSE_PROTECT_SOURCE = `(function(){
  if (window.__FMV_pauseProtect) return true;
  window.__FMV_pauseProtect = true;

  // 1) fake visibility state — the game never sees a hidden tab
  try {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, get: function () { return 'visible'; }
    });
    Object.defineProperty(document, 'hidden', {
      configurable: true, get: function () { return false; }
    });
  } catch (e) {}
  try { document.hasFocus = function () { return true; }; } catch (e) {}

  // 2) swallow visibilitychange listeners (document === window addEventListener)
  try {
    const docAdd = document.addEventListener.bind(document);
    const docRemove = document.removeEventListener.bind(document);
    const VIS = 'visibilitychange';
    document.addEventListener = function (type, fn, opts) {
      if (type === VIS) return undefined;
      return docAdd(type, fn, opts);
    };
    document.removeEventListener = function (type, fn, opts) {
      if (type === VIS) return undefined;
      return docRemove(type, fn, opts);
    };
  } catch (e) {}

  // 3) rAF bridge: real rAF first, timer watchdog fallback when it stalls
  try {
    const realRaf = window.requestAnimationFrame.bind(window);
    const realCaf = window.cancelAnimationFrame.bind(window);
    const FALLBACK_MS = 48;
    const pending = new Map();
    let nextId = 1;
    window.requestAnimationFrame = function (cb) {
      const id = nextId++;
      const entry = { timer: null, realId: null };
      let fired = false;
      const fire = function (ts) {
        if (fired) return;
        fired = true;
        if (entry.timer) clearTimeout(entry.timer);
        pending.delete(id);
        cb(ts);
      };
      entry.realId = realRaf(fire);
      entry.timer = setTimeout(function () { fire(performance.now()); }, FALLBACK_MS);
      pending.set(id, entry);
      return id;
    };
    window.cancelAnimationFrame = function (id) {
      const entry = pending.get(id);
      if (!entry) { realCaf(id); return; }
      if (entry.realId !== null) { try { realCaf(entry.realId); } catch (e) {} }
      if (entry.timer) clearTimeout(entry.timer);
      pending.delete(id);
    };
  } catch (e) {}

  // 4) late repair: if the game is already up, resume any paused systems
  try {
    const pv = window.FMV && window.FMV.rootServices && window.FMV.rootServices().pageVisibility;
    if (pv) {
      pv._hidden = false;
      if (pv.onPageFocused && typeof pv.onPageFocused.fire === 'function') pv.onPageFocused.fire();
    }
  } catch (e) {}

  return true;
})();`;
