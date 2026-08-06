// Poller injected via Page.addScriptToEvaluateOnNewDocument into the game frame
// BEFORE any game script runs. It pushes fake chunks onto the shared webpack
// chunk hook; every runtime that processes a chunk hands its require function
// to the callback. All captured requires are stored in window.__FMV_rt —
// the extraction step later picks the MAIN runtime (largest module map, owns
// the live farm services).
//
// Name-independent: works for any obfuscation run of the game build.
//
// Fake chunk ids (0x7ff00000 + n) are safe: real ids are small hex, and the
// handler skips runtime callbacks for already-loaded ids.

export const POLLER_SOURCE = `(function(){
  if (window.__FMV_poller) return;
  window.__FMV_poller = true;
  window.__FMV_rt = [];
  window.__FMV_captures = [];
  window.__FMV_err = null;
  const h = self['webpackChunkfarm_merge_game'] || (self['webpackChunkfarm_merge_game'] = []);
  let n = 0;
  const nameOf = (fn) => {
    const s = String(fn);
    const m = s.match(/function\s*([_\w$]+)/);
    return m ? m[1] : s.slice(0, 40);
  };
  const timer = setInterval(function(){
    try {
      h.push([[0x7ff00000 + (n++)], {}, function(r){
        const rec = { name: nameOf(r), at: Date.now() };
        try { rec.mcount = Object.keys(r.m || {}).length; } catch (e) {}
        window.__FMV_rt.push(r);
        window.__FMV_captures.push(rec);
      }]);
    } catch(e) { window.__FMV_err = String(e); }
  }, 100);
  window.__FMV_stop = function(){ clearInterval(timer); };
  setTimeout(function(){ clearInterval(timer); }, 60000);
})();`;
