// In-frame webpack module hunter (used by install_fmv.mjs).
// Re-discovers, for the CURRENT build: main runtime require, root container,
// farm services, component map (I) and the MergeTrigger constructor.
//
// This build mangles most string literals AND some property names, so discovery
// is done structurally over executed module exports (side-effect-free):
//   1. the runtime with the largest module map is tried first (only others if
//      it fails) — keeps the main-thread work minimal (Discord's activity
//      watchdog restarts the frame on long stalls)
//   2. executed modules are enumerated by temporarily stubbing factories
//      (safe: no factory re-execution) IN BATCHES with event-loop breathing
//   3. root container: an executed export subtree containing a services
//      collection ('.services' or '._nonCriticalServices') with timer
//      (_updatableGroup._members), inventory (getAmount) and hudServiceRegistry
//      (_activeService)
//   4. farm services: first timer member's _services with .mapGrid
//   5. component map: export with .Mergeable and .GridPosition keys
//   6. MergeTrigger ctor: function export whose instance has .cell and .chain
// Result is stored in window.__FMV_*.

export const HUNTER_SOURCE = `(async function(){
  const out = { ok: false };
  const rt = window.__FMV_rt || [];
  if (!rt.length) return Object.assign(out, { reason: 'no runtime captured — run install_poller.mjs first' });

  const seen = new Set();
  const cands = [];
  for (const r of rt) {
    try {
      if (seen.has(r)) continue;
      seen.add(r);
      let n = -1;
      try { n = Object.keys(r.m || {}).length; } catch (e) {}
      cands.push({ r, n });
    } catch (e) {}
  }
  if (!cands.length) return Object.assign(out, { reason: 'no runtimes captured' });
  cands.sort((a, b) => b.n - a.n);

  const breathe = () => new Promise((res) => setTimeout(res, 0));
  const isSvcColl = (o) => o && typeof o === 'object' &&
    o.timer && o.timer._updatableGroup && Array.isArray(o.timer._updatableGroup._members) &&
    o.inventory && typeof o.inventory.getAmount === 'function' &&
    o.hudServiceRegistry && o.hudServiceRegistry._activeService;

  const attempts = [];
  let best = null, bestScore = -Infinity;

  for (const { r: req, n } of cands) {
    // enumerate executed modules in batches (stub factories -> no re-execution)
    const orig = {};
    const ids = [];
    try {
      for (const k of Object.keys(req.m || {})) {
        const id = Number(k);
        if (!Number.isFinite(id)) continue;
        ids.push(id);
        const f = req.m[id];
        if (typeof f !== 'function') continue;
        orig[id] = f;
        req.m[id] = function(){ throw new Error('FMV_STUB_' + id); };
      }
    } catch (e) {}
    const executed = [];
    for (let i = 0; i < ids.length; i += 200) {
      const end = Math.min(i + 200, ids.length);
      for (let j = i; j < end; j++) {
        const id = ids[j];
        try { req(id); executed.push(id); } catch (e) {
          if (!String(e.message).startsWith('FMV_STUB_')) executed.push(id);
        }
      }
      await breathe();
    }
    for (const id of Object.keys(orig)) req.m[id] = orig[id];

    // ---- root container: walk executed exports ----
    let root = null, rootId = null, rootPath = null, servicesKey = null;
    let farm = null, mapId = null, mapKey = null, hc = null, hcId = null, hcKey = null;
    let boardSize = -1;

    const visited = new Set();
    // path = key path from the module export to the container object. The
    // full path (not just the first key) is kept so root() resolves correctly
    // even when the services collection sits deeper than depth 1.
    const walk = (obj, path, depth, srcId) => {
      if (depth > 3 || !obj || typeof obj !== 'object' || visited.size > 8000) return;
      if (visited.has(obj)) return;
      visited.add(obj);
      if (Array.isArray(obj)) {
        for (const e of obj) walk(e, path, depth + 1, srcId);
        return;
      }
      if (!root) {
        for (const sk of ['_nonCriticalServices', 'services']) {
          const c = obj[sk];
          if (c && typeof c === 'object' && isSvcColl(c)) {
            root = obj; rootId = srcId; servicesKey = sk;
            rootPath = path.length ? path : null;
            return;
          }
        }
      }
      const keys = Object.keys(obj).slice(0, 40);
      for (const k of keys) {
        const v = obj[k];
        if (v && typeof v === 'object') walk(v, path.concat(k), depth + 1, srcId);
      }
    };

    for (let i = 0; i < executed.length && !root; i += 150) {
      const end = Math.min(i + 150, executed.length);
      for (let j = i; j < end; j++) {
        try {
          const ex = req(executed[j]);
          if (!ex || typeof ex !== 'object') continue;
          walk(ex, [], 0, executed[j]);
          if (root) break;
        } catch (e) {}
      }
      await breathe();
    }

    if (root) {
      try {
        const members = root[servicesKey].timer._updatableGroup._members;
        for (const m of members) {
          if (m && m._services && m._services.mapGrid) { farm = m._services; break; }
        }
      } catch (e) {}
      if (farm) { try { boardSize = farm.mapGrid._cells.size; } catch (e) {} }
    }

    // ---- component map + MergeTrigger ctor: one fused scan ----
    // (single sync pass, like the original two scans — no added breathing,
    //  so real install wall time is unchanged)
    for (let i = 0; i < executed.length && (mapId === null || hc === null); i++) {
      try {
        const ex = req(executed[i]);
        if (!ex || typeof ex !== 'object') continue;
        if (mapId === null) {
          const cand = (ex.I !== undefined) ? ex.I : ex;
          if (cand && typeof cand === 'object' &&
              cand.Mergeable !== undefined && cand.GridPosition !== undefined) {
            mapId = executed[i]; mapKey = (ex.I !== undefined) ? 'I' : null;
          }
        }
        if (hc === null) {
          for (const k of Object.keys(ex)) {
            const v = ex[k];
            if (typeof v !== 'function' || !v.prototype) continue;
            try {
              const p = new v({ cell: { column: 1, row: 1 }, chain: [] });
              if (p && typeof p === 'object' && p.cell && p.chain) {
                hc = v; hcId = executed[i]; hcKey = k;
                break;
              }
            } catch (e) {}
          }
        }
      } catch (e) {}
    }

    const score = (farm ? 1000 : 0) + Math.max(0, Math.min(999, boardSize)) +
                  (hc ? 100 : 0) + (mapId !== null ? 10 : 0);
    attempts.push({
      name: (() => { const m = String(req).match(/function\\s*([_\\w$]+)/); return m ? m[1] : '?'; })(),
      executed: executed.length, score, rootId, rootPath, mapId, hcId, boardSize
    });
    if (score > bestScore) {
      bestScore = score;
      best = { req, root, rootId, rootPath, servicesKey, farm, mapId, mapKey, hc, hcId, hcKey, boardSize };
    }
    if (score > 1000) break; // live farm found — enough
  }

  if (!best) return Object.assign(out, { reason: 'no discoveries' });
  out.reqName = (() => { const m = String(best.req).match(/function\\s*([_\\w$]+)/); return m ? m[1] : null; })();
  out.rootId = best.rootId; out.rootPath = best.rootPath; out.servicesKey = best.servicesKey;
  out.mapId = best.mapId; out.mapKey = best.mapKey;
  out.hcId = best.hcId; out.hcKey = best.hcKey;
  out.boardSize = best.boardSize;
  out.farm = !!best.farm;
  out.attempts = attempts;

  if (best.root === null)  out.errors = (out.errors || []).concat('root container not found');
  if (best.farm === null)  out.errors = (out.errors || []).concat('farm services not ready');
  if (best.mapId === null) out.errors = (out.errors || []).concat('component map not found');
  if (best.hc === null)    out.errors = (out.errors || []).concat('MergeTrigger ctor not found');
  out.ok = (best.root !== null && best.farm !== null && best.mapId !== null && best.hc !== null);

  if (out.ok) {
    window.__FMV_req = best.req;
    window.__FMV_rootId = best.rootId;
    window.__FMV_rootPath = best.rootPath;
    // clear the legacy single-key form: fmv_helper's fallback must never
    // resolve against a stale key left by a previous-session install
    try { delete window.__FMV_rootKey; } catch (e) {}
    window.__FMV_servicesKey = best.servicesKey;
    window.__FMV_mapId = best.mapId;
    window.__FMV_mapKey = best.mapKey;
    window.__FMV_hcId = best.hcId;
    window.__FMV_hcKey = best.hcKey;
  }
  return out;
})();`;
