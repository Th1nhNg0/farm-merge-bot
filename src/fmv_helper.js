// window.FMV helper (v4, build-agnostic) installed into the game frame.
// Reads the discovered module ids/layout from window.__FMV_* (set by hunter).
// Works with the Discord-embedded build (services under _nonCriticalServices).
//   FMV.board()                        -> [{col, row, id, tier, mergeable}, ...]
//   FMV.merge(fromCol, fromRow, toCol, toRow) -> {ok, reason|chainLen, total}
//   FMV.remove(col, row)               -> shovel-style removal (shovelable only)
//   FMV.move / FMV.swap / FMV.spawnCrate / FMV.services()
//   FMV.req, FMV.I, FMV.mergeCtor, FMV.root(), FMV.rootServices()

export const FMV_HELPER_SOURCE = `(function(){
  const req = window.__FMV_req;
  const rootPath = window.__FMV_rootPath;
  const servicesKey = window.__FMV_servicesKey;
  // resolve the container by its full export key path (hunter v1.7.3+);
  // fall back to the legacy single-key form for older hunter payloads
  const root = () => {
    const base = req(window.__FMV_rootId);
    if (rootPath && rootPath.length) {
      let o = base;
      for (const k of rootPath) o = o[k];
      return o;
    }
    const k = window.__FMV_rootKey;
    return k ? base[k] : base;
  };
  const rootServices = () => root()[servicesKey];
  const I = () => window.__FMV_mapKey === 'I' ? req(window.__FMV_mapId).I : req(window.__FMV_mapId);
  const MergeTriggerCtor = () => req(window.__FMV_hcId)[window.__FMV_hcKey];

  // objectRemoval behavior ctor: function export of the trigger module whose
  // instance .type === 'objectRemoval' (same structural scan as lootReceived)
  let _objectRemovalCtor = null;
  function ObjectRemovalCtor() {
    if (_objectRemovalCtor) return _objectRemovalCtor;
    try {
      const mod = req(window.__FMV_hcId);
      for (const k of Object.keys(mod)) {
        const v = mod[k];
        if (typeof v !== 'function' || !v.prototype) continue;
        try {
          const inst = new v({});
          if (inst && inst.type === 'objectRemoval') { _objectRemovalCtor = v; return v; }
        } catch (e) {}
      }
    } catch (e) {}
    return null;
  }

  function services() {
    try {
      const members = rootServices().timer._updatableGroup._members;
      for (const m of members) {
        if (m && m._services && m._services.mapGrid) return m._services;
      }
    } catch (e) {}
    return null;
  }

  function board() {
    const S = services();
    if (!S) return { error: 'services not ready' };
    const out = [];
    try {
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell || !cell.content) continue;
        const c = cell.content;
        let info = null;
        try { info = c.getObjectIdAndTier ? c.getObjectIdAndTier() : null; } catch (e) {}
        out.push({
          col: cell.column, row: cell.row,
          id: info ? info.id : (c.getBlueprintID ? c.getBlueprintID() : null),
          tier: info ? info.tier : null,
          mergeable: c.hasBehavior ? c.hasBehavior(I().Mergeable) : false
        });
      }
    } catch (e) { return { error: 'board scan failed: ' + e.message }; }
    return out;
  }

  // size = planned chain size (5/10/15) from the planner; caps the live flood
  // fill so a natural chunk cannot swallow a neighbour group's items.
  function merge(fromCol, fromRow, toCol, toRow, size) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const from = S.mapGrid.getCell(fromCol, fromRow);
    const to = S.mapGrid.getCell(toCol, toRow);
    if (!from || !to) return { ok: false, reason: 'no such cell' };
    if (!from.content || !to.content) return { ok: false, reason: 'empty cell', fromEmpty: !from.content, toEmpty: !to.content };
    if (from.content === to.content) return { ok: false, reason: 'same object' };
    const mergeable = to.content.getBehavior(I().Mergeable);
    if (!mergeable) return { ok: false, reason: 'target not mergeable' };
    const spec = mergeable.targetSpecification;
    const cap = Number.isFinite(size) && size > 0 ? size : undefined;
    let chain;
    try {
      // flood fill INCLUDES the start cell; the game's own call passes undefined
      // as 3rd arg (it is a max-length cap, not an exclusion list). The source
      // cell must be filtered out because the game would have it empty mid-drag.
      chain = S.gridFilter.getAdjacentObjectsWithSameID(to, spec, cap, [I().Mergeable]);
      // Item-loss guards: the trigger merges chain + the removed source, so the
      // source MUST be part of the flood fill (same id, adjacent-connected to
      // the target) AND the total must be a legal merge size — the game only
      // fires 5/10/15 merges, so an off-multiple live chain (6/7/11/16…, e.g.
      // after a failed move/swap in the batch) would destroy the source for
      // nothing. Skip instead: the next round re-plans.
      if (chain.indexOf(from) === -1)
        return { ok: false, reason: 'source not adjacent to target — board changed, retry', chainLen: chain.length };
      chain = chain.filter(c => c !== from);
      const total = chain.length + 1;
      if (total % 5 !== 0)
        return { ok: false, reason: 'merge size ' + total + ' not 5/10/15 — board changed, retry', chainLen: chain.length };
      const Trigger = MergeTriggerCtor();
      if (typeof Trigger !== 'function') return { ok: false, reason: 'merge trigger ctor not found' };
      let trigger;
      try { trigger = new Trigger({ cell: to.position, chain }); }
      catch (e) { return { ok: false, reason: 'merge trigger failed: ' + e.message }; }
      const source = from.content;
      let removed = false;
      try {
        S.world.removeGameObject(source);
        removed = true;
        to.content.addBehavior(trigger);
        return { ok: true, chainLen: chain.length, total };
      } catch (e) {
        if (removed) {
          try { S.world.addGameObject(source); } catch (e2) {}
          try { S.mapGrid.setContent(fromCol, fromRow, source); } catch (e2) {}
        }
        return { ok: false, reason: 'merge call failed: ' + e.message };
      }
    } catch (e) { return { ok: false, reason: 'merge call failed: ' + e.message }; }
  }

  function spawnCrate(col, row) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const cell = S.mapGrid.getCell(col, row);
    if (!cell) return { ok: false, reason: 'no such cell' };
    if (cell.content) return { ok: false, reason: 'cell not empty' };
    let crateEv = null;
    try {
      crateEv = rootServices().hudServiceRegistry._activeService._commonEvents.spawnCrates;
    } catch (e) { return { ok: false, reason: 'spawnCrates event not found: ' + e.message }; }
    try {
      crateEv.fire({ position: { column: col, row: row } });
    } catch (e) { return { ok: false, reason: 'fire failed: ' + e.message }; }
    return { ok: true, cratesLeft: rootServices().inventory.getAmount('crates') };
  }

  function remove(col, row) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const cell = S.mapGrid.getCell(col, row);
    if (!cell) return { ok: false, reason: 'no such cell' };
    if (!cell.content) return { ok: false, reason: 'empty cell' };
    const entity = cell.content;
    if (!entity.hasBehavior || !entity.hasBehavior(I().Shovelable))
      return { ok: false, reason: 'not removable (no shovelable behavior)' };
    const Ctor = ObjectRemovalCtor();
    if (!Ctor) return { ok: false, reason: 'objectRemoval ctor not found' };
    try {
      entity.addBehavior(new Ctor({}));
    } catch (e) { return { ok: false, reason: 'remove failed: ' + e.message }; }
    return { ok: true, removed: entity.getBlueprintID() };
  }

  function move(fromCol, fromRow, toCol, toRow) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const from = S.mapGrid.getCell(fromCol, fromRow);
    const to = S.mapGrid.getCell(toCol, toRow);
    if (!from || !to) return { ok: false, reason: 'no such cell' };
    if (!from.content) return { ok: false, reason: 'source cell empty' };
    if (to.content) return { ok: false, reason: 'target cell not empty' };
    const entity = from.content;
    const gp = entity.getBehavior(I().GridPosition);
    if (!gp) return { ok: false, reason: 'source has no GridPosition' };
    if (!entity.position || typeof entity.position.copyFrom !== 'function')
      return { ok: false, reason: 'source has no position' };
    const oldGp = {
      column: gp.column, row: gp.row,
      data: gp._data ? { column: gp._data.column, row: gp._data.row } : null
    };
    const oldPos = { x: entity.position.x, y: entity.position.y, z: entity.position.z };
    // compute everything fallible BEFORE mutating the grid — a failure here
    // must not leave the board half-changed (desync + false ok:false)
    let worldPos;
    try { worldPos = S.axonometricProjection.getWorldPosition(toCol, toRow); }
    catch (e) { return { ok: false, reason: 'position calc failed: ' + e.message }; }
    try {
      S.mapGrid.setContent(fromCol, fromRow, null);
      S.mapGrid.setContent(toCol, toRow, entity);
      gp.column = toCol; gp.row = toRow;
      if (gp._data) { gp._data.column = toCol; gp._data.row = toRow; }
      entity.position.copyFrom(worldPos);
    } catch (e) {
      try { S.mapGrid.setContent(toCol, toRow, null); } catch (e2) {}
      try { S.mapGrid.setContent(fromCol, fromRow, entity); } catch (e2) {}
      try {
        gp.column = oldGp.column; gp.row = oldGp.row;
        if (oldGp.data && gp._data) {
          gp._data.column = oldGp.data.column; gp._data.row = oldGp.data.row;
        }
      } catch (e2) {}
      try { entity.position.copyFrom(oldPos); }
      catch (e2) {
        try {
          entity.position.x = oldPos.x; entity.position.y = oldPos.y; entity.position.z = oldPos.z;
        } catch (e3) {}
      }
      return { ok: false, reason: 'move failed: ' + e.message };
    }
    return { ok: true, moved: entity.getBlueprintID() };
  }

  function swap(aCol, aRow, bCol, bRow) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const A = S.mapGrid.getCell(aCol, aRow);
    const B = S.mapGrid.getCell(bCol, bRow);
    if (!A || !B) return { ok: false, reason: 'no such cell' };
    if (!A.content || !B.content) return { ok: false, reason: 'one cell empty — use move()' };
    const ea = A.content, eb = B.content;
    if (ea === eb) return { ok: false, reason: 'same object' };
    const gpa = ea.getBehavior(I().GridPosition);
    const gpb = eb.getBehavior(I().GridPosition);
    if (!gpa || !gpb) return { ok: false, reason: 'missing GridPosition' };
    if (!ea.position || typeof ea.position.copyFrom !== 'function' ||
        !eb.position || typeof eb.position.copyFrom !== 'function')
      return { ok: false, reason: 'object position unavailable' };
    const oldA = {
      column: gpa.column, row: gpa.row,
      data: gpa._data ? { column: gpa._data.column, row: gpa._data.row } : null,
      pos: { x: ea.position.x, y: ea.position.y, z: ea.position.z }
    };
    const oldB = {
      column: gpb.column, row: gpb.row,
      data: gpb._data ? { column: gpb._data.column, row: gpb._data.row } : null,
      pos: { x: eb.position.x, y: eb.position.y, z: eb.position.z }
    };
    let wa, wb;
    try {
      wa = S.axonometricProjection.getWorldPosition(bCol, bRow);
      wb = S.axonometricProjection.getWorldPosition(aCol, aRow);
    } catch (e) { return { ok: false, reason: 'position calc failed: ' + e.message }; }
    try {
      S.mapGrid.setContent(aCol, aRow, eb);
      S.mapGrid.setContent(bCol, bRow, ea);
      gpa.column = bCol; gpa.row = bRow;
      if (gpa._data) { gpa._data.column = bCol; gpa._data.row = bRow; }
      gpb.column = aCol; gpb.row = aRow;
      if (gpb._data) { gpb._data.column = aCol; gpb._data.row = aRow; }
      ea.position.copyFrom(wa);
      eb.position.copyFrom(wb);
    } catch (e) {
      try { S.mapGrid.setContent(aCol, aRow, ea); } catch (e2) {}
      try { S.mapGrid.setContent(bCol, bRow, eb); } catch (e2) {}
      try {
        gpa.column = oldA.column; gpa.row = oldA.row;
        if (oldA.data && gpa._data) {
          gpa._data.column = oldA.data.column; gpa._data.row = oldA.data.row;
        }
        gpb.column = oldB.column; gpb.row = oldB.row;
        if (oldB.data && gpb._data) {
          gpb._data.column = oldB.data.column; gpb._data.row = oldB.data.row;
        }
      } catch (e2) {}
      try { ea.position.copyFrom(oldA.pos); }
      catch (e2) { try { ea.position.x = oldA.pos.x; ea.position.y = oldA.pos.y; ea.position.z = oldA.pos.z; } catch (e3) {} }
      try { eb.position.copyFrom(oldB.pos); }
      catch (e2) { try { eb.position.x = oldB.pos.x; eb.position.y = oldB.pos.y; eb.position.z = oldB.pos.z; } catch (e3) {} }
      return { ok: false, reason: 'swap failed: ' + e.message };
    }
    return { ok: true, moved: [ea.getBlueprintID(), eb.getBlueprintID()] };
  }

  // ── exploit helpers ───────────────────────────────────────────────────────
  // Verified against the live build: the game's reward pipeline
  // (rewardService._parseAndClaimRewards) grants inventory currency and
  // autosave.forceSave()s the result — the backend is client-authoritative
  // for inventory, so grants persist. Object rewards become storage bubbles
  // (world entities) that the game collects via the storageBubbleTap family.
  const VALID_INV_KEYS = ['coins', 'gems', 'energy', 'crates', 'wood', 'stone'];

  // CRATE caveat (verified): crate blueprints must NEVER go through the
  // bubble path — the tap handler spawns them into the world but the
  // moveContentToCell placement never completes for crates, so they
  // pile up as broken world objects (40+ of them froze the game loop).
  // Crates are placed DIRECTLY into a cell instead: factory object +
  // GridPosition behavior + mapGrid.setContent (the same machinery the
  // move/swap ops use), which the game saves and processes normally.
  let gridPosCtor = null;
  function placeCrate(blueprint, col, row) {
    const S = services();
    const I = window.FMV.I();
    const G = window.FMV.rootServices().gameObjectFactory;
    if (!S || !G) return { ok: false, reason: 'services not ready' };
    const cell = S.mapGrid.getCell(col, row);
    if (!cell) return { ok: false, reason: 'no such cell' };
    if (cell.content) return { ok: false, reason: 'cell not empty' };
    let obj = null;
    try { obj = G.createFromSerializedData({ data: {}, blueprint: blueprint }); } catch (e) {}
    if (!obj) return { ok: false, reason: 'create failed for ' + blueprint };
    let worldAdded = false;
    let gridTouched = false;
    let gp = null;
    try {
      gp = obj.getBehavior && obj.getBehavior(I.GridPosition);
      if (!gp && !gridPosCtor) {
        for (const c of S.mapGrid._cells.values()) {
          if (!c || !c.content) continue;
          const existing = c.content.getBehavior && c.content.getBehavior(I.GridPosition);
          if (existing && existing.constructor) { gridPosCtor = existing.constructor; break; }
        }
      }
      if (!gp && typeof gridPosCtor === 'function') {
        obj.addBehavior(new gridPosCtor({ column: col, row: row }));
        gp = obj.getBehavior && obj.getBehavior(I.GridPosition);
      }
      if (!gp) return { ok: false, reason: 'GridPosition ctor not found' };
      if (!obj.position || typeof obj.position.copyFrom !== 'function')
        return { ok: false, reason: 'object position unavailable' };
      S.world.addGameObject(obj);
      worldAdded = true;
      gridTouched = true;
      S.mapGrid.setContent(col, row, obj);
      if (!gp._data) gp._data = {};
      gp.column = col; gp.row = row;
      gp._data.column = col; gp._data.row = row;
      obj.position.copyFrom(S.axonometricProjection.getWorldPosition(col, row));
      return { ok: true };
    } catch (e) {
      if (gridTouched) { try { S.mapGrid.setContent(col, row, null); } catch (e2) {} }
      if (worldAdded) { try { S.world.removeGameObject(obj); } catch (e2) {} }
      return { ok: false, reason: 'place failed: ' + (e && e.message) };
    }
  }
  const isCrateBp = (bp) => typeof bp === 'string' && bp.indexOf('reward_crate') === 0;

  // grant inventory currency: [{key, amount}, ...] -> autosaved
  async function grant(rewards) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const R = S.rewardService;
    if (!R || typeof R._parseAndClaimRewards !== 'function')
      return { ok: false, reason: 'rewardService not found' };
    const list = [];
    for (const r of rewards || []) {
      if (!r || r.key === undefined || r.amount === undefined) continue;
      if (VALID_INV_KEYS.indexOf(r.key) === -1)
        return { ok: false, reason: 'invalid key ' + r.key + ' (valid: ' + VALID_INV_KEYS.join(',') + ')' };
      list.push({ key: r.key, amount: Math.max(0, Math.floor(Number(r.amount) || 0)) });
    }
    if (!list.length) return { ok: false, reason: 'no rewards' };
    try {
      await R._parseAndClaimRewards(list, null, null);
      return { ok: true, granted: list };
    } catch (e) {
      return { ok: true, granted: list, note: 'strategy error (grant still applied): ' + (e && e.message) };
    }
  }

  // spawn blueprint objects. Crates are placed DIRECTLY into empty cells;
  // other blueprints become storage bubbles (collect via collectBubbles).
  function spawn(blueprints) {
    const S = services();
    if (!S) return { ok: false, reason: 'services not ready' };
    const R = S.rewardService;
    const bc = window.FMV.rootServices().blueprintCollection;
    if (!R || typeof R._claimObjectRewards !== 'function')
      return { ok: false, reason: 'rewardService not found' };
    const bubbles = [];
    const placed = [];
    let unplacedCrates = 0;
    for (const b of blueprints || []) {
      if (!b || typeof b.key !== 'string') continue;
      let has = false;
      try { has = bc.hasBlueprint(b.key); } catch (e) {}
      if (!has) return { ok: false, reason: 'not a blueprint: ' + b.key };
      const amount = Math.max(1, Math.floor(Number(b.amount) || 1));
      if (isCrateBp(b.key)) {
        // direct placement: fill as many empty cells as we can
        let n = 0;
        for (const cell of S.mapGrid._cells.values()) {
          if (n >= amount) break;
          if (!cell || cell.content) continue;
          const r = placeCrate(b.key, cell.column, cell.row);
          if (r.ok) n++;
        }
        placed.push({ key: b.key, amount: n });
        if (n < amount) unplacedCrates += amount - n;
      } else {
        bubbles.push({ key: b.key, amount: amount });
      }
    }
    let spawnedBubbles = 0;
    if (bubbles.length) {
      try { R._claimObjectRewards(bubbles); spawnedBubbles = bubbles.length; } catch (e) {
        return { ok: false, reason: 'spawn failed: ' + (e && e.message) };
      }
    }
    const out = { ok: true, placed: placed, bubbles: spawnedBubbles };
    if (unplacedCrates) out.note = 'no empty cells for ' + unplacedCrates + ' crate(s) — re-run after clearing space';
    return out;
  }

  // collect storage bubbles. Crates are SALVAGED (direct-placed into empty
  // cells, then the bubble's content is emptied — the bubble tap path cannot
  // place crates); everything else goes through the game's own tap handler
  // (_onStorageBubbleTapped — tapRouter._simulateClick does NOT work on
  // bubbles: they have no valid GridPosition, the router rejects them). The
  // spawn animation is slow (~10s+ in hidden tabs), so we iterate settle
  // rounds and never tap a bubble twice (double-tap spawns duplicates). The
  // bubble's own pop trigger destroys it later — do NOT call
  // _initiateBubblePop directly (its async destroy crashed the game loop).
  let bubbleTapCtx = null;
  let bubbleTapCtxSvc = null;
  const tappedBubbles = new Map(); // entity -> tap timestamp (cross-call guard)
  const TAP_LAG_MS = 90000;
  function findBubbleTapCtx() {
    const S = services();
    if (!S) return false;
    // behavior-family registries are rebuilt as subsystems spawn/die or the
    // farm changes (friend visits swap the farm services) — a cached context
    // is only valid for the services identity it was found on.
    if (bubbleTapCtx && bubbleTapCtxSvc === S) return true;
    bubbleTapCtx = null;
    // walk the shared onBehaviorAdded registries (no FMVUtil dependency —
    // the helper installs before the menu prepends util.js)
    let ev = null;
    try {
      for (const cell of S.mapGrid._cells.values()) {
        if (cell && cell.content && cell.content.onBehaviorAdded) { ev = cell.content.onBehaviorAdded; break; }
      }
    } catch (e) {}
    if (!ev) {
      try {
        for (const e of (S.world._gameObjects || [])) {
          if (e.onBehaviorAdded) { ev = e.onBehaviorAdded; break; }
        }
      } catch (e2) {}
    }
    if (!ev || !ev._subscribers) return false;
    for (let i = 0; i < ev._subscribers.length; i++) {
      const reg = ev._subscribers[i].context;
      if (!reg || !reg.onGameObjectAdded || !reg._filter) continue;
      let types = null;
      try { types = reg._filter._behaviorTypes; } catch (e) {}
      if (!types || !Array.isArray(types)) continue;
      let sub = null;
      try { sub = reg.onGameObjectAdded._subscribers[0].context; } catch (e) { continue; }
      if (!sub) continue;
      if (types.indexOf('storageBubbleTap') !== -1 && typeof sub._onStorageBubbleTapped === 'function') {
        bubbleTapCtx = sub;
        bubbleTapCtxSvc = S;
        return true;
      }
    }
    return false;
  }
  async function collectBubbles() {
    const S = services();
    const I = window.FMV.I();
    if (!S) return { ok: false, reason: 'services not ready' };
    const hasTap = findBubbleTapCtx();
    const scan = () => {
      const out = [];
      for (const e of (S.world._gameObjects || [])) {
        try {
          if (!e.hasBehavior || !e.hasBehavior(I.StorageBubble)) continue;
          const content = e.getBehavior(I.StorageBubble).content;
          if (!content || !content.length) continue;
          out.push(e);
        } catch (e2) {}
      }
      return out;
    };
    let tapped = 0, salvaged = 0, salvagedN = 0;
    // round 0: salvage crate bubbles (direct placement — no tap, no pop)
    for (const b of scan()) {
      const sb = b.getBehavior(I.StorageBubble);
      const content = Array.isArray(sb.content) ? sb.content.slice() : [];
      if (!content.some((c) => c && isCrateBp(c.blueprint))) continue;
      const remaining = [];
      let n = 0;
      for (const item of content) {
        if (!item || !isCrateBp(item.blueprint)) {
          remaining.push(item);
          continue;
        }
        let placed = false;
        for (const cell of S.mapGrid._cells.values()) {
          if (!cell || cell.content) continue;
          const r = placeCrate(item.blueprint, cell.column, cell.row);
          if (r.ok) { placed = true; break; }
        }
        if (placed) n++; else remaining.push(item);
      }
      if (n > 0) {
        sb.content = remaining;
        salvaged++;
        salvagedN += n;
        // a fully-emptied bubble is a dead world object — drop it so it
        // never lingers in the world model / storage bubble save
        if (!remaining.length) {
          try { S.world.removeGameObject(b); } catch (e2) {}
        }
      }
    }
    // rounds: tap non-crate bubbles via the game's own handler. 0.1s per
    // tap, no batch pause (user-tuned; note: 0.5s crashed the frame once —
    // raise toward 1000 if it freezes again).
    const tapDelay = 100;
    if (hasTap) {
      for (let round = 0; round < 8; round++) {
        let done = true;
        for (const b of scan()) {
          const bubbleContent = b.getBehavior(I.StorageBubble).content || [];
          // Never send a bubble containing an unplaced crate through the tap
          // path; crate moveContentToCell hangs and can freeze the game loop.
          if (bubbleContent.some((c) => c && isCrateBp(c.blueprint))) continue;
          const last = tappedBubbles.get(b);
          if (last && Date.now() - last < TAP_LAG_MS) continue; // already tapped, spawn in progress
          done = false;
          try {
            bubbleTapCtx._onStorageBubbleTapped(b);
            tappedBubbles.set(b, Date.now());
            tapped++;
          } catch (e2) {}
          await new Promise((r) => setTimeout(r, tapDelay));
        }
        if (done) break;
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
    if (tappedBubbles.size > 32) {
      const now = Date.now();
      for (const [k, t] of tappedBubbles) if (now - t > TAP_LAG_MS * 2) tappedBubbles.delete(k);
    }
    const stuck = scan().length;
    return { ok: true, tapped, salvaged, salvagedN, stuck };
  }

  // instantly finish every ACTIVE timer whose label starts with the prefix
  // (crates: 'RewardCrateCooldown', productions: 'Order_', regen: 'regenerate_')
  function finishTimers(labelPrefix) {
    try {
      const timers = window.FMV.rootServices().timer._timerModel._timers;
      let n = 0;
      for (const [, e] of timers) {
        if (e._state !== 'ACTIVE') continue;
        if (labelPrefix && String(e._label || '').indexOf(labelPrefix) !== 0) continue;
        try { e._remaining = 0; e._onFinish(); n++; } catch (e2) {}
      }
      return { ok: true, finished: n };
    } catch (e) { return { ok: false, reason: e.message }; }
  }

  window.FMV = { board, merge, move, swap, remove, spawnCrate, services, req, I, root, rootServices,
                 grant, spawn, collectBubbles, finishTimers,
                 mergeCtor: MergeTriggerCtor, version: window.__FMV_version || '1.14.0' };
})();`;
