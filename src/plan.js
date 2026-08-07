// Shared merge planner — single source of truth for the in-game menu
// (menu.js embeds this source) and Node scripts that import it. Pure
// functions over a board snapshot:
//   board = {
//     cells:   { "col:row": { col, row, empty, neighbors: ["col:row", ...],
//                             id, tier, mergeable, target } },
//     items:   [ ... non-empty cells ... ],
//     empties: [ ... empty cells ... ]
//   }
// No game/DOM dependencies — runs as a plain <script> in the game frame
// (window.FMVPlan); menu.js prepends this source to its own injection.
//
// Merge rules (enforced by the game, mirrored here):
//   - 5 identical items merge -> 2 of the next level; 10 -> 4; 15 -> 6
//     (bonus math for exact multiples of 5, so merges only fire on
//     5/10/15 chains).

globalThis.FMVPlan = (function () {
  "use strict";

  function summarize(items) {
    const counts = {};
    for (const i of items) {
      if (!i.mergeable || !i.id) continue;
      const k = i.id + "_" + i.tier;
      counts[k] = (counts[k] || 0) + 1;
    }
    return counts;
  }

  // ── never-move rule (family-based) ─────────────────────────────────────────
  // Items are skipped by sort/plan+merge when they have no item id (buildings)
  // or their family has no mergeable level (tree/rock/area/premium land,
  // traintrack, delivery, decorative, blocker, toolbox). Families WITH mergeable
  // levels are fully movable, including their non-mergeable MAX level items.
  const KNOWN_STATIC = new Set(['tree', 'rock', 'area', 'premium', 'traintrack',
    'delivery', 'decorative', 'decorative_timelimitedevent', 'blocker', 'toolbox']);
  function computeNeverMove(board) {
    const chainIds = new Set();
    for (const it of board.items) {
      if (it.mergeable && it.id) chainIds.add(it.id);
      if (it.target) {
        const i = String(it.target).lastIndexOf("_");
        chainIds.add(i > 0 ? String(it.target).slice(0, i) : String(it.target));
      }
    }
    return (it) => {
      if (!it || !it.id) return true;
      if (KNOWN_STATIC.has(it.id)) return true;
      if (chainIds.has(it.id)) return false;
      return !it.mergeable;
    };
  }

  function connectedComponents(cells, key) {
    const keyCells = cells.filter((c) => c.id === key.id && c.tier === key.tier);
    const byKey = new Map(keyCells.map((c) => [c.col + ":" + c.row, c]));
    const seen = new Set();
    const comps = [];
    for (const start of keyCells) {
      const sk = start.col + ":" + start.row;
      if (seen.has(sk)) continue;
      const comp = [];
      const queue = [start];
      seen.add(sk);
      while (queue.length) {
        const c = queue.shift();
        comp.push(c);
        for (const nk of c.neighbors) {
          const n = byKey.get(nk);
          if (n && !seen.has(nk)) { seen.add(nk); queue.push(n); }
        }
      }
      comps.push(comp);
    }
    return comps;
  }

  // ── grouping plan: find a connected group of `size` cells for a key ────────
  // Cells are steppable when empty, already same-key, or occupied by anything
  // (swap target). Same-key/empty cells are preferred as BFS neighbors.
  // `usedCells`/`usedItems` exclude cells claimed by other plans (multi-group
  // planning on a single snapshot).
  // Returns { group, needsMove, needsSwap, sources } or null.
  function planGroup(board, key, size, usedCells, usedItems, neverMove) {
    const keyCells = board.items.filter((c) => c.id === key.id && c.tier === key.tier &&
      !neverMove(c) &&
      !usedCells.has(c.col + ":" + c.row) && !usedItems.has(c.col + ":" + c.row));
    if (keyCells.length < size) return null;
    const byPos = board.cells;
    const isTarget = (c) => c.empty || (c.id === key.id && c.tier === key.tier);

    let bestAnchor = null;
    let bestScore = -1;
    for (const c of keyCells) {
      let score = 0;
      for (const nk of c.neighbors) {
        const n = byPos[nk];
        if (n && !usedCells.has(nk) && isTarget(n)) score++;
      }
      if (score > bestScore) { bestScore = score; bestAnchor = c; }
    }
    if (!bestAnchor) return null;

    const group = [];
    const visited = new Set([bestAnchor.col + ":" + bestAnchor.row]);
    const queue = [bestAnchor];
    while (queue.length && group.length < size) {
      const c = queue.shift();
      group.push(c);
      const neighbors = c.neighbors
        .map((nk) => byPos[nk])
        .filter((n) => n && !visited.has(n.col + ":" + n.row) && !usedCells.has(n.col + ":" + n.row) &&
          !usedItems.has(n.col + ":" + n.row) &&
          (n.empty || !neverMove(n)));
      // prefer empty/same-key cells first, then any occupied cell (swap target)
      neighbors.sort((a, b) => (isTarget(b) ? 1 : 0) - (isTarget(a) ? 1 : 0));
      for (const n of neighbors) {
        if (group.length >= size) break;
        visited.add(n.col + ":" + n.row);
        queue.push(n);
      }
    }
    if (group.length < size) return null;

    const groupKeys = new Set(group.map((c) => c.col + ":" + c.row));
    const needsMove = group.filter((c) => c.empty);
    const needsSwap = group.filter((c) => !c.empty && !(c.id === key.id && c.tier === key.tier));
    const need = needsMove.length + needsSwap.length;
    if (!need) return null;
    // sources: same-key items outside the group, unclaimed
    const sources = board.items.filter((c) => c.id === key.id && c.tier === key.tier &&
      !neverMove(c) &&
      !groupKeys.has(c.col + ":" + c.row) &&
      !usedCells.has(c.col + ":" + c.row) && !usedItems.has(c.col + ":" + c.row));
    if (sources.length < need) return null;
    return { group, needsMove, needsSwap, sources: sources.slice(0, need) };
  }

  // ── plan ALL merges + groups from one board snapshot ───────────────────────
  // Returns { naturals: [...], groups: [...] } with disjoint cells/items.
  function planAll(board) {
    const neverMove = computeNeverMove(board);
    const items = board.items.filter((i) => i.mergeable && i.id);
    const counts = summarize(items);
    const naturals = [];
    const usedCells = new Set();
    const usedItems = new Set();

    // 1) natural connected components: chunk every component of >= 5 into
    //    15/10/5 chains (largest first, contiguous BFS prefixes), so solid
    //    clusters that aren't exact multiples of 5 still merge
    //    (16 -> 15+1, 12 -> 10+2) instead of being skipped entirely
    for (const k of Object.keys(counts)) {
      if (counts[k] < 5) continue;
      const [id, tier] = k.split("_");
      for (const comp of connectedComponents(items, { id, tier })) {
        let restMap = new Map(comp.map((c) => [c.col + ":" + c.row, c]));
        while (restMap.size >= 5) {
          // previous chunks can disconnect the rest — chunk the LARGEST
          // connected piece each iteration (smaller pieces stay for groups)
          const pieces = connectedComponents([...restMap.values()], { id, tier });
          const bestPiece = pieces.reduce((a, b) => (a.length >= b.length ? a : b));
          if (bestPiece.length < 5) break;
          const size = bestPiece.length >= 15 ? 15 : bestPiece.length >= 10 ? 10 : 5;
          const chunk = [];
          const seen = new Set();
          const queue = [bestPiece[0]];
          seen.add(bestPiece[0].col + ":" + bestPiece[0].row);
          while (queue.length && chunk.length < size) {
            const c = queue.shift();
            chunk.push(c);
            for (const nk of c.neighbors) {
              if (chunk.length >= size) break;
              const n = restMap.get(nk);
              if (n && !seen.has(nk)) { seen.add(nk); queue.push(n); }
            }
          }
          naturals.push({ key: k, cells: chunk });
          for (const c of chunk) { usedCells.add(c.col + ":" + c.row); restMap.delete(c.col + ":" + c.row); }
        }
      }
    }

    // 2) grouped plans: biggest groups first (15 > 10 > 5), many per key
    const groups = [];
    const keys = Object.entries(counts)
      .filter(([, n]) => n >= 5)
      .map(([k, n]) => {
        const [id, tier] = k.split("_");
        return [k, n, connectedComponents(items, { id, tier }).length];
      })
      .sort((a, b) => a[2] - b[2] || b[1] - a[1]);
    for (const [k] of keys) {
      const [id, tier] = k.split("_");
      const avail = () => items.filter((c) => c.id === id && c.tier === tier &&
        !usedCells.has(c.col + ":" + c.row) && !usedItems.has(c.col + ":" + c.row));
      let n = avail().length;
      while (n >= 5) {
        const maxSize = n >= 15 ? 15 : n >= 10 ? 10 : 5;
        let g = null;
        for (let size = maxSize; size >= 5; size -= 5) {
          g = planGroup(board, { id, tier }, size, usedCells, usedItems, neverMove);
          if (g) break;
        }
        if (!g) break;
        groups.push({ key: k, size: g.group.length, group: g.group, needsMove: g.needsMove, needsSwap: g.needsSwap, sources: g.sources });
        for (const c of g.group) usedCells.add(c.col + ":" + c.row);
        for (const s of g.sources) usedItems.add(s.col + ":" + s.row);
        n = avail().length;
      }
    }
    return { naturals, groups };
  }

  return { summarize, computeNeverMove, connectedComponents, planGroup, planAll };
})();
