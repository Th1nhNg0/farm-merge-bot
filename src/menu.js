// In-game bot menu overlay (FMV Bot). Installed by install_menu.mjs.
// All bot logic runs INSIDE the game frame — the menu buttons drive it:
//   [Fill]         spawn crates on every empty cell until the map is full
//   [Merge]        plan ALL groups (natural 5/10/15 + move/swap grouping)
//                  from one snapshot, then execute them in one batched pass
//   [Orders]       claim completed orders, then start every affordable order
//   Auto tab: checkboxes select which automations run — [Auto Orders] claim +
//                  start affordable orders in a loop (plan+merge when the board
//                  fills up), [Auto Clear] spend energy clearing tree/rock/
//                  toolbox via the game's own payment/collect tap functions —
//                  and [Auto All] starts every checked loop in parallel.
//   [Refresh]      update the items/empty/crates status line
// Exposes window.FMV.menu = { orders, fill, planMerge, autoOrders, autoClear,
//                             autoAll, stop, status, running }.

// Shared planner (plan.js) is prepended to the injected source, so the menu
// IIFE below can use window.FMVPlan (same logic as the CLI scripts).
import { readFileSync } from "node:fs";

export const MENU_SOURCE = readFileSync(new URL("./plan.js", import.meta.url), "utf8") + "\n" + `(function(){
  if (window.FMV && window.FMV.menu && window.FMV.menu.running && window.FMV.menu.running()) {
    return { ok: false, reason: 'menu running' };
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // ── adaptive settle ───────────────────────────────────────────────────────
  // Measures the game's real tick rate once (~300ms window) and derives a
  // settle time of ~4 ticks, clamped to [minMs, maxMs]. Visible tabs tick at
  // ~15fps (~66ms/tick) → settle ≈ 150-260ms; throttled background tabs tick
  // at ~1fps → settle ≈ 1500ms (the old fixed wait). The measurement is cached
  // ~30s and invalidated on visibility changes, so operations stay fast when
  // the window is visible and safe when the tab is throttled.
  let settleCacheMs = null, settleCacheAt = 0;
  function invalidateSettle() { settleCacheMs = null; settleCacheAt = 0; }
  try {
    document.addEventListener('visibilitychange', invalidateSettle);
    document.addEventListener('focus', invalidateSettle);
    document.addEventListener('blur', invalidateSettle);
  } catch (e) {}
  const adaptSettle = async (minMs, maxMs) => {
    if (settleCacheMs !== null && Date.now() - settleCacheAt < 30000) return settleCacheMs;
    const tickMs = await new Promise((res) => {
      let n = 0, first = null;
      const tick = (t) => {
        if (first === null) first = t;
        else if (t - first >= 300) { res((t - first) / n); return; }
        n++;
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      setTimeout(() => res(n ? 300 / n : 1000), 400);
    });
    settleCacheMs = Math.max(minMs, Math.min(maxMs, tickMs * 4));
    settleCacheAt = Date.now();
    return settleCacheMs;
  };
  const state = { busy: false, running: false, stop: false, rounds: 0, opStart: null };
  const stats = { merged: 0, moved: 0, swapped: 0, crates: 0, harvested: 0,
    lootCollected: 0, groundCollected: 0, sourcesCleared: 0, energySpent: 0,
    ordersClaimed: 0, ordersStarted: 0, friendRewards: 0, failed: 0, startedAt: Date.now(),
    mergedBy: {} };
  function statsReset() {
    stats.merged = 0; stats.moved = 0; stats.swapped = 0; stats.crates = 0;
    stats.harvested = 0; stats.lootCollected = 0; stats.groundCollected = 0;
    stats.sourcesCleared = 0; stats.energySpent = 0;
    stats.ordersClaimed = 0; stats.ordersStarted = 0; stats.friendRewards = 0;
    stats.failed = 0; stats.startedAt = Date.now();
    stats.mergedBy = {};
  }
  const MAX_FILL_ROUNDS = 40;
  const MAX_PLAN_ROUNDS = 60;
  const ORDERS_WAIT_MS = 5000;
  const CLEAR_WAIT_MS = 4000;
  const CLEAR_SOURCES = new Set(['tree', 'rock', 'toolbox']);
  // ── logging ──────────────────────────────────────────────────────────────
  function ts() {
    const d = new Date(), p = (n) => (n < 10 ? '0' : '') + n;
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function updateLogView() {
    const open = logEl.current.classList.contains('open');
    const kids = logEl.current.childNodes;
    for (let i = 0; i < kids.length; i++) kids[i].style.display = open || i === kids.length - 1 ? '' : 'none';
    logEl.current.scrollTop = logEl.current.scrollHeight;
  }
  function log(msg, level) {
    const line = '[' + ts() + '] ' + msg;
    if (typeof console !== 'undefined') console.log('FMV-BOT ' + line);
    if (logEl.current) {
      const div = document.createElement('div');
      div.className = 'l' + (level ? ' ' + level : '');
      div.textContent = line;
      logEl.current.appendChild(div);
      while (logEl.current.childNodes.length > 300) logEl.current.removeChild(logEl.current.firstChild);
      updateLogView();
    }
  }
  const logEl = { current: null };

  function cratesLeft() {
    return window.FMV.rootServices().inventory.getAmount('crates');
  }
  function assertFMV() {
    if (!window.FMV || !window.FMV.services) {
      throw new Error('FMV lost — Discord activity restarted; re-run install_menu.mjs');
    }
  }

  // ── board snapshot (read in-frame) ───────────────────────────────────────
  function readBoard() {
    assertFMV();
    const S = window.FMV.services();
    if (!S || !S.mapGrid) return { error: 'farm services not ready' };
    const I = window.FMV.I();
    const out = { cells: {}, empties: [], items: [] };
    for (const cell of S.mapGrid._cells.values()) {
      if (!cell) continue;
      const e = { col: cell.column, row: cell.row, empty: !cell.content, neighbors: [] };
      try { e.neighbors = cell.getNeighbors().map((n) => n.column + ':' + n.row); } catch (e2) {}
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
      const key = e.col + ':' + e.row;
      out.cells[key] = e;
      if (e.empty) out.empties.push(e); else out.items.push(e);
    }
    return out;
  }

  // ── planner: shared logic from plan.js (window.FMVPlan) ────────────────────

  // ── execute all moves/swaps + merges in one batched pass (breathing) ────
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
    for (const nat of naturals) merges.push({ key: nat.key, from: nat.cells[0], to: nat.cells[1] });
    for (const g of groups) merges.push({ key: g.key, from: g.group[0], to: g.group[1] });

    const out = { moves: [], swaps: [], merges: [] };
    const FMV = window.FMV;
    for (let i = 0; i < moves.length && !state.stop; i += 30) {
      for (const m of moves.slice(i, i + 30)) out.moves.push(FMV.move(m[0][0], m[0][1], m[1][0], m[1][1]));
      await sleep(0);
    }
    for (let i = 0; i < swaps.length && !state.stop; i += 30) {
      for (const m of swaps.slice(i, i + 30)) out.swaps.push(FMV.swap(m[0][0], m[0][1], m[1][0], m[1][1]));
      await sleep(0);
    }
    for (let i = 0; i < merges.length && !state.stop; i += 20) {
      for (const m of merges.slice(i, i + 20)) {
        let tex = null;
        try {
          const cell = FMV.services().mapGrid.getCell(m.from.col, m.from.row);
          if (cell && cell.content && cell.content.children) {
            for (const k of cell.content.children) {
              if (k && k.name === 'maincontainer' && k.children) {
                for (const s of k.children) {
                  if (s && s.name === 'mainsprite' && s._texture) { tex = s._texture; break; }
                }
                if (tex) break;
              }
            }
          }
        } catch (e3) {}
        const r = FMV.merge(m.from.col, m.from.row, m.to.col, m.to.row);
        out.merges.push(r);
        if (r && r.ok) {
          const prev = stats.mergedBy[m.key];
          stats.mergedBy[m.key] = { n: (prev ? prev.n : 0) + 1, tex: tex || (prev && prev.tex) || null };
        }
      }
      await sleep(0);
    }
    return out;
  }

  // ── Phase 1: FILL ────────────────────────────────────────────────────────
  async function phaseFill() {
    const spawnWait = 1000;
    let round = 0;
    let spawnedTotal = 0;
    while (round < MAX_FILL_ROUNDS && !state.stop) {
      round++;
      state.rounds = round;
      assertFMV();
      const board = readBoard();
      if (board.error) throw new Error(board.error);
      if (!board.empties.length) {
        log('map full', 'ok');
        return { filled: true, spawned: spawnedTotal };
      }
      const crates = cratesLeft();
      log('fill ' + round + ': ' + board.empties.length + ' empty · ' + crates + ' crates');
      if (crates <= 0) { log('no crates — fill stop', 'warn'); return { filled: false, spawned: spawnedTotal }; }
      let spawned = 0;
      for (const e of board.empties) {
        if (state.stop) break;
        const r = window.FMV.spawnCrate(e.col, e.row);
        if (r && r.ok) spawned++;
        if (spawned % 50 === 0) await sleep(0);
      }
      spawnedTotal += spawned;
      stats.crates += spawned;
      log('+' + spawned + '/' + board.empties.length + ' crates, opening…');
      await sleep(spawnWait);
    }
    log('fill cap');
    return { filled: false, spawned: spawnedTotal };
  }

  // ── Phase 2+3: PLAN + MERGE ──────────────────────────────────────────────
  async function phasePlanMerge() {
    const mergeWait = await adaptSettle(150, 1500);
    let round = 0;
    while (round < MAX_PLAN_ROUNDS && !state.stop) {
      round++;
      state.rounds = round;
      assertFMV();
      const board = readBoard();
      const { naturals, groups } = window.FMVPlan.planAll(board);
      if (!naturals.length && !groups.length) {
        log('nothing to merge — done');
        return false;
      }
      log('r' + round + ': ' + naturals.length + '+' + groups.length + ' groups');
      const result = await executeBatch(naturals, groups);
      const movesOk = (result.moves || []).filter((m) => m && m.ok).length;
      const swapsOk = (result.swaps || []).filter((m) => m && m.ok).length;
      const mergesOk = (result.merges || []).filter((m) => m && m.ok).length;
      log('mv ' + movesOk + '/' + result.moves.length +
        ' · sw ' + swapsOk + '/' + result.swaps.length +
        ' · mg ' + mergesOk + '/' + result.merges.length);
      stats.merged += mergesOk;
      stats.moved += movesOk;
      stats.swapped += swapsOk;
      stats.failed += (result.moves.length - movesOk) + (result.swaps.length - swapsOk) +
        (result.merges.length - mergesOk);
      if (mergesOk === 0) { log('no merges — stop', 'warn'); return false; }
      if (state.stop) { log('stop — halting', 'warn'); return false; }
      await sleep(mergeWait);
    }
    log('plan cap');
    return true;
  }

  const VAULT_IDS = new Set(['coin', 'gem', 'crystal', 'energy', 'greenhouse', 'gazebo']);

  async function sortBoard() {
    assertFMV();
    const board = readBoard();
    if (board.error) throw new Error(board.error);

    const neverMove = window.FMVPlan.computeNeverMove(board);
    const fixedCells = new Set();
    const groups = new Map();
    for (const it of board.items) {
      const ck = it.col + ':' + it.row;
      if (neverMove(it)) { fixedCells.add(ck); continue; }
      const key = it.id + '_' + it.tier;
      if (!groups.has(key)) groups.set(key, { key: key, id: it.id, tier: it.tier, vault: VAULT_IDS.has(it.id), items: [] });
      groups.get(key).items.push(it);
    }
    // sort priority: TOP of the board → BOTTOM (money sits at the very bottom).
    // ids match by prefix or suffix so families like reward_crate_key hit 'key'.
    // Unknown ids land between animals and the vault block; within an id, tier low → high.
    const SORT_PRIORITY = [
      ['key', 'chest', 'reward_crate'],
      ['wood', 'log'],
      ['stone'],
      ['shovel', 'saw', 'axe', 'hammer', 'pickaxe', 'rake', 'scythe', 'shears',
       'watering', 'bucket', 'net', 'spade'],
      ['wheat', 'corn', 'carrot', 'tomato', 'pumpkin', 'potato',
       'strawberry', 'blueberry', 'grape', 'melon'],
      ['pig', 'chicken', 'cow', 'sheep', 'goat', 'duck', 'egg', 'bee'],
      ['greenhouse', 'gazebo'],
      ['energy'],
      ['gem', 'crystal'],
      ['coin'],
    ];
    const prioOf = (id) => {
      for (let i = 0; i < SORT_PRIORITY.length; i++)
        for (const p of SORT_PRIORITY[i])
          if (id.startsWith(p) || id.endsWith(p)) return i;
      return 6; // unknown → between animals and the vault block
    };
    const tierCmp = (x, y) => {
      const nx = Number(x), ny = Number(y);
      if (!isNaN(nx) && !isNaN(ny)) return nx - ny;
      if (isNaN(nx) && isNaN(ny)) return String(x).localeCompare(String(y));
      return isNaN(nx) ? 1 : -1;
    };
    const byKey = (a, b) => prioOf(a.id) - prioOf(b.id) || a.id.localeCompare(b.id) || tierCmp(a.tier, b.tier);
    const normal = [...groups.values()].filter((g) => !g.vault).sort(byKey);
    const vault = [...groups.values()].filter((g) => g.vault).sort(byKey);
    const vaultN = vault.reduce((s, g) => s + g.items.length, 0);

    const free = Object.values(board.cells)
      .filter((c) => !fixedCells.has(c.col + ':' + c.row))
      .sort((a, b) => a.row - b.row || a.col - b.col);

    // bottom strip: the vaultN free cells with the highest rows (bottom-most),
    // so money/energy/diamond always sit at the very bottom of the board —
    // not just at some far corner. Coins take the deepest cells.
    const zone = free.slice().sort((a, b) => b.row - a.row || a.col - b.col).slice(0, vaultN);
    const zoneSet = new Set(zone.map((c) => c.col + ':' + c.row));
    const zoneCells = zone.slice().sort((a, b) => a.row - b.row || a.col - b.col);
    const cropFree = free.filter((c) => !zoneSet.has(c.col + ':' + c.row));

    const plan = [];
    const targetKey = new Map();
    const assignCells = (groups, pool) => {
      let cur = 0;
      const missing = [];
      for (const g of groups) {
        const cells = pool.slice(cur, cur + g.items.length);
        if (cells.length < g.items.length) { missing.push(g); continue; }
        for (const c of cells) targetKey.set(c.col + ':' + c.row, g.key);
        plan.push({ key: g.key, cells: cells });
        cur += g.items.length;
      }
      return { cur: cur, missing: missing };
    };
    const cropRes = assignCells(normal, cropFree);
    let vaultRes = { missing: [] };
    if (vaultN) {
      vaultRes = assignCells(vault, zoneCells);
      if (vaultRes.missing.length) {
        const rest = assignCells(vaultRes.missing, cropFree.slice(cropRes.cur));
        log('sort: vault overflow ' + rest.missing.length + ' grp', 'warn');
      }
    }
    const total = plan.reduce((s, p) => s + p.cells.length, 0);
    log('sort: ' + plan.length + ' grp · ' + total + ' items · ' + fixedCells.size + ' fixed' +
      (vaultN ? ' · ' + vaultN + ' vault' : ''), 'ok');

    const FMV = window.FMV;
    const mirror = new Map();
    for (const c of Object.values(board.cells)) {
      mirror.set(c.col + ':' + c.row,
        c.empty ? null : { key: c.id + '_' + c.tier });
    }
    const split = (k) => k.split(':');
    let moves = 0, swaps = 0, fails = 0;
    const cap = total * 4 + 500;
    const breathe = async () => { if ((moves + swaps) % 50 === 0) await sleep(0); };
    const mv = async (s, t) => {
      const ss = split(s), tt = split(t);
      const r = FMV.move(+ss[0], +ss[1], +tt[0], +tt[1]);
      if (r && r.ok) { mirror.set(t, mirror.get(s)); mirror.set(s, null); moves++; }
      else fails++;
      await breathe();
    };
    const sw = async (s, t) => {
      const ss = split(s), tt = split(t);
      const r = FMV.swap(+ss[0], +ss[1], +tt[0], +tt[1]);
      if (r && r.ok) { const tmp = mirror.get(s); mirror.set(s, mirror.get(t)); mirror.set(t, tmp); swaps++; }
      else fails++;
      await breathe();
    };

    // phase 1: vacate target cells occupied by a different key (empty cells as buffers)
    const vacants = board.empties.map((e) => e.col + ':' + e.row);
    for (const t of targetKey.keys()) {
      if (state.stop || moves + swaps >= cap) break;
      const cur = mirror.get(t);
      if (!cur || cur.key === targetKey.get(t)) continue;
      if (!vacants.length) continue;
      const v = vacants.pop();
      await mv(t, v);
      if (mirror.get(v)) vacants.push(t); else vacants.push(v);
    }

    // phase 2: place one item of each key into its block (any same-key item is equivalent)
    const placed = new Set();
    const findItem = (key) => {
      for (const [ck, v] of mirror) {
        if (v && v.key === key && !placed.has(ck)) return ck;
      }
      return null;
    };
    for (const p of plan) {
      for (const c of p.cells) {
        if (state.stop || moves + swaps >= cap) break;
        const t = c.col + ':' + c.row;
        const cur = mirror.get(t);
        if (cur && cur.key === p.key) { placed.add(t); continue; }
        const s = findItem(p.key);
        if (!s) { fails++; continue; }
        if (!cur) await mv(s, t); else await sw(s, t);
        if (mirror.get(t) && mirror.get(t).key === p.key) placed.add(t);
      }
    }

    log('sort: mv ' + moves + ' · sw ' + swaps + ' · fail ' + fails +
      (moves + swaps >= cap ? ' (cap)' : ''));
    stats.moved += moves;
    stats.swapped += swaps;
    stats.failed += fails;
  }

  // ── HARVEST: tap every READY harvestable via the game's own tap path ─────
  // Uses the game's own harvest machinery directly:
  //   HARVEST  — adding a LootReceived behavior triggers the game's harvest
  //              service (_onLootReceived → _handleHarvest): hp -1, cooldown,
  //              and the produced item becomes a lootable bubble. Plain
  //              tapRouter._simulateClick does NOT harvest (verified: crops
  //              tapped that way never lose hp or produce loot).
  //   COLLECT  — one tap on a lootable harvestable (via _simulateClick) spawns
  //              the loot objects and consumes the crop (lootingRemovesObject).
  // The game processes each action on its next loop tick (~1 fps in background
  // tabs), so the collect phase iterates with settle delays until no lootables
  // remain instead of a single sweep that can race the game.
  // Readiness = no cooldown entry in the tile save model (the game writes it
  // on harvest) + hitpoints remaining.
  function getTapRouter(S) {
    // The subscriber list is rebuilt by the game as it spawns/destroys
    // subsystems, so never trust index 0 — find any context with _simulateClick.
    try {
      const subs = S.interactionService.onGestureTap._subscribers;
      for (const s of subs || []) {
        if (s && s.context && typeof s.context._simulateClick === 'function') return s.context;
      }
    } catch (e) {}
    return null;
  }

  // Entities WE already sent LootReceived to this session, with the send time.
  // The game writes the cooldown (tile entry / cooldown behavior / timer) only
  // when it processes the queued behavior (~1 tick per item; ~1s/tick in a
  // hidden tab), so a second Harvest run within that lag window would otherwise
  // re-harvest cooling crops — the direct LootReceived path does not gate on
  // cooldown itself. Skip anything harvested within LAG_WINDOW ms.
  const pendingHarvests = new Map();
  const LAG_WINDOW = 6000;

  let lootReceivedCtor = null;
  function findLootReceivedCtor() {
    if (lootReceivedCtor) return true;
    // the trigger module (MergeTrigger, __FMV_hcId) also exports LootReceived /
    // LootTrigger — an instance whose type is 'lootReceived' identifies it
    const scan = (ex) => {
      if (!ex || typeof ex !== 'object') return null;
      for (const k of Object.keys(ex)) {
        const v = ex[k];
        if (typeof v !== 'function' || !v.prototype) continue;
        try {
          const inst = new v({});
          if (inst && inst.type === 'lootReceived') return v;
        } catch (e) {}
      }
      return null;
    };
    try { lootReceivedCtor = scan(window.FMV.req(window.__FMV_hcId)); } catch (e) {}
    return !!lootReceivedCtor;
  }

  async function harvestAll() {
    assertFMV();
    const S = window.FMV.services();
    const I = window.FMV.I();
    const tapRouter = getTapRouter(S);
    if (!tapRouter || typeof tapRouter._simulateClick !== 'function') {
      throw new Error('tap router not found — game version changed?');
    }
    const hasHarvestTrigger = findLootReceivedCtor();
    let tiles = null;
    try { tiles = window.FMV.rootServices().playerData._dataContainers['0']._data; } catch (e) {}
    const tileAt = (col, row) => {
      if (!tiles) return null;
      try {
        const m = tiles['TilesStateModel_' + col + ':' + row];
        return m && m.data && m.data.state ? m.data.state.data : null;
      } catch (e) { return null; }
    };

    // phase 1: harvest every READY harvestable — no tile-model cooldown
    // (respects the game's wait between harvests), not lootable, hp remaining,
    // and not already sent LootReceived within the pending-harvest lag window
    // (the game writes the cooldown only after it processes the queued add).
    let harvested = 0, cooling = 0, depleted = 0;
    if (hasHarvestTrigger) {
      for (const cell of S.mapGrid._cells.values()) {
        if (state.stop) break;
        if (!cell || !cell.content) continue;
        const e = cell.content;
        if (!e.hasBehavior || !e.hasBehavior(I.Harvestable)) continue;
        if (e.hasBehavior(I.Lootable)) continue;
        const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
        if (hp && typeof hp.current === 'number' && hp.current <= 0) { depleted++; continue; }
        if (tileAt(cell.column, cell.row) && tileAt(cell.column, cell.row).cooldown) { cooling++; continue; }
        const pendingAt = pendingHarvests.get(e);
        if (pendingAt && Date.now() - pendingAt < LAG_WINDOW) { cooling++; continue; }
        if (S.mapGrid.getCell(cell.column, cell.row).content !== e) continue;
        try { e.addBehavior(new lootReceivedCtor({})); pendingHarvests.set(e, Date.now()); harvested++; } catch (e2) {
          log('harvest fail ' + cell.column + ':' + cell.row, 'warn');
        }
        if (harvested % 20 === 0) await sleep(0);
      }
      if (pendingHarvests.size > 64) {
        const now = Date.now();
        for (const [k, t] of pendingHarvests) {
          if (now - t > LAG_WINDOW) pendingHarvests.delete(k);
        }
      }
    }

    // phase 2: collect — keep tapping lootable harvestables (the harvest
    // results, plus leftovers) and GROUND COLLECTABLES (the produced items
    // that land on empty cells as bubbles) until none remain; each round
    // waits for the game to process the previous round's actions. The settle
    // time adapts to the game's measured tick rate (~66ms when visible,
    // ~1000ms when throttled in a background tab) so visible runs are fast.
    let collected = 0;
    let ground = 0;
    let failed = 0;
    // only tap collectables whose reward is a real board item (blueprint) —
    // coin/gem/energy reward bubbles are not harvest products and must not be
    // clicked
    const isProductCollectable = (e) => {
      try {
        const cb = e.getBehavior(I.Collectable);
        const r = cb && cb._data && cb._data.reward;
        if (!r || !r[0] || !r[0].key) return false;
        return window.FMV.rootServices().blueprintCollection.hasBlueprint(r[0].key);
      } catch (err) { return false; }
    };
    const settle = await adaptSettle(150, 1500);
    if (harvested > 0) await sleep(settle);
    for (let round = 0; round < 6 && !state.stop; round++) {
      const lootables = [];
      const collectables = [];
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell || !cell.content) continue;
        const e = cell.content;
        if (!e.hasBehavior || !e.hasBehavior(I.Collectable)) {
          if (!e.hasBehavior(I.Harvestable)) continue;
          if (!e.hasBehavior(I.Lootable)) continue;
        }
        const rec = { e: e, col: cell.column, row: cell.row };
        if (e.hasBehavior(I.Lootable)) lootables.push(rec);
        else if (isProductCollectable(e)) collectables.push(rec);
      }
      if (!lootables.length && !collectables.length) break;
      // guard: the board shifts while we run — never tap a stale reference
      // (the entity may have moved or been consumed; tapping it would hit
      // whatever the game now resolves at that cell)
      const atCell = (r) => {
        const c = S.mapGrid.getCell(r.col, r.row);
        return c && c.content === r.e;
      };
      for (const r of lootables) {
        if (state.stop) break;
        if (!atCell(r) || !r.e.hasBehavior(I.Lootable)) { failed++; continue; }
        try { tapRouter._simulateClick(r.e); collected++; } catch (e2) { failed++; }
        if (collected % 20 === 0) await sleep(0);
      }
      for (const r of collectables) {
        if (state.stop) break;
        if (!atCell(r) || !r.e.hasBehavior(I.Collectable)) { failed++; continue; }
        try { tapRouter._simulateClick(r.e); ground++; } catch (e2) { failed++; }
        if (ground % 20 === 0) await sleep(0);
      }
      if (lootables.length + collectables.length > 0) await sleep(settle);
    }
    log('harvest: ' + harvested + ' harvest · ' + collected + ' loot · ' + ground + ' ground' +
      ' · ' + cooling + ' cd' +
      (depleted ? ' · ' + depleted + ' dep' : '') +
      (failed ? ' · ' + failed + ' fail' : '') +
      (hasHarvestTrigger ? '' : ' · no trigger'));
    stats.harvested += harvested;
    stats.lootCollected += collected;
    stats.groundCollected += ground;
    stats.failed += failed;
  }

  // ── ORDERS: claim finished orders, then start affordable orders ────────────
  // The live service uses numeric states: 1 = startable, 3 = complete.
  // startOrder/rewardOrder retain the game's inventory, timer, camera and save paths.
  async function orders() {
    assertFMV();
    const S = window.FMV.services();
    const O = S && S.ordersService;
    if (!O || typeof O.getCurrentOrders !== 'function' ||
        typeof O.startOrder !== 'function' || typeof O.rewardOrder !== 'function') {
      throw new Error('orders service not found — game version changed?');
    }

    const STARTABLE = 1;
    const COMPLETE = 3;
    let claimed = 0;
    let started = 0;
    let skipped = 0;
    const claiming = () => typeof O.isClaiming === 'function' ? O.isClaiming() : !!O.isClaiming;
    const waitForClaim = async () => {
      const deadline = Date.now() + 8000;
      while (!state.stop && claiming() && Date.now() < deadline) await sleep(100);
    };

    // Claim first so the reward animation can free the building and refresh its order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
      if (state.stop) break;
      if (!order || order.state !== COMPLETE) continue;
      O.rewardOrder(order.buildingID);
      await waitForClaim();
      claimed++;
    }

    // Re-read after claims because the service may replace a claimed order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
      if (state.stop) break;
      if (!order || order.state !== STARTABLE) continue;
      const affordable = typeof O._canAffordOrder === 'function' && O._canAffordOrder(order);
      if (!affordable) {
        skipped++;
        continue;
      }
      O.startOrder(order.buildingID);
      started++;
    }
    log('orders: +' + claimed + ' · +' + started + ' start' + (skipped ? ' · ' + skipped + ' skip' : ''));
    stats.ordersClaimed += claimed;
    stats.ordersStarted += started;
  }

  // ── Stop request: set the flag + give immediate UI feedback ──────────────
  function requestStop() {
    state.stop = true;
    log('stop — halting', 'warn');
    if (autoBtn && state.mode === 'auto-all') {
      autoBtn.textContent = 'Stopping…';
      autoBtn.disabled = true;
    }
  }

  // ── Board-full guard: when no empty cells are left, merge to free space ───
  async function freeBoardSpace() {
    assertFMV();
    if (state.stop) return false;
    const board = readBoard();
    if (board.error) throw new Error(board.error);
    if (board.empties.length > 0) return false;
    log('board full — merging', 'warn');
    await phasePlanMerge();
    return true;
  }

  // ── Auto All: runs every SELECTED automation loop in parallel. The checkboxes
  //    on the Auto tab choose which loops the master button starts; each loop
  //    stops on its own (clear: energy out etc.) or on the shared STOP.
  const prefs = { orders: true, clear: true, clearTree: true, clearRock: true, clearToolbox: true };
  try {
    const p = window.__FMV_prefs;
    if (p && typeof p === 'object') {
      prefs.orders = p.orders !== false;
      prefs.clear = p.clear !== false;
      prefs.clearTree = p.clearTree !== false;
      prefs.clearRock = p.clearRock !== false;
      prefs.clearToolbox = p.clearToolbox !== false;
    }
  } catch (e) {}
  function savePrefs() {
    try {
      window.__FMV_prefs = {
        orders: !!prefs.orders, clear: !!prefs.clear,
        clearTree: !!prefs.clearTree, clearRock: !!prefs.clearRock,
        clearToolbox: !!prefs.clearToolbox
      };
    } catch (e) {}
  }

  // ── auto-orders loop body: claim + start orders; plan+merge when full ────
  async function runOrdersLoop() {
    let cycle = 0;
    while (state.running && !state.stop) {
      cycle++;
      log('orders cyc ' + cycle, 'ok');
      await orders();
      await freeBoardSpace();
      const wait = await adaptSettle(150, 1500) * 8;
      const deadline = Date.now() + Math.min(ORDERS_WAIT_MS, Math.max(1500, wait));
      while (!state.stop && Date.now() < deadline) await sleep(250);
    }
  }

  // ── auto-clear loop body: one pass per cycle. Transient blockers (energy
  //    out, no free workers — both recover over time) only pause the loop;
  //    it waits and retries until energy regenerates / workers free up.
  //    Permanent blockers (board full, nothing ready) auto-off.
  async function runClearLoop() {
    let cycle = 0;
    let waiting = false;
    while (state.running && !state.stop) {
      cycle++;
      log('clear cyc ' + cycle, 'ok');
      const reason = await clearOnce();
      if (reason === 'not focused' || reason === 'no tap services') {
        // game paused (background tab) or tap services mid-rebuild — wait and retry
        await sleep(1000);
        continue;
      }
      if (reason === 'energy out' || reason === 'collected only' || reason === 'no free workers') {
        if (!waiting) log('clear: ' + reason + ' — waiting', 'warn');
        waiting = true;
        const deadline = Date.now() + CLEAR_WAIT_MS * 3;
        while (!state.stop && Date.now() < deadline) await sleep(1000);
        continue;
      }
      waiting = false;
      if (reason) {
        log('clear stop: ' + reason, 'warn');
        break;
      }
      const wait = await adaptSettle(150, 1500) * 4;
      const deadline = Date.now() + Math.min(CLEAR_WAIT_MS, Math.max(1000, wait));
      while (!state.stop && Date.now() < deadline) await sleep(250);
    }
  }

  async function autoAll() {
    if (state.running) { requestStop(); return; }
    if (state.busy) return;
    const selected = [];
    if (prefs.orders) selected.push(runOrdersLoop);
    if (prefs.clear) {
      if (!prefs.clearTree && !prefs.clearRock && !prefs.clearToolbox) {
        log('clear: no source types selected — tick Tree/Rock/Toolbox', 'warn');
      } else {
        selected.push(runClearLoop);
      }
    }
    if (!selected.length) { log('no automation selected — tick the boxes', 'warn'); return; }
    state.running = true;
    state.mode = 'auto-all';
    state.stop = false;
    state.rounds = 0;
    state.opStart = Date.now();
    setUI();
    try {
      await Promise.all(selected.map(function (fn) { return fn(); }));
    } catch (e) {
      log('ERR: ' + (e && e.message ? e.message : e), 'err');
    }
    state.running = false;
    state.mode = null;
    state.stop = false;
    state.opStart = null;
    setUI();
    refreshStatus();
    log('auto all off');
  }

  // ── CLEAR: spend energy on tree/rock/toolbox sources by driving the game's
  //    own tap functions directly (payment service + lootable collector — no
  //    click simulation): pay deducts energy and turns the source lootable,
  //    collect spawns the loot and damages hp. Cheapest taps first.
  //    Returns a stop reason (or null to keep going): energy out / no free
  //    workers / board full (drops need space — avoid burning energy when the
  //    board can't take the loot).
  // ── tap services discovery (cached) ─────────────────────────────────────
  // The game's own source-tap machinery: a resource-gate payment service
  // (_attemptPayment: checks workers, deducts energy, turns the source
  // lootable) and a lootable collector (_onInteractionAdded: spawns the loot
  // objects and damages hp). Both are reachable through the entity
  // behavior-family registries on onBehaviorAdded; calling them directly IS
  // the game's own tap action (no popouts, no click simulation).
  let paySvc = null, lootSvc = null;
  function findTapServices() {
    if (paySvc && lootSvc) return true;
    let pay = null, loot = null;
    const S = window.FMV.services();
    outer:
    for (const cell of S.mapGrid._cells.values()) {
      if (!cell || !cell.content) continue;
      let ev = null;
      try { ev = cell.content.onBehaviorAdded; } catch (e) { continue; }
      if (!ev || !ev._subscribers) continue;
      for (let i = 0; i < ev._subscribers.length; i++) {
        const reg = ev._subscribers[i].context;
        if (!reg || !reg.onGameObjectAdded || !reg._filter) continue;
        let types = null;
        try { types = reg._filter._behaviorTypes; } catch (e) {}
        if (!types || !Array.isArray(types)) continue;
        let sub = null;
        try { sub = reg.onGameObjectAdded._subscribers[0].context; } catch (e) { continue; }
        if (!sub) continue;
        if (!pay && typeof sub._attemptPayment === 'function') pay = sub;
        if (!loot && types.indexOf('interactionTap') !== -1 && types.indexOf('lootable') !== -1 &&
            typeof sub._onInteractionAdded === 'function') loot = sub;
        if (pay && loot) break outer;
      }
    }
    paySvc = pay;
    lootSvc = loot;
    return !!(pay && loot);
  }

  async function clearOnce() {
    assertFMV();
    // The game pauses its main loop while the tab is hidden, so taps would
    // queue up and all fire at once on refocus — never tap while hidden.
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return 'not focused';
    const S = window.FMV.services();
    const I = window.FMV.I();
    if (!findTapServices()) return 'no tap services';
    const tapRouter = getTapRouter(S);
    if (!tapRouter || typeof tapRouter._simulateClick !== 'function') return 'no router';
    let tiles = null;
    try { tiles = window.FMV.rootServices().playerData._dataContainers['0']._data; } catch (e) {}
    const readEnergy = () => {
      try {
        const v = window.FMV.rootServices().inventory.getAmount('energy');
        return typeof v === 'number' ? v : Number(v) || 0;
      } catch (e) { return 0; }
    };

    // scan: sources = hp>0, not cooling, not lootable (payable) | lootables (collect)
    const scan = () => {
      const cands = [];
      const lootables = [];
      let empties = 0;
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell) continue;
        if (!cell.content) { empties++; continue; }
        const e = cell.content;
        let id = null;
        try { id = e.getObjectIdAndTier().id; } catch (e2) {}
        if (!id || !(prefs.clearTree && id === 'tree' ||
                     prefs.clearRock && id === 'rock' ||
                     prefs.clearToolbox && id === 'toolbox')) continue;
        const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
        if (!hp || typeof hp.current !== 'number' || hp.current <= 0) continue;
        let tile = null;
        if (tiles) {
          try {
            const m = tiles['TilesStateModel_' + cell.column + ':' + cell.row];
            tile = m && m.data && m.data.state ? m.data.state.data : null;
          } catch (e2) {}
        }
        if (tile && tile.cooldown) continue;
        if (tile && tile.lootable) {
          lootables.push({ entity: e, col: cell.column, row: cell.row });
          continue;
        }
        const rg = e.hasBehavior(I.ResourceGate) ? e.getBehavior(I.ResourceGate) : null;
        let cost = null;
        try { if (rg && Array.isArray(rg.cost) && rg.cost[0]) cost = Number(rg.cost[0].amount); } catch (e2) {}
        if (!Number.isFinite(cost)) continue;
        let workers = 1;
        try { workers = Number(rg.workers) || 1; } catch (e2) {}
        cands.push({ entity: e, cost: cost, workers: workers, col: cell.column, row: cell.row });
      }
      return { cands, lootables, empties };
    };

    let { cands, lootables, empties } = scan();
    if (empties === 0) return 'board full';

    // 1) collect pending loot first (free — frees sources for the next payment)
    let collected = 0;
    for (const l of lootables) {
      if (state.stop) break;
      try { lootSvc._onInteractionAdded(l.entity); collected++; } catch (e2) {
        log('loot fail ' + l.col + ':' + l.row, 'warn');
        stats.failed++;
      }
      if (collected % 20 === 0) await sleep(0);
    }
    // the collected sources may be payable now — rescan before paying
    if (collected) ({ cands } = scan());
    if (!cands.length) return (collected ? 'collected only' : 'nothing ready');

    // 2) pay ready sources, cheapest first, until energy is too low
    cands.sort((a, b) => a.cost - b.cost);
    let energy = readEnergy();
    const energyBefore = energy;
    if (energy < cands[0].cost) {
      return (collected ? 'collected only' : 'energy out');
    }
    let tapped = 0;
    let noWorkers = 0;
    for (const c of cands) {
      if (state.stop) break;
      energy = readEnergy();
      if (energy < c.cost) break;
      let free = true;
      try { free = !!S.gameWorkers.hasEnoughWorkers(c.workers); } catch (e2) {}
      if (!free) { noWorkers++; continue; }
      try {
        await paySvc._attemptPayment(c.entity, 'fmv-' + c.col + ':' + c.row, c.entity.getBehavior(I.ResourceGate));
        try { lootSvc._onInteractionAdded(c.entity); } catch (e3) {}
        tapped++;
      } catch (e2) {
        log('pay fail ' + c.col + ':' + c.row, 'warn');
        stats.failed++;
      }
      if (tapped % 10 === 0) await sleep(0);
    }
    if (tapped === 0 && noWorkers > 0) return 'no free workers';

    // 3) collect ground collectables — produced items land on empty cells as
    //    bubbles (Collectable behavior) and need a tap to be picked up; only
    //    PRODUCT bubbles (reward key is a real blueprint) — coin/gem/energy
    //    reward bubbles are not ours to click. Guards stale references too.
    //    Iterates with adaptive settles until none remain (the loot lands on
    //    the game's next tick, so a single sweep can race it).
    let ground = 0;
    const isProductCollectable = (e) => {
      try {
        const cb = e.getBehavior(I.Collectable);
        const r = cb && cb._data && cb._data.reward;
        if (!r || !r[0] || !r[0].key) return false;
        return window.FMV.rootServices().blueprintCollection.hasBlueprint(r[0].key);
      } catch (err) { return false; }
    };
    if (tapped) await sleep(await adaptSettle(150, 1500));
    for (let round = 0; round < 4 && !state.stop; round++) {
      const found = [];
      for (const cell of S.mapGrid._cells.values()) {
        if (state.stop) break;
        if (!cell || !cell.content) continue;
        const e = cell.content;
        if (!e.hasBehavior || !e.hasBehavior(I.Collectable)) continue;
        if (!isProductCollectable(e)) continue;
        if (S.mapGrid.getCell(cell.column, cell.row).content !== e) continue;
        found.push(e);
      }
      if (!found.length) break;
      for (const e of found) {
        if (state.stop) break;
        try { tapRouter._simulateClick(e); ground++; } catch (e2) {}
        if (ground % 20 === 0) await sleep(0);
      }
      await sleep(await adaptSettle(150, 1500));
    }

    energy = readEnergy();
    stats.sourcesCleared += tapped;
    stats.energySpent += Math.max(0, energyBefore - energy);
    log('clear: ' + tapped + ' tap · ' + energy + ' energy' +
      (collected ? ' · ' + collected + ' loot' : '') +
      (ground ? ' · ' + ground + ' ground' : '') +
      (noWorkers ? ' · ' + noWorkers + ' noworkers' : '') +
      (state.stop ? ' · stop' : ''));
    return null;
  }

  // ── VISIT: auto-collect friend-reward bubbles ─────────────────────────────
  // Two tap paths exist, depending on whose farm is loaded:
  //   visitor path (a FRIEND's farm — you are the visitor): entities carry a
  //     VisitorAction behavior; the game taps them through the visitorAction
  //     family — the family simulator fires interactionHelper._createClick
  //     (the official pipeline), the processor _onActivityTapped adds your
  //     reward to visitorReward and registers the owner reward on the farm.
  //   owner path (YOUR farm — friends visited you): entities carry FriendReward;
  //     the game processes taps via the friendReward family (_onInteractionTap
  //     → _processReward).
  // The behavior-family registries are rebuilt when the farm changes, so the
  // discovery runs fresh on every call (never cache across farms). The
  // visitorReward service is a stable root singleton and is cached.
  let visitRewardSvc = null;
  function findVisitServices() {
    const S = window.FMV.services();
    if (!S) return false;
    const ctx = { visitorProc: null, visitorSim: null, ownerProc: null, ownerSim: null };
    outer:
    for (const cell of S.mapGrid._cells.values()) {
      if (!cell || !cell.content) continue;
      let ev = null;
      try { ev = cell.content.onBehaviorAdded; } catch (e) { continue; }
      if (!ev || !ev._subscribers) continue;
      for (let i = 0; i < ev._subscribers.length; i++) {
        const reg = ev._subscribers[i].context;
        if (!reg || !reg.onGameObjectAdded || !reg._filter) continue;
        let types = null;
        try { types = reg._filter._behaviorTypes; } catch (e) {}
        if (!types || !Array.isArray(types)) continue;
        const isVisitor = types.indexOf('visitorAction') !== -1;
        const isOwner = types.indexOf('friendReward') !== -1;
        if (!isVisitor && !isOwner) continue;
        let sub = null;
        try { sub = reg.onGameObjectAdded._subscribers[0].context; } catch (e) { continue; }
        if (!sub) continue;
        if (isVisitor) {
          if (!ctx.visitorProc && typeof sub._onActivityTapped === 'function' && typeof sub._createVisitorReward === 'function') ctx.visitorProc = sub;
          if (!ctx.visitorSim && typeof sub._simulateClick === 'function') ctx.visitorSim = sub;
        }
        if (isOwner) {
          if (!ctx.ownerProc && typeof sub._onInteractionTap === 'function' && typeof sub._processReward === 'function') ctx.ownerProc = sub;
          if (!ctx.ownerSim && typeof sub._simulateClick === 'function') ctx.ownerSim = sub;
        }
        if (ctx.visitorProc && ctx.visitorSim && ctx.ownerProc && ctx.ownerSim) break outer;
      }
    }
    window.__FMV_visitCtx = ctx;
    if (visitRewardSvc) return !!(ctx.visitorProc || ctx.visitorSim || ctx.ownerProc || ctx.ownerSim);
    const R = window.FMV.rootServices();
    const seen = new Set();
    const search = (obj, depth) => {
      if (!obj || depth > 6 || visitRewardSvc) return;
      try {
        for (const k of Object.keys(obj)) {
          let v;
          try { v = obj[k]; } catch (e) { continue; }
          if (v && typeof v === 'object' && !seen.has(v)) {
            seen.add(v);
            if (typeof v.hasRewards === 'function' && v._model && Array.isArray(v._model._rewards)) {
              visitRewardSvc = v;
              return;
            }
            search(v, depth + 1);
          }
        }
      } catch (e) {}
    };
    search(R, 0);
    return !!(ctx.visitorProc || ctx.visitorSim || ctx.ownerProc || ctx.ownerSim);
  }

  // which tap path applies to an entity = which family registry is attached to
  // its onBehaviorAdded event (the game routes taps the same way)
  function isVisitorEntity(e) {
    try {
      const ev = e.onBehaviorAdded;
      if (!ev || !ev._subscribers) return false;
      for (let i = 0; i < ev._subscribers.length; i++) {
        const reg = ev._subscribers[i].context;
        if (!reg || !reg._filter) continue;
        let types = null;
        try { types = reg._filter._behaviorTypes; } catch (e2) {}
        if (!types || !Array.isArray(types) || types.indexOf('visitorAction') === -1) continue;
        return true;
      }
    } catch (e2) {}
    return false;
  }

  async function collectVisits() {
    assertFMV();
    if (!findVisitServices()) throw new Error('friend reward services not found — game version changed?');
    const S = window.FMV.services();
    const I = window.FMV.I();
    const C = window.__FMV_visitCtx;
    let processed = 0, visitorTaps = 0, ownerTaps = 0, failed = 0;
    for (let round = 0; round < 6 && !state.stop; round++) {
      const cands = [];
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell || !cell.content) continue;
        const e = cell.content;
        let va = false, fr = false;
        try { va = e.hasBehavior(I.VisitorAction); } catch (e2) {}
        try { fr = e.hasBehavior(I.FriendReward); } catch (e2) {}
        if (!va && !fr) continue;
        // visitor path (a friend's farm): live = the action behavior is present
        // (the tap consumes VisitorAction; a lingering FriendReward is spent)
        if (isVisitorEntity(e) && !va) continue;
        cands.push({ e: e, col: cell.column, row: cell.row, visitor: va || isVisitorEntity(e) });
      }
      if (!cands.length) break;
      for (const c of cands) {
        if (state.stop) break;
        const cell = S.mapGrid.getCell(c.col, c.row);
        if (!cell || cell.content !== c.e) { failed++; continue; }
        let va = false, fr = false;
        try { va = c.e.hasBehavior(I.VisitorAction); } catch (e2) {}
        try { fr = c.e.hasBehavior(I.FriendReward); } catch (e2) {}
        if (c.visitor) {
          if (!va) continue;
        } else if (!fr) {
          failed++;
          continue;
        }
        try {
          if (c.visitor) {
            if (C.visitorSim) C.visitorSim._simulateClick(c.e);
            else if (C.visitorProc) C.visitorProc._onActivityTapped(c.e);
            else throw new Error('no visitor tap handler');
            visitorTaps++;
          } else {
            if (C.ownerProc && typeof C.ownerProc._onInteractionTap === 'function') C.ownerProc._onInteractionTap(c.e);
            else if (C.ownerProc) C.ownerProc._processReward(c.e);
            else if (C.ownerSim) C.ownerSim._simulateClick(c.e);
            else throw new Error('no owner tap handler');
            ownerTaps++;
          }
          processed++;
        } catch (e2) { failed++; }
        if (processed % 20 === 0) await sleep(0);
      }
      await sleep(await adaptSettle(150, 1500));
    }
    let claimed = 0;
    if (visitRewardSvc) {
      // the reward pipeline lands asynchronously (a few ticks after the tap) —
      // settle before reading the pending list so nothing is missed
      await sleep(await adaptSettle(150, 1500));
      try {
        const m = visitRewardSvc._model;
        if (m && Array.isArray(m._rewards)) claimed = m._rewards.length;
        if (claimed && typeof visitRewardSvc._addRewardsToInventory === 'function') {
          visitRewardSvc._addRewardsToInventory();
        } else if (claimed && typeof visitRewardSvc.showNewRewards === 'function') {
          visitRewardSvc.showNewRewards();
        }
      } catch (e2) {}
    }
    stats.friendRewards += processed;
    log('visit: ' + processed + ' bubble' +
      (visitorTaps ? ' · ' + visitorTaps + ' visitor' : '') +
      (ownerTaps ? ' · ' + ownerTaps + ' owner' : '') +
      (claimed ? ' · ' + claimed + ' reward' : '') +
      (failed ? ' · ' + failed + ' fail' : ''));
  }

  // ── one-shot op wrapper ──────────────────────────────────────────────────
  async function runOp(fn) {
    if (state.busy || state.running) return;
    state.busy = true;
    state.stop = false;
    state.rounds = 0;
    state.opStart = Date.now();
    setUI();
    try { await fn(); } catch (e) { log('ERR: ' + (e && e.message ? e.message : e), 'err'); }
    state.busy = false;
    state.opStart = null;
    if (state.stop) log('stopped', 'warn');
    setUI();
    refreshStatus();
  }

  // ── UI ───────────────────────────────────────────────────────────────────
  let dot, autoBtn, sortBtn, fillBtn, harvestBtn, planBtn, orderBtn, analyzeBtn, clearBtn, visitBtn;
  let chkOrders, chkClear, chkTree, chkRock, chkToolbox, clearTypesRow;
  function refreshStatus() {
    const el = document.getElementById('fmv-status');
    if (!el) return;
    try {
      assertFMV();
      const b = readBoard();
      const crates = cratesLeft();
      const items = b.error ? '-' : b.items.length;
      const empty = b.error ? '-' : b.empties.length;
      let extra = '';
      if (state.running || state.busy) {
        if (state.rounds) extra += ' · r' + state.rounds;
        if (state.opStart) extra += ' · ' + Math.floor((Date.now() - state.opStart) / 1000) + 's';
      }
      el.textContent = 'items ' + items + ' · empty ' + empty + ' · crates ' + crates + extra;
      el.className = 'status' + (b.error ? ' err' : '');
    } catch (e) {
      el.textContent = 'FMV not ready — re-run install_menu.mjs';
      el.className = 'status err';
    }
  }
  function setUI() {
    if (!dot) return;
    dot.className = 'dot' + (state.running || state.busy ? ' busy' : '');
    autoBtn.textContent = state.mode === 'auto-all' ? '■ STOP' : '▶ Auto All';
    const dis = state.busy || state.running;
    autoBtn.disabled = dis && state.mode !== 'auto-all';
    if (chkOrders) chkOrders.disabled = dis;
    if (chkClear) chkClear.disabled = dis;
    if (chkTree) chkTree.disabled = dis;
    if (chkRock) chkRock.disabled = dis;
    if (chkToolbox) chkToolbox.disabled = dis;
    sortBtn.disabled = dis;
    fillBtn.disabled = dis;
    harvestBtn.disabled = dis;
    planBtn.disabled = dis;
    orderBtn.disabled = dis;
    if (clearBtn) clearBtn.disabled = dis;
    if (visitBtn) visitBtn.disabled = dis;
  }

  function buildUI() {
    const oldMenu = document.getElementById('fmv-menu');
    if (oldMenu) oldMenu.remove();
    const oldStyle = document.getElementById('fmv-menu-style');
    if (oldStyle) oldStyle.remove();
    const style = document.createElement('style');
    style.id = 'fmv-menu-style';
    style.textContent = '#fmv-menu{position:fixed;top:12px;right:12px;z-index:2147483647;width:244px;'
      + 'background:rgba(13,14,22,.82);color:#d7d7e0;font:10px/1.35 ui-monospace,Consolas,monospace;'
      + 'border:1px solid rgba(130,150,255,.18);border-radius:9px;box-shadow:0 8px 32px rgba(0,0,0,.55);'
      + 'user-select:none;overflow:hidden;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);}'
      + '#fmv-menu .head{display:flex;align-items:center;gap:5px;padding:4px 8px;cursor:move;touch-action:none;'
      + 'background:linear-gradient(180deg,rgba(255,255,255,.05),transparent);}'
      + '#fmv-menu .title{font-weight:700;font-size:10.5px;color:#9ad0ff;flex:1;letter-spacing:.3px;}'
      + '#fmv-menu .fold{color:#7a7a88;font-size:10px;}'
      + '#fmv-menu .dot{width:6px;height:6px;border-radius:50%;background:#3d3;box-shadow:0 0 6px #3d3;}'
      + '#fmv-menu .dot.busy{background:#fa0;animation:pulse 1s infinite;}'
      + '@keyframes pulse{50%{opacity:.35}}'
      + '#fmv-menu .body{padding:5px 6px 6px;}'
      + '#fmv-menu .status{padding:2px 5px;background:rgba(255,255,255,.04);border-radius:5px;'
      + 'margin-bottom:5px;color:#9a9aa8;font-size:9px;}'
      + '#fmv-menu .status.err{color:#ff9a9a;}'
      + '#fmv-menu .btns{display:grid;grid-template-columns:repeat(4,1fr);gap:3px;margin-bottom:5px;}'
      + '#fmv-menu .btns > *:only-child{grid-column:1 / -1;}'
      + '#fmv-menu .btns:last-of-type{margin-bottom:0;}'
      + '#fmv-menu .tabs{display:flex;gap:10px;margin-bottom:5px;padding:0 2px;'
      + 'border-bottom:1px solid rgba(130,150,255,.12);}'
      + '#fmv-menu .tabs button{flex:1;font:inherit;padding:2px 0 4px;border:none;border-radius:0;'
      + 'background:none;color:#8a8a99;cursor:pointer;transition:color .15s;}'
      + '#fmv-menu .tabs button:hover:not(:disabled){background:none;color:#b8c8e8;}'
      + '#fmv-menu .tabs button.on{color:#9ad0ff;box-shadow:inset 0 -2px 0 #9ad0ff;}'
      + '#fmv-menu button{flex:1;font:inherit;padding:3px 0;border:1px solid rgba(130,150,255,.14);'
      + 'border-radius:5px;background:rgba(255,255,255,.05);color:#e8e8f0;cursor:pointer;'
      + 'transition:background .15s,transform .05s;}'
      + '#fmv-menu button.toggle{background:rgba(90,220,140,.06);border-color:rgba(90,220,140,.22);'
      + 'font-weight:600;}'
      + '#fmv-menu button.toggle:hover:not(:disabled){background:rgba(90,220,140,.14);}'
      + '#fmv-menu .chks{display:flex;flex-direction:column;gap:2px;margin-bottom:5px;}'
      + '#fmv-menu .chk{display:flex;align-items:center;gap:6px;padding:2px 5px;border-radius:5px;'
      + 'cursor:pointer;color:#c8c8d4;user-select:none;}'
      + '#fmv-menu .chk:hover{background:rgba(255,255,255,.05);}'
      + '#fmv-menu .chk input{accent-color:#5adc8c;margin:0;cursor:pointer;}'
      + '#fmv-menu .chk input:disabled{cursor:default;opacity:.5;}'
      + '#fmv-menu .chk input:disabled + span{opacity:.5;}'
      + '#fmv-menu .chkrow{display:flex;align-items:center;gap:8px;padding:1px 5px 4px 23px;}'
      + '#fmv-menu .chkmini{display:flex;align-items:center;gap:3px;font-size:9px;color:#9a9aa8;'
      + 'cursor:pointer;user-select:none;}'
      + '#fmv-menu .chkmini input{accent-color:#5adc8c;margin:0;cursor:pointer;}'
      + '#fmv-menu .chkmini input:disabled{cursor:default;opacity:.5;}'
      + '#fmv-menu button:hover:not(:disabled){background:rgba(130,150,255,.16);}'
      + '#fmv-menu button:active:not(:disabled){transform:translateY(1px);}'
      + '#fmv-menu button:disabled{opacity:.4;cursor:default;}'
      + '#fmv-menu .logwrap{position:relative;}'
      + '#fmv-menu .log{height:16px;overflow:hidden;scrollbar-width:thin;background:rgba(0,0,0,.32);'
      + 'border:1px solid rgba(255,255,255,.06);border-radius:5px;padding:1px 20px 1px 5px;'
      + 'font-size:8.5px;line-height:1.3;white-space:pre-wrap;word-break:break-word;margin-top:5px;}'
      + '#fmv-menu .log.open{height:90px;overflow:auto;padding:2px 20px 2px 5px;}'
      + '#fmv-menu .log::-webkit-scrollbar{width:6px;}'
      + '#fmv-menu .log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px;}'
      + '#fmv-menu #fmv-log-toggle{position:absolute;top:2px;right:2px;width:16px;height:14px;padding:0;'
      + 'font-size:9px;line-height:1;border-radius:4px;background:rgba(255,255,255,.06);'
      + 'border:1px solid rgba(130,150,255,.15);color:#8a8a99;cursor:pointer;z-index:2;}'
      + '#fmv-menu .log.open + #fmv-log-toggle{right:10px;}'
      + '#fmv-menu #fmv-log-toggle:hover{background:rgba(130,150,255,.22);color:#e8e8f0;}'
      + '#fmv-menu .l{color:#b8b8c8;}#fmv-menu .l.warn{color:#ffd479;}'
      + '#fmv-menu .l.ok{color:#7ed67e;}#fmv-menu .l.err{color:#ff8f8f;}'
      + '#fmv-menu button.on{color:#9ad0ff;border-color:rgba(130,150,255,.35);}'
      + '#fmv-analyze{position:fixed;top:12px;right:264px;z-index:2147483647;width:700px;'
      + 'background:rgba(15,17,28,.9);color:#d7d7e0;font:11px/1.5 ui-monospace,Consolas,monospace;'
      + 'border:1px solid rgba(130,150,255,.22);border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.6);'
      + 'user-select:none;overflow:hidden;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);}'
      + '#fmv-analyze .head{display:flex;align-items:center;gap:8px;padding:10px 12px 8px;cursor:move;touch-action:none;'
      + 'background:linear-gradient(180deg,rgba(130,150,255,.08),transparent);}'
      + '#fmv-analyze .title{font-weight:700;font-size:13px;color:#9ad0ff;flex:1;letter-spacing:.3px;}'
      + '#fmv-analyze .sub{color:#6f6f82;font-size:10px;letter-spacing:.4px;}'
      + '#fmv-analyze .close{flex:none;width:20px;height:20px;padding:0;font-size:12px;line-height:1;'
      + 'border:1px solid rgba(130,150,255,.18);border-radius:6px;background:rgba(255,255,255,.06);'
      + 'color:#8a8a99;cursor:pointer;transition:background .15s;}'
      + '#fmv-analyze .close:hover{background:rgba(130,150,255,.22);color:#e8e8f0;}'
      + '#fmv-analyze .sec{color:#7c7c92;font-size:9.5px;padding:10px 12px 4px;letter-spacing:1px;text-transform:uppercase;}'
      + '#fmv-analyze .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:2px 12px 4px;}'
      + '#fmv-analyze .stat{display:flex;flex-direction:column;gap:2px;background:rgba(255,255,255,.04);'
      + 'border:1px solid rgba(130,150,255,.1);border-radius:8px;padding:7px 10px;}'
      + '#fmv-analyze .stat span{color:#8a8a99;font-size:9px;letter-spacing:.5px;text-transform:uppercase;}'
      + '#fmv-analyze .stat b{color:#eef0ff;font-size:15px;font-weight:700;line-height:1.2;}'
      + '#fmv-analyze .stat.fail{border-color:rgba(255,120,120,.25);}'
      + '#fmv-analyze .stat.fail b{color:#ff8f8f;}'
      + '#fmv-analyze .stat.warn b{color:#ffd479;}'
      + '#fmv-analyze .row{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:2px 12px;}'
      + '#fmv-analyze .row .dim{color:#8a8a99;font-size:10px;}'
      + '#fmv-analyze .tile{position:relative;aspect-ratio:1;display:flex;align-items:center;justify-content:center;'
      + 'border:1px solid rgba(130,150,255,.12);border-radius:8px;background:rgba(255,255,255,.04);'
      + 'transition:border-color .15s,background .15s;cursor:pointer;}'
      + '#fmv-analyze .tile:hover{border-color:rgba(130,150,255,.4);background:rgba(130,150,255,.1);}'
      + '#fmv-analyze .tile img.px{width:46px;height:46px;object-fit:contain;image-rendering:auto;pointer-events:none;}'
      + '#fmv-analyze .tile b{position:absolute;right:4px;bottom:3px;font-size:9.5px;font-weight:700;color:#fff;'
      + 'background:rgba(10,12,20,.8);border-radius:5px;padding:0 4px;line-height:1.6;pointer-events:none;}'
      + '#fmv-analyze .igrid{display:grid;grid-template-columns:repeat(8,1fr);gap:2px;padding:4px 12px 10px;}'
      + '#fmv-analyze .atabs{display:flex;gap:4px;padding:8px 12px 4px;}'
      + '#fmv-analyze .atab{flex:1;width:auto;padding:5px 0;font-size:10.5px;border:none;border-radius:7px;'
      + 'background:rgba(255,255,255,.04);color:#8a8a99;cursor:pointer;transition:background .15s,color .15s;}'
      + '#fmv-analyze .atab:hover{background:rgba(255,255,255,.08);color:#c8c8d5;}'
      + '#fmv-analyze .atab.on{background:rgba(130,150,255,.2);color:#cfe3ff;}'
      + '#fmv-analyze .apane{max-height:400px;overflow:auto;scrollbar-width:thin;padding-bottom:4px;}'
      + '#fmv-analyze .foot{padding:8px 12px 10px;}'
      + '#fmv-analyze button{font:inherit;width:100%;padding:6px 0;border:1px solid rgba(130,150,255,.16);'
      + 'border-radius:8px;background:rgba(255,255,255,.05);color:#e8e8f0;cursor:pointer;'
      + 'transition:background .15s,transform .05s;}'
      + '#fmv-analyze button:hover{background:rgba(130,150,255,.16);}'
      + '#fmv-analyze button:active{transform:translateY(1px);}'
      + '#input-field{display:none !important;}';
    document.head.appendChild(style);

    const el = document.createElement('div');
    el.id = 'fmv-menu';
    el.innerHTML = '<div class="head"><span class="dot" title="stop current op"></span><span class="title">FMV Bot v' + (window.FMV && window.FMV.version ? window.FMV.version : '?') + ' · weepingangel89</span>'
      + '<span class="fold">-</span></div>'
      + '<div class="body">'
      + '<div class="status" id="fmv-status">installing...</div>'
      + '<div class="tabs">'
      + '<button id="fmv-tab-farm" class="tab on">Farm</button>'
      + '<button id="fmv-tab-auto" class="tab">Auto</button>'
      + '<button id="fmv-tab-analyze" class="tab">Analyze</button>'
      + '</div>'
      + '<div class="tabpane" id="fmv-pane-farm">'
      + '<div class="btns">'
      + '<button id="fmv-fill">▦ Fill</button>'
      + '<button id="fmv-plan">◆ Merge</button>'
      + '<button id="fmv-harvest">✦ Harvest</button>'
      + '<button id="fmv-sort">⇅ Sort</button>'
      + '</div>'
      + '<div class="btns">'
      + '<button id="fmv-orders">⚑ Orders</button>'
      + '<button id="fmv-clear-once">⛏ Clear</button>'
      + '<button id="fmv-visit">☕ Visit</button>'
      + '</div>'
      + '</div>'
      + '<div class="tabpane" id="fmv-pane-auto" style="display:none">'
      + '<div class="chks">'
      + '<label class="chk" title="claim + start orders in a loop"><input type="checkbox" id="fmv-chk-orders"' + (prefs.orders ? ' checked' : '') + '><span>Auto Orders</span></label>'
      + '<label class="chk" title="spend energy clearing sources"><input type="checkbox" id="fmv-chk-clear"' + (prefs.clear ? ' checked' : '') + '><span>Auto Clear</span></label>'
      + '</div>'
      + '<div class="chkrow" id="fmv-clear-types"' + (prefs.clear ? '' : ' style="display:none"') + '>'
      + '<label class="chkmini"><input type="checkbox" id="fmv-chk-tree"' + (prefs.clearTree ? ' checked' : '') + '><span>Tree</span></label>'
      + '<label class="chkmini"><input type="checkbox" id="fmv-chk-rock"' + (prefs.clearRock ? ' checked' : '') + '><span>Rock</span></label>'
      + '<label class="chkmini"><input type="checkbox" id="fmv-chk-toolbox"' + (prefs.clearToolbox ? ' checked' : '') + '><span>Toolbox</span></label>'
      + '</div>'
      + '<div class="btns">'
      + '<button id="fmv-auto-all" class="toggle">▶ Auto All</button>'
      + '</div>'
      + '</div>'
      + '<div class="logwrap">'
      + '<div class="log"></div>'
      + '<button id="fmv-log-toggle" title="expand log">▾</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(el);

    const oldPopup = document.getElementById('fmv-analyze');
    if (oldPopup) oldPopup.remove();
    if (window.__FMV_analyzeTimer) { clearInterval(window.__FMV_analyzeTimer); window.__FMV_analyzeTimer = null; }
    const popup = document.createElement('div');
    popup.id = 'fmv-analyze';
    popup.style.display = 'none';
    popup.innerHTML = '<div class="head"><span class="title">Analysis</span><span class="sub">session</span><button class="close" title="close">×</button></div>'
      + '<div class="atabs">'
      + '<button class="atab on" data-at="summary">Summary</button>'
      + '<button class="atab" data-at="items">Items</button>'
      + '</div>'
      + '<div class="apane" data-ap="summary">'
      + '<div class="sec">Production</div>'
      + '<div class="stats">'
      + '<div class="stat"><span>merges</span><b data-k="merged">0</b></div>'
      + '<div class="stat"><span>moves</span><b data-k="moved">0</b></div>'
      + '<div class="stat"><span>swaps</span><b data-k="swapped">0</b></div>'
      + '<div class="stat"><span>crates</span><b data-k="crates">0</b></div>'
      + '<div class="stat"><span>harvests</span><b data-k="harvested">0</b></div>'
      + '<div class="stat"><span>loot picked</span><b data-k="lootCollected">0</b></div>'
      + '<div class="stat"><span>ground picked</span><b data-k="groundCollected">0</b></div>'
      + '<div class="stat"><span>visits</span><b data-k="friendRewards">0</b></div>'
      + '</div>'
      + '<div class="sec">Orders</div>'
      + '<div class="stats">'
      + '<div class="stat"><span>claimed</span><b data-k="ordersClaimed">0</b></div>'
      + '<div class="stat"><span>started</span><b data-k="ordersStarted">0</b></div>'
      + '</div>'
      + '<div class="sec">Clear</div>'
      + '<div class="stats">'
      + '<div class="stat"><span>sources</span><b data-k="sourcesCleared">0</b></div>'
      + '<div class="stat"><span>energy spent</span><b data-k="energySpent">0</b></div>'
      + '</div>'
      + '<div class="sec">Misc</div>'
      + '<div class="stats">'
      + '<div class="stat fail"><span>failures</span><b data-k="failed">0</b></div>'
      + '<div class="stat"><span>elapsed</span><b data-k="elapsed">0s</b></div>'
      + '</div>'
      + '</div>'
      + '<div class="apane" data-ap="items" style="display:none">'
      + '<div class="sec">Merges by item</div>'
      + '<div id="fmv-an-mrg" class="igrid"></div>'
      + '</div>'
      + '<div class="foot"><button id="fmv-analyze-reset">Reset</button></div>';
    document.body.appendChild(popup);
    const spriteUrlCache = new Map();
    const spriteURL = (k) => {
      if (spriteUrlCache.has(k)) return spriteUrlCache.get(k);
      let url = null;
      try {
        const tex = stats.mergedBy[k] && stats.mergedBy[k].tex;
        if (tex && tex.baseTexture && tex.baseTexture.resource && tex._frame) {
          const res = tex.baseTexture.resolution || 1;
          const f = tex._frame;
          const src = tex.baseTexture.resource.source;
          const cv = document.createElement('canvas');
          cv.width = Math.max(1, Math.round(f.width * res));
          cv.height = Math.max(1, Math.round(f.height * res));
          const ctx = cv.getContext('2d');
          ctx.drawImage(src, f.x * res, f.y * res, f.width * res, f.height * res, 0, 0, cv.width, cv.height);
          url = cv.toDataURL();
        }
      } catch (e2) {}
      spriteUrlCache.set(k, url);
      return url;
    };
    const renderItemList = (el, map) => {
      const entries = Object.entries(map).sort((a, b) => b[1].n - a[1].n);
      if (!entries.length) {
        el.innerHTML = '<div class="row"><span class="dim">— none yet —</span></div>';
        return;
      }
      el.innerHTML = entries.map(([k, v]) => {
        const url = spriteURL(k);
        const img = url ? '<img class="px" src="' + url + '" alt="' + k + '">' : '';
        return '<div class="tile" title="' + k + '">' + img + '<b>' + v.n + '</b></div>';
      }).join('');
    };
    const analyzeTick = () => {
      if (popup.style.display === 'none') return;
      for (const b of popup.querySelectorAll('b[data-k]')) {
        const k = b.getAttribute('data-k');
        if (k === 'elapsed') {
          const s = Math.floor((Date.now() - stats.startedAt) / 1000);
          b.textContent = Math.floor(s / 60) + ':' + ((s % 60) < 10 ? '0' : '') + (s % 60);
        } else {
          b.textContent = stats[k];
        }
      }
      renderItemList(popup.querySelector('#fmv-an-mrg'), stats.mergedBy);
    };
    window.__FMV_analyzeTimer = setInterval(analyzeTick, 2000);
    popup.querySelectorAll('.atab').forEach((t) => t.addEventListener('click', () => {
      popup.querySelectorAll('.atab').forEach((x) => x.classList.toggle('on', x === t));
      popup.querySelectorAll('.apane').forEach((p) => { p.style.display = p.getAttribute('data-ap') === t.getAttribute('data-at') ? '' : 'none'; });
    }));
    const closePopup = () => {
      popup.style.display = 'none';
      if (analyzeBtn) analyzeBtn.classList.remove('on');
    };
    const popupHead = popup.querySelector('.head');
    const popupClose = popup.querySelector('.close');
    popupClose.addEventListener('pointerdown', (e) => e.stopPropagation());
    popupClose.addEventListener('click', closePopup);
    popup.querySelector('#fmv-analyze-reset').addEventListener('click', () => { statsReset(); spriteUrlCache.clear(); analyzeTick(); });
    let popupDrag = false;
    popupHead.addEventListener('pointerdown', (e) => {
      popupDrag = true;
      const r = popup.getBoundingClientRect();
      popup.style.left = r.left + 'px';
      popup.style.top = r.top + 'px';
      popup.style.right = 'auto';
      popup.__offX = e.clientX - r.left;
      popup.__offY = e.clientY - r.top;
      try { popupHead.setPointerCapture(e.pointerId); } catch (e2) {}
    });
    popupHead.addEventListener('pointermove', (e) => {
      if (!popupDrag) return;
      e.preventDefault();
      popup.style.left = (e.clientX - popup.__offX) + 'px';
      popup.style.top = (e.clientY - popup.__offY) + 'px';
    });
    const endPopupDrag = (e) => {
      popupDrag = false;
      try { popupHead.releasePointerCapture(e.pointerId); } catch (e2) {}
    };
    popupHead.addEventListener('pointerup', endPopupDrag);
    popupHead.addEventListener('pointercancel', endPopupDrag);

    dot = el.querySelector('.dot');
    dot.title = 'stop current op';
    dot.style.cursor = 'pointer';
    dot.addEventListener('click', () => { if (window.FMV && window.FMV.menu) window.FMV.menu.stop(); });
    autoBtn = el.querySelector('#fmv-auto-all');
    chkOrders = el.querySelector('#fmv-chk-orders');
    chkClear = el.querySelector('#fmv-chk-clear');
    chkTree = el.querySelector('#fmv-chk-tree');
    chkRock = el.querySelector('#fmv-chk-rock');
    chkToolbox = el.querySelector('#fmv-chk-toolbox');
    clearTypesRow = el.querySelector('#fmv-clear-types');
    sortBtn = el.querySelector('#fmv-sort');
    fillBtn = el.querySelector('#fmv-fill');
    harvestBtn = el.querySelector('#fmv-harvest');
    planBtn = el.querySelector('#fmv-plan');
    orderBtn = el.querySelector('#fmv-orders');
    analyzeBtn = el.querySelector('#fmv-tab-analyze');
    clearBtn = el.querySelector('#fmv-clear-once');
    visitBtn = el.querySelector('#fmv-visit');
    logEl.current = el.querySelector('.log');
    const body = el.querySelector('.body');
    const fold = el.querySelector('.fold');
    const head = el.querySelector('.head');
    let dragMoved = false;
    head.addEventListener('pointerdown', (e) => {
      dragMoved = false;
      const r = el.getBoundingClientRect();
      el.style.left = r.left + 'px';
      el.style.top = r.top + 'px';
      el.style.right = 'auto';
      el.__offX = e.clientX - r.left;
      el.__offY = e.clientY - r.top;
      el.__dragging = true;
      try { head.setPointerCapture(e.pointerId); } catch (e2) {}
    });
    head.addEventListener('pointermove', (e) => {
      if (!el.__dragging) return;
      e.preventDefault();
      if (Math.abs(e.clientX - el.__offX - parseInt(el.style.left || '0', 10)) > 3 ||
          Math.abs(e.clientY - el.__offY - parseInt(el.style.top || '0', 10)) > 3) dragMoved = true;
      el.style.left = (e.clientX - el.__offX) + 'px';
      el.style.top = (e.clientY - el.__offY) + 'px';
    });
    const endDrag = (e) => {
      if (!el.__dragging) return;
      el.__dragging = false;
      try { head.releasePointerCapture(e.pointerId); } catch (e2) {}
    };
    head.addEventListener('pointerup', endDrag);
    head.addEventListener('pointercancel', endDrag);
    head.addEventListener('click', () => {
      if (dragMoved) return;
      body.style.display = body.style.display === 'none' ? '' : 'none';
      fold.textContent = body.style.display === 'none' ? '+' : '-';
    });
    autoBtn.addEventListener('click', autoAll);
    sortBtn.addEventListener('click', () => runOp(sortBoard));
    fillBtn.addEventListener('click', () => runOp(phaseFill));
    clearBtn.addEventListener('click', () => runOp(clearOnce));
    visitBtn.addEventListener('click', () => runOp(collectVisits));
    harvestBtn.addEventListener('click', () => runOp(harvestAll));
    planBtn.addEventListener('click', () => runOp(phasePlanMerge));
    orderBtn.addEventListener('click', () => runOp(orders));
    analyzeBtn.addEventListener('click', () => {
      const open = popup.style.display !== 'none';
      popup.style.display = open ? 'none' : '';
      analyzeBtn.classList.toggle('on', !open);
      analyzeTick();
    });
    const logToggle = el.querySelector('#fmv-log-toggle');
    logToggle.addEventListener('click', () => {
      const open = logEl.current.classList.toggle('open');
      logToggle.textContent = open ? '▴' : '▾';
      logToggle.title = open ? 'collapse log' : 'expand log';
      updateLogView();
    });
    const tabFarm = el.querySelector('#fmv-tab-farm');
    const tabAuto = el.querySelector('#fmv-tab-auto');
    const paneFarm = el.querySelector('#fmv-pane-farm');
    const paneAuto = el.querySelector('#fmv-pane-auto');
    const selectTab = (name) => {
      const isAuto = name === 'auto';
      paneFarm.style.display = isAuto ? 'none' : '';
      paneAuto.style.display = isAuto ? '' : 'none';
      tabFarm.classList.toggle('on', !isAuto);
      tabAuto.classList.toggle('on', isAuto);
    };
    tabFarm.addEventListener('click', () => selectTab('farm'));
    tabAuto.addEventListener('click', () => selectTab('auto'));
    const syncClearTypes = () => {
      clearTypesRow.style.display = chkClear.checked ? '' : 'none';
    };
    chkOrders.addEventListener('change', function () { prefs.orders = chkOrders.checked; savePrefs(); });
    chkClear.addEventListener('change', function () {
      prefs.clear = chkClear.checked;
      syncClearTypes();
      savePrefs();
    });
    chkTree.addEventListener('change', function () { prefs.clearTree = chkTree.checked; savePrefs(); });
    chkRock.addEventListener('change', function () { prefs.clearRock = chkRock.checked; savePrefs(); });
    chkToolbox.addEventListener('change', function () { prefs.clearToolbox = chkToolbox.checked; savePrefs(); });
  }

  // ── install ──────────────────────────────────────────────────────────────
  buildUI();
  window.FMV.menu = {
    orders: () => runOp(orders),
    sort: () => runOp(sortBoard),
    harvest: () => runOp(harvestAll),
    fill: () => runOp(phaseFill),
    planMerge: () => runOp(phasePlanMerge),
    autoOrders: () => { prefs.orders = true; prefs.clear = false; savePrefs(); return autoAll(); },
    autoClear: () => { prefs.orders = false; prefs.clear = true; savePrefs(); return autoAll(); },
    visit: () => runOp(collectVisits),
    autoAll,
    stop: () => {
      if (state.running || state.busy) requestStop();
    },
    status: refreshStatus,
    stats: () => {
      const counts = {};
      for (const k of Object.keys(stats.mergedBy)) counts[k] = stats.mergedBy[k].n;
      return { ...stats, mergedBy: counts, elapsed: Math.floor((Date.now() - stats.startedAt) / 1000) };
    },
    resetStats: statsReset,
    running: () => state.running,
    version: '1.5.3'
  };
  setUI();
  log('menu v' + window.FMV.version + ' installed', 'ok');
  refreshStatus();
  if (!window.__FMV_statusTimer) window.__FMV_statusTimer = setInterval(refreshStatus, 2500);
  return { ok: true };
})();`;
