// window.FMV helper (v4, build-agnostic) installed into the game frame.
// Reads the discovered module ids/layout from window.__FMV_* (set by hunter).
// Works with the Discord-embedded build (services under _nonCriticalServices).
//   FMV.board()                        -> [{col, row, id, tier, mergeable}, ...]
//   FMV.merge(fromCol, fromRow, toCol, toRow) -> {ok, reason|chainLen, total}
//   FMV.move / FMV.swap / FMV.spawnCrate / FMV.services()
//   FMV.req, FMV.I, FMV.mergeCtor, FMV.root(), FMV.rootServices()

export const FMV_HELPER_SOURCE = `(function(){
  const req = window.__FMV_req;
  const rootKey = window.__FMV_rootKey;
  const servicesKey = window.__FMV_servicesKey;
  const root = () => rootKey ? req(window.__FMV_rootId)[rootKey] : req(window.__FMV_rootId);
  const rootServices = () => root()[servicesKey];
  const I = () => window.__FMV_mapKey === 'I' ? req(window.__FMV_mapId).I : req(window.__FMV_mapId);
  const MergeTriggerCtor = () => req(window.__FMV_hcId)[window.__FMV_hcKey];

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

  function merge(fromCol, fromRow, toCol, toRow) {
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
    let chain;
    try {
      // flood fill INCLUDES the start cell; the game's own call passes undefined
      // as 3rd arg (it is a max-length cap, not an exclusion list). The source
      // cell must be filtered out because the game would have it empty mid-drag.
      chain = S.gridFilter.getAdjacentObjectsWithSameID(to, spec, undefined, [I().Mergeable]);
      chain = chain.filter(c => c !== from);
    } catch (e) { return { ok: false, reason: 'chain calc failed: ' + e.message }; }
    if (chain.length < 2) return { ok: false, reason: 'chain too short', chainLen: chain.length };
    try {
      S.world.removeGameObject(from.content);
      to.content.addBehavior(new (MergeTriggerCtor())({ cell: to.position, chain }));
    } catch (e) { return { ok: false, reason: 'merge call failed: ' + e.message }; }
    return { ok: true, chainLen: chain.length, total: chain.length + 1 };
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
    try {
      S.mapGrid.setContent(fromCol, fromRow, null);
      gp.column = toCol; gp.row = toRow;
      gp._data.column = toCol; gp._data.row = toRow;
      S.mapGrid.setContent(toCol, toRow, entity);
      const worldPos = S.axonometricProjection.getWorldPosition(toCol, toRow);
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
    try {
      S.mapGrid.setContent(aCol, aRow, eb);
      S.mapGrid.setContent(bCol, bRow, ea);
      gpa.column = bCol; gpa.row = bRow;
      gpa._data.column = bCol; gpa._data.row = bRow;
      gpb.column = aCol; gpb.row = aRow;
      gpb._data.column = aCol; gpb._data.row = aRow;
      ea.position.copyFrom(S.axonometricProjection.getWorldPosition(bCol, bRow));
      eb.position.copyFrom(S.axonometricProjection.getWorldPosition(aCol, aRow));
    } catch (e) { return { ok: false, reason: 'swap failed: ' + e.message }; }
    return { ok: true, moved: [ea.getBlueprintID(), eb.getBlueprintID()] };
  }

  window.FMV = { board, merge, move, swap, spawnCrate, services, req, I, root, rootServices,
                 mergeCtor: MergeTriggerCtor, version: '1.4.1' };
})();`;
