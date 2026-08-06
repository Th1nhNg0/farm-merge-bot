// Auto-farm loop over CDP (Discord-embedded game):
//   1. FILL — spawn crates on every empty cell until the map is full
//      (crate contents are ignored)
//   2. PLAN — plan ALL groups at once from one snapshot: natural 5/10/15
//      components + move/swap grouping for every key with >= 5 items
//   3. MERGE — all moves/swaps and merges are executed in ONE page evaluation
//      (batched with event-loop breathing) — fast, no per-op round trips
//   4. Repeats (re-fill after merges) until out of crates or no groups
//
// Usage:  node auto_farm.mjs
// Requires: Chrome on a CDP port (MCP Chrome: --remote-debugging-port=9222),
//           the Discord activity open + loaded,
//           poller installed (install_poller.mjs) + FMV installed (install_fmv.mjs).

import { CDP, attach, evalIn, findGameTarget, WS_URL } from "./cdp_lib.mjs";

const SPAWN_WAIT_MS = 4000;   // crate auto-open takes ~1-2 s
const MERGE_WAIT_MS = 1200;   // merge executor + animations (batched, so per-round)
const MAX_FILL_ROUNDS = 40;
const MAX_PLAN_ROUNDS = 60;
const MAX_CYCLE_ROUNDS = 12;

const cdp = new CDP(WS_URL);
await cdp.connect();
const target = await findGameTarget(cdp);
if (!target) throw new Error("game frame target not found — open the Discord activity first");
const sid = await attach(cdp, target.targetId);

const hasFmv = await evalIn(cdp, sid, "!!window.FMV");
if (!hasFmv.result.value) throw new Error("window.FMV not installed — run install_fmv.mjs first");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── board snapshot (cells + neighbors, read in the game frame) ─────────────
async function readBoard() {
  const res = await evalIn(
    cdp,
    sid,
    `(function(){
      const S = window.FMV.services();
      const I = window.FMV.I();
      const out = { cells: {}, empties: [], items: [] };
      for (const cell of S.mapGrid._cells.values()) {
        const e = {
          col: cell.column, row: cell.row, empty: !cell.content,
          neighbors: cell.getNeighbors().map(n => n.column + ':' + n.row)
        };
        if (cell.content) {
          const info = cell.content.getObjectIdAndTier ? cell.content.getObjectIdAndTier() : null;
          e.id = info ? info.id : null;
          e.tier = info ? info.tier : null;
          e.mergeable = cell.content.hasBehavior(I.Mergeable);
          if (e.mergeable) {
            try {
              const mb = cell.content.getBehavior(I.Mergeable);
              e.target = mb && mb._data && mb._data.target ? String(mb._data.target) : null;
            } catch (e2) {}
          }
        }
        const key = e.col + ':' + e.row;
        out.cells[key] = e;
        if (e.empty) out.empties.push(e); else out.items.push(e);
      }
      return out;
    })()`
  );
  if (res.exceptionDetails) return { error: "eval", cells: {} };
  return res.result.value;
}

// Wait for the farm to be fully loaded (mapGrid populated); aborts if the
// Discord activity restarts mid-run (FMV gone or board empty).
async function waitBoard() {
  for (let i = 0; i < 24; i++) {
    const alive = await evalIn(cdp, sid, "!!window.FMV");
    if (alive.result.value !== true) {
      throw new Error("window.FMV lost — the Discord activity restarted; re-run install_fmv.mjs");
    }
    const board = await readBoard();
    if (board && board.items && board.items.length + board.empties.length > 500) return board;
    await sleep(5000);
  }
  throw new Error("board not loaded after 2 min — is the farm visible?");
}

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

// ── Phase 1: FILL — spawn crates until no empty cells ──────────────────────
async function phaseFill() {
  let round = 0;
  let spawnedTotal = 0;
  while (round < MAX_FILL_ROUNDS) {
    round++;
    const board = await readBoard();
    if (!board.empties.length) {
      console.log("Map is full — no empty cells left.");
      return { filled: true, spawned: spawnedTotal };
    }
    const cratesRes = await evalIn(
      cdp, sid, "window.FMV.rootServices().inventory.getAmount('crates')"
    );
    const crates = cratesRes.result.value;
    console.log(`fill ${round}: ${board.empties.length} empty cells, ${crates} crates left`);
    if (crates <= 0) { console.log("Out of crates — stopping fill."); return { filled: false, spawned: spawnedTotal }; }
    let spawned = 0;
    for (const e of board.empties) {
      const r = await evalIn(cdp, sid, `window.FMV.spawnCrate(${e.col}, ${e.row})`);
      if (r.result.value && r.result.value.ok) spawned++;
    }
    spawnedTotal += spawned;
    console.log(`spawned ${spawned}/${board.empties.length} crates, waiting for auto-open...`);
    await sleep(SPAWN_WAIT_MS);
  }
  console.log("Fill hit round cap.");
  return { filled: false, spawned: spawnedTotal };
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
          !neverMove(n));
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

  // 1) natural connected components that are multiples of 5 (no moves needed)
  for (const k of Object.keys(counts)) {
    if (counts[k] < 5) continue;
    const [id, tier] = k.split("_");
    for (const comp of connectedComponents(items, { id, tier })) {
      if (comp.length >= 5 && comp.length % 5 === 0) {
        naturals.push({ key: k, cells: comp });
        for (const c of comp) usedCells.add(c.col + ":" + c.row);
      }
    }
  }

  // 2) grouped plans: biggest groups first (15 > 10 > 5), many per key
  const groups = [];
  const keys = Object.entries(counts)
    .filter(([, n]) => n >= 5)
    .sort((a, b) => b[1] - a[1]);
  for (const [k] of keys) {
    const [id, tier] = k.split("_");
    const avail = () => items.filter((c) => c.id === id && c.tier === tier &&
      !usedCells.has(c.col + ":" + c.row) && !usedItems.has(c.col + ":" + c.row));
    let n = avail().length;
    while (n >= 5) {
      const size = n >= 15 ? 15 : n >= 10 ? 10 : 5;
        const g = planGroup(board, { id, tier }, size, usedCells, usedItems, neverMove);
      if (!g) break;
      groups.push({ key: k, size, ...g });
      for (const c of g.group) usedCells.add(c.col + ":" + c.row);
      for (const s of g.sources) usedItems.add(s.col + ":" + s.row);
      n = avail().length;
    }
  }
  return { naturals, groups };
}

// ── execute all moves/swaps + merges in ONE page evaluation (fast path) ────
async function executeBatch(naturals, groups) {
  const moves = [];
  const swaps = [];
  const merges = [];
  for (const g of groups) {
    let si = 0;
    for (const t of g.needsMove) {
      const s = g.sources[si++];
      moves.push([[s.col, s.row], [t.col, t.row]]);
    }
    for (const t of g.needsSwap) {
      const s = g.sources[si++];
      swaps.push([[s.col, s.row], [t.col, t.row]]);
    }
  }
  for (const nat of naturals) merges.push([[nat.cells[0].col, nat.cells[0].row], [nat.cells[1].col, nat.cells[1].row]]);
  for (const g of groups) merges.push([[g.group[0].col, g.group[0].row], [g.group[1].col, g.group[1].row]]);

  const expr = `(async function(){
    const moves = ${JSON.stringify(moves)};
    const swaps = ${JSON.stringify(swaps)};
    const merges = ${JSON.stringify(merges)};
    const out = { moves: [], swaps: [], merges: [] };
    const breathe = () => new Promise((r) => setTimeout(r, 0));
    for (let i = 0; i < moves.length; i += 30) {
      for (const m of moves.slice(i, i + 30)) out.moves.push(window.FMV.move(m[0][0], m[0][1], m[1][0], m[1][1]));
      await breathe();
    }
    for (let i = 0; i < swaps.length; i += 30) {
      for (const m of swaps.slice(i, i + 30)) out.swaps.push(window.FMV.swap(m[0][0], m[0][1], m[1][0], m[1][1]));
      await breathe();
    }
    for (let i = 0; i < merges.length; i += 20) {
      for (const m of merges.slice(i, i + 20)) out.merges.push(window.FMV.merge(m[0][0], m[0][1], m[1][0], m[1][1]));
      await breathe();
    }
    return out;
  })()`;
  const res = await evalIn(cdp, sid, expr);
  if (res.exceptionDetails) return { error: "eval", exceptionDetails: res.exceptionDetails };
  return res.result.value;
}

// ── Phase 2+3: PLAN (group items into 5/10/15) + MERGE the groups ──────────
async function phasePlanMerge() {
  let round = 0;
  while (round < MAX_PLAN_ROUNDS) {
    round++;
    const board = await readBoard();
    const { naturals, groups } = planAll(board);
    if (!naturals.length && !groups.length) {
      console.log("No 5/10/15 group possible this round — done.");
      return false;
    }

    const result = await executeBatch(naturals, groups);
    if (result.error) {
      console.log(`round ${round}: batch failed: ${result.error}`);
      return false;
    }
    const movesOk = (result.moves || []).filter((m) => m && m.ok).length;
    const swapsOk = (result.swaps || []).filter((m) => m && m.ok).length;
    const mergesOk = (result.merges || []).filter((m) => m && m.ok).length;
    console.log(`round ${round}: ${naturals.length} natural + ${groups.length} grouped, ` +
      `moves ${movesOk}/${result.moves.length}, swaps ${swapsOk}/${result.swaps.length}, merges ${mergesOk}/${result.merges.length}`);
    if (mergesOk === 0) { console.log("No merge succeeded — stopping plan phase."); return false; }
    await sleep(MERGE_WAIT_MS);
  }
  console.log("Plan/merge hit round cap.");
  return true;
}

// ── Main loop: FILL -> PLAN+MERGE -> repeat ────────────────────────────────
const before = await waitBoard();
console.log("board before:", JSON.stringify(summarize(before.items)));
console.log("empty cells:", before.empties.length);

for (let cycle = 1; cycle <= MAX_CYCLE_ROUNDS; cycle++) {
  console.log(`\n=== cycle ${cycle}: fill ===`);
  const fill = await phaseFill();
  await sleep(1500);
  console.log(`=== cycle ${cycle}: plan+merge ===`);
  const progressed = await phasePlanMerge();
  // stop when nothing was grouped/merged AND no crates were spawned to change the board
  if (!progressed && fill.spawned === 0) { console.log("No further progress — stopping."); break; }
}

const after = await readBoard();
console.log("\nboard after:", JSON.stringify(summarize(after.items)));
console.log("empty cells:", after.empties.length);
const cratesLeft = await evalIn(
  cdp, sid, "window.FMV.rootServices().inventory.getAmount('crates')"
);
console.log("crates left:", cratesLeft.result.value);
console.log("done");

cdp.close();
