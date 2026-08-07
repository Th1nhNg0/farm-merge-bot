// Shared in-frame game-access helpers (window.FMVUtil). Prepended to the menu
// injection by menu.js (same pattern as plan.js). Pure helpers over the live
// game services — no state, no UI. Everything reads window.FMV at call time,
// so the source works regardless of install order.

globalThis.FMVUtil = (function () {
  "use strict";

  // iterate every cell of the map grid (skip holes); fn returning false stops
  function forEachCell(S, fn) {
    if (!S || !S.mapGrid || !S.mapGrid._cells) return;
    for (const cell of S.mapGrid._cells.values()) {
      if (!cell) continue;
      if (fn(cell) === false) return;
    }
  }

  // board snapshot: { cells: {"col:row": {col,row,empty,neighbors,id,tier,
  // mergeable,target}}, empties: [...], items: [...] } or { error }
  function readBoard() {
    if (!window.FMV || !window.FMV.services) return { error: "FMV lost" };
    const S = window.FMV.services();
    if (!S || !S.mapGrid) return { error: "farm services not ready" };
    const I = window.FMV.I();
    const out = { cells: {}, empties: [], items: [] };
    forEachCell(S, (cell) => {
      const e = { col: cell.column, row: cell.row, empty: !cell.content, neighbors: [] };
      try { e.neighbors = cell.getNeighbors().map((n) => n.column + ":" + n.row); } catch (e2) {}
      if (cell.content) {
        let info = null;
        try { info = cell.content.getObjectIdAndTier ? cell.content.getObjectIdAndTier() : null; } catch (e2) {}
        e.id = info ? info.id : (cell.content.getBlueprintID ? cell.content.getBlueprintID() : null);
        e.tier = info ? info.tier : null;
        e.mergeable = !!(cell.content.hasBehavior && cell.content.hasBehavior(I.Mergeable));
        if (e.mergeable) {
          try {
            const mb = cell.content.getBehavior(I.Mergeable);
            e.target = mb && mb._data && mb._data.target ? String(mb._data.target) : null;
          } catch (e2) {}
        }
      }
      const key = e.col + ":" + e.row;
      out.cells[key] = e;
      if (e.empty) out.empties.push(e); else out.items.push(e);
    });
    return out;
  }

  // tile save model (the game's per-tile persistence: cooldown, lootable...)
  function tileModel() {
    try {
      return window.FMV.rootServices().playerData._dataContainers["0"]._data;
    } catch (e) { return null; }
  }
  function tileAt(tiles, col, row) {
    if (!tiles) return null;
    try {
      const m = tiles["TilesStateModel_" + col + ":" + row];
      return m && m.data && m.data.state ? m.data.state.data : null;
    } catch (e) { return null; }
  }

  // tap router: any subscriber context exposing the game's own click
  // simulation (the subscriber list is rebuilt as subsystems spawn/die, so
  // never trust index 0)
  function getTapRouter(S) {
    try {
      const subs = S.interactionService.onGestureTap._subscribers;
      for (const s of subs || []) {
        if (s && s.context && typeof s.context._simulateClick === "function") return s.context;
      }
    } catch (e) {}
    return null;
  }

  // walk one entity's onBehaviorAdded event: every behavior-family registry
  // yields (registry, behaviorTypes, familyContext); visit returning false
  // stops the walk
  function walkBehaviorRegistries(ev, visit) {
    if (!ev || !ev._subscribers) return;
    for (let i = 0; i < ev._subscribers.length; i++) {
      const reg = ev._subscribers[i].context;
      if (!reg || !reg.onGameObjectAdded || !reg._filter) continue;
      let types = null;
      try { types = reg._filter._behaviorTypes; } catch (e) {}
      if (!types || !Array.isArray(types)) continue;
      let sub = null;
      try { sub = reg.onGameObjectAdded._subscribers[0].context; } catch (e) { continue; }
      if (!sub) continue;
      if (visit(reg, types, sub) === false) return;
    }
  }

  // ground Collectable bubbles whose reward is a real board item (blueprint) —
  // coin/gem/energy reward bubbles are not harvest/clear products
  function isProductCollectable(e, I) {
    try {
      const cb = e.getBehavior(I.Collectable);
      const r = cb && cb._data && cb._data.reward;
      if (!r || !r[0] || !r[0].key) return false;
      return window.FMV.rootServices().blueprintCollection.hasBlueprint(r[0].key);
    } catch (err) { return false; }
  }

  // live ground collectables on the board (stale references guarded)
  function collectablesOnBoard(S, I) {
    const out = [];
    forEachCell(S, (cell) => {
      if (!cell.content) return;
      const e = cell.content;
      if (!e.hasBehavior || !e.hasBehavior(I.Collectable)) return;
      if (!isProductCollectable(e, I)) return;
      if (S.mapGrid.getCell(cell.column, cell.row).content !== e) return;
      out.push(e);
    });
    return out;
  }

  return { forEachCell, readBoard, tileModel, tileAt, getTapRouter,
           walkBehaviorRegistries, isProductCollectable, collectablesOnBoard };
})();
