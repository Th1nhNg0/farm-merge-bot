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
    const members = rootServices().timer._updatableGroup._members;
    for (const m of members) {
      if (m && m._services && m._services.mapGrid) return m._services;
    }
    return null;
  }

  function board() {
    const S = services();
    if (!S) return { error: 'services not ready' };
    const out = [];
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
      S.world.removeGameObject(from.content);
      to.content.addBehavior(new (MergeTriggerCtor())({ cell: to.position, chain }));
      return { ok: true, chainLen: chain.length, total };
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
    // compute everything fallible BEFORE mutating the grid — a failure here
    // must not leave the board half-changed (desync + false ok:false)
    let worldPos;
    try { worldPos = S.axonometricProjection.getWorldPosition(toCol, toRow); }
    catch (e) { return { ok: false, reason: 'position calc failed: ' + e.message }; }
    try {
      S.mapGrid.setContent(fromCol, fromRow, null);
      gp.column = toCol; gp.row = toRow;
      if (gp._data) { gp._data.column = toCol; gp._data.row = toRow; }
      S.mapGrid.setContent(toCol, toRow, entity);
      entity.position.copyFrom(worldPos);
    } catch (e) { return { ok: false, reason: 'move failed: ' + e.message }; }
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
    } catch (e) { return { ok: false, reason: 'swap failed: ' + e.message }; }
    return { ok: true, moved: [ea.getBlueprintID(), eb.getBlueprintID()] };
  }

  window.FMV = { board, merge, move, swap, remove, spawnCrate, services, req, I, root, rootServices,
                 mergeCtor: MergeTriggerCtor, version: window.__FMV_version || '1.7.3' };
})();`;
