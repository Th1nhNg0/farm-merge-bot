// In-game bot menu overlay (FMV Bot). Installed by install.mjs.
// All bot logic runs INSIDE the game frame — the menu buttons drive it:
//   [Fill]         spawn crates on every empty cell until the map is full
//   [Merge]        plan ALL groups (natural 5/10/15 + move/swap grouping)
//                  from one snapshot, then execute them in one batched pass
//   [Orders]       claim completed orders, then start every affordable order
//   [Auto Orders]  toggle loop: claim + start orders until board full / nothing
//                  to do; click again (or the dot) to stop
//   [Auto Clear]   toggle loop: clear tree/rock/toolbox as fast as possible
//                  (cooldowns skipped) until energy out / board full; click
//                  again (or the dot) to stop
//   [Auto Flash Deals] toggle loop: every cycle refresh the flash deals +
//                  buy the stock of the deal types ticked in the Cheat tab
//                  (ingredient/generator/material/chest/key/greenhouse);
//                  click again (or the dot) to stop
//   [Refresh]      update the items/empty/crates status line
// Exposes window.FMV.menu = { orders, fill, planMerge, autoOrders, autoClear,
//                             autoMarket, stop, status, running }.

// Shared planner (plan.js) and game-access helpers (util.js) are prepended to
// the injected source, so the menu IIFE below can use window.FMVPlan (same
// logic as the CLI scripts) and window.FMVUtil.
import { readFileSync } from "node:fs";

// The injected payload crosses CDP and is parsed in the game frame — every
// byte costs install time. Full-line comments (the only comment style in
// these sources: no block comments, no template literals — verified) are
// stripped from the embedded copies only; the annotated source files stay
// untouched.
const stripCommentLines = (s) =>
  s.split("\n").filter((line) => !/^\s*\/\//.test(line)).join("\n");

export const MENU_SOURCE = stripCommentLines(readFileSync(new URL("./plan.js", import.meta.url), "utf8"))
  + "\n" + stripCommentLines(readFileSync(new URL("./util.js", import.meta.url), "utf8"))
  + "\n" + stripCommentLines(`(function(){
  // The injected payload must never be re-evaluated while an operation is
  // in flight: a rebuild would swap window.FMV.menu to a fresh closure and
  // the running op would become unstoppable. Guard on BOTH states (auto
  // loops and one-shot ops).
  if (window.FMV && window.FMV.menu &&
      ((window.FMV.menu.running && window.FMV.menu.running()) ||
       (window.FMV.menu.busy && window.FMV.menu.busy()))) {
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
    // NOTE: pause_protect (installed first) swallows document-level
    // 'visibilitychange' registrations, so that event can never invalidate
    // the cache here — the 30s expiry + focus/blur do the job instead.
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
  const SETTLE_MIN = 150;
  const SETTLE_MAX = 1500;
  const settleSleep = async () => sleep(await adaptSettle(SETTLE_MIN, SETTLE_MAX));
  // Keep harvest work in short tasks so a large crop board cannot monopolize
  // the game's main thread while direct behaviors/clicks are being queued.
  const HARVEST_ACTION_BATCH = 8;
  const createHarvestBreather = () => {
    let actions = 0;
    return async () => {
      actions++;
      if (actions < HARVEST_ACTION_BATCH) return;
      actions = 0;
      await sleep(0);
    };
  };
  const state = { busy: false, running: false, stop: false, rounds: 0, opStart: null, mode: null };
  // flash-deal ids, shared by the one-shot buy-all and the auto toggle
  const FLASH_DEAL_IDS = ['flash_deal_ingredient', 'flash_deal_generator',
    'flash_deal_material', 'flash_deal_chest', 'flash_deal_key', 'flash_deal_greenhouse'];
  // which deal types the Auto Flash Deals toggle buys. Defaults mirror the
  // one-shot: everything except harvest products (ingredient pool = crops +
  // animal produce — the farm makes them for free). Persisted per install
  // in localStorage 'fmv-market-filter'.
  const MARKET_DEAL_DEFAULT = { flash_deal_ingredient: false, flash_deal_generator: true,
    flash_deal_material: true, flash_deal_chest: true, flash_deal_key: true,
    flash_deal_greenhouse: true };
  let marketDealFilter = null;
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
  // idle wait when the orders loop hits a wall and merging changed nothing
  // (order slots can open later — keep the loop alive, never self-stop)
  const ORDERS_IDLE_WAIT_MS = 3000;
  const ORDERS_WAIT_MS = 5000;
  const CLEAR_SOURCES = new Set(['tree', 'rock', 'toolbox']);
  // Sources also come in tiered variants in this build (tree_small/
  // tree_medium/tree_large, rock_small/_medium/_large — the shop's moveable
  // builds report object ids like 'tree_small' for blueprint
  // 'tree_small_moveable'), so match by family prefix, not exact id.
  // Non-source entities never reach the payment path anyway (no
  // MapSource/ResourceGate → no cost → skipped).
  const isClearSource = (id) => CLEAR_SOURCES.has(id) || /^(tree|rock|toolbox)/.test(id);
  // payments per clear turn. Each payment's collect is processed by the game
  // one tick at a time (loot landing + source re-arm), so throughput is
  // bounded by how many sources are in flight in parallel — a higher cap
  // means more sources draining per tick window. The cap is adapted to the
  // free cells (each payment drops ~4-6 loot items), so a big wave never
  // floods the board into a premature 'board full' stop.
  // max payments per clear cycle — 5 at a time keeps the game light
  const CLEAR_TAP_CAP = 5;
  // consecutive 'board full' cycles before Auto Clear gives up (the pre-sweep
  // frees cells when collectables are on the ground; overflow loot parks in
  // the source's tile record, so the loop can keep clearing through a full
  // board — this only stops it when nothing frees up for a while)
  const CLEAR_BOARD_FULL_RETRIES = 20;
  // A source can take a tick to become payable after release/collection. Keep
  // Auto Clear alive through that short idle window, but stop eventually when
  // the board genuinely has nothing left to clear.
  const CLEAR_IDLE_RETRIES = 60;
  // idle retry cadence: sources spend seconds re-arming (loot landing), so
  // retry fast to notice re-armed sources quickly; each retry also sweeps
  // ground loot, so stragglers get picked up without extra settles
  const CLEAR_IDLE_WAIT_MS = 500;
  // ── logging ──────────────────────────────────────────────────────────────
  function ts() {
    const d = new Date(), p = (n) => (n < 10 ? '0' : '') + n;
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  // collapsed log view is pure CSS now (#fmv-menu .log:not(.open) .l hidden,
  // :last-child shown — the browser re-evaluates on append), so a scroll is
  // all that's needed here (was O(n) per-line display writes before)
  function updateLogView() {
    if (logEl.current) logEl.current.scrollTop = logEl.current.scrollHeight;
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
      throw new Error('FMV lost — Discord activity restarted; re-run install.mjs');
    }
  }

  // ── board snapshot (read in-frame, via FMVUtil) ─────────────────────────
  // Returns { cells, empties, items } or { error } (no throw — callers check).
  const readBoard = window.FMVUtil.readBoard;

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
    for (const nat of naturals) merges.push({ key: nat.key, from: nat.cells[0], to: nat.cells[1], size: nat.cells.length });
    for (const g of groups) merges.push({ key: g.key, from: g.group[0], to: g.group[1], size: g.group.length });

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
        const r = FMV.merge(m.from.col, m.from.row, m.to.col, m.to.row, m.size);
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
        // re-check every 20 spawns: the event fires even with 0 crates left
        // (and reports ok), so stop early instead of firing into the void
        if (spawned > 0 && spawned % 20 === 0 && cratesLeft() <= 0) {
          log('no crates — fill stop', 'warn');
          break;
        }
        const r = window.FMV.spawnCrate(e.col, e.row);
        if (r && r.ok) {
          spawned++;
          if (r.cratesLeft !== undefined && Number(r.cratesLeft) <= 0) {
            log('no crates — fill stop', 'warn');
            break;
          }
        }
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
    const mergeWait = await adaptSettle(SETTLE_MIN, SETTLE_MAX);
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

    const capped = moves + swaps >= cap;
    const unplaced = plan.reduce((s, p) => s + p.cells.length, 0) - placed.size;
    log('sort: mv ' + moves + ' · sw ' + swaps + ' · fail ' + fails +
      (capped ? ' (cap — ' + Math.max(0, unplaced) + ' unplaced)' : ''));
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
  // tabs), so harvest runs in small alternating batches: queue crops, settle,
  // collect their loot/ground drops, then queue the next batch.
  // Readiness = no cooldown entry in the tile save model (the game writes it
  // on harvest) + hitpoints remaining.
  const getTapRouter = window.FMVUtil.getTapRouter;

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
    try { lootReceivedCtor = FMVUtil.findCtorByType(window.FMV.req(window.__FMV_hcId), 'lootReceived'); } catch (e) {}
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
    const tiles = FMVUtil.tileModel();
    const breathe = createHarvestBreather();
    const CYCLE_BATCH = HARVEST_ACTION_BATCH;
    const MAX_CYCLES = 128;
    const CYCLE_COLLECT_ROUNDS = 2;
    const DRAIN_COLLECT_ROUNDS = 6;

    let harvested = 0, cooling = 0, depleted = 0;
    let collected = 0, ground = 0, failed = 0;
    let settle = null;
    const getSettle = async () => {
      if (settle === null) settle = await adaptSettle(SETTLE_MIN, SETTLE_MAX);
      return settle;
    };
    const prunePending = () => {
      if (pendingHarvests.size <= 64) return;
      const now = Date.now();
      for (const [k, t] of pendingHarvests) {
        if (now - t > LAG_WINDOW) pendingHarvests.delete(k);
      }
    };

    // Find only a snapshot of ready crops. Harvesting is deliberately capped
    // per cycle so drops are collected before another crop batch is queued.
    const scanReady = () => {
      const ready = [];
      let coolingSeen = 0;
      let depletedSeen = 0;
      const now = Date.now();
      if (!hasHarvestTrigger) return { ready, cooling: 0, depleted: 0 };
      for (const cell of S.mapGrid._cells.values()) {
        if (state.stop) break;
        if (!cell || !cell.content) continue;
        const e = cell.content;
        if (!e.hasBehavior || !e.hasBehavior(I.Harvestable)) continue;
        if (e.hasBehavior(I.Lootable)) continue;
        const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
        if (hp && typeof hp.current === 'number' && hp.current <= 0) { depletedSeen++; continue; }
        const tile = FMVUtil.tileAt(tiles, cell.column, cell.row);
        if (tile && tile.cooldown) { coolingSeen++; continue; }
        const pendingAt = pendingHarvests.get(e);
        if (pendingAt && now - pendingAt < LAG_WINDOW) { coolingSeen++; continue; }
        ready.push({ cell: cell, entity: e });
      }
      return { ready, cooling: coolingSeen, depleted: depletedSeen };
    };

    const queueHarvestBatch = async (ready) => {
      let added = 0;
      for (const r of ready) {
        if (state.stop || added >= CYCLE_BATCH) break;
        const cell = r.cell;
        const e = r.entity;
        if (!cell || cell.content !== e) { await breathe(); continue; }
        if (!e.hasBehavior(I.Harvestable) || e.hasBehavior(I.Lootable)) { await breathe(); continue; }
        const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
        if (hp && typeof hp.current === 'number' && hp.current <= 0) { await breathe(); continue; }
        const tile = FMVUtil.tileAt(tiles, cell.column, cell.row);
        if (tile && tile.cooldown) { await breathe(); continue; }
        const pendingAt = pendingHarvests.get(e);
        if (pendingAt && Date.now() - pendingAt < LAG_WINDOW) { await breathe(); continue; }
        try {
          e.addBehavior(new lootReceivedCtor({}));
          pendingHarvests.set(e, Date.now());
          harvested++;
          added++;
        } catch (e2) {
          log('harvest fail ' + cell.column + ':' + cell.row, 'warn');
        }
        await breathe();
      }
      prunePending();
      return added;
    };

    const scanDrops = () => {
      const lootables = [];
      const collectables = [];
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell || !cell.content) continue;
        const e = cell.content;
        const lootable = e.hasBehavior && e.hasBehavior(I.Lootable);
        if (lootable) {
          if (e.hasBehavior(I.Collectable) || e.hasBehavior(I.Harvestable)) {
            lootables.push({ e: e, cell: cell });
          }
          continue;
        }
        if (!e.hasBehavior || !e.hasBehavior(I.Collectable)) continue;
        if (FMVUtil.isProductCollectable(e, I)) collectables.push({ e: e, cell: cell });
      }
      return { lootables, collectables };
    };

    const collectDrops = async (roundLimit) => {
      let total = 0;
      for (let round = 0; round < roundLimit && !state.stop; round++) {
        const found = scanDrops();
        if (!found.lootables.length && !found.collectables.length) break;
        let actions = 0;
        const atCell = (r) => r.cell && r.cell.content === r.e;
        const collectList = async (records, kind) => {
          let handled = 0;
          for (const r of records) {
            if (state.stop || handled >= CYCLE_BATCH) break;
            if (!atCell(r) || !r.e.hasBehavior(kind === 'loot' ? I.Lootable : I.Collectable)) {
              failed++;
              await breathe();
              continue;
            }
            try {
              tapRouter._simulateClick(r.e);
              if (kind === 'loot') collected++; else ground++;
              handled++;
              actions++;
              total++;
            } catch (e2) {
              failed++;
            }
            await breathe();
          }
        };
        // Pick up already-landed product bubbles first, then pop new harvest
        // lootables. The next round catches items spawned by those pops.
        await collectList(found.collectables, 'ground');
        await collectList(found.lootables, 'loot');
        if (!actions) break;
        await sleep(await getSettle());
      }
      return total;
    };

    let cycles = 0;
    while (!state.stop && cycles < MAX_CYCLES) {
      cycles++;
      const scan = scanReady();
      if (!scan.ready.length) {
        cooling += scan.cooling;
        depleted += scan.depleted;
        break;
      }
      const added = await queueHarvestBatch(scan.ready);
      if (!added) break;
      await sleep(await getSettle());
      await collectDrops(CYCLE_COLLECT_ROUNDS);
    }
    if (!state.stop && cycles >= MAX_CYCLES) log('harvest cycle cap', 'warn');
    if (!state.stop && harvested > 0) await sleep(await getSettle());
    if (!state.stop) await collectDrops(DRAIN_COLLECT_ROUNDS);

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
      // count only claims that observably landed: the order flipped state or
      // was replaced/removed (fire-and-forget counting drifted after claims)
      const after = (O.getCurrentOrders() || []).find((o) => o && o.buildingID === order.buildingID);
      if (!after || after.state !== COMPLETE) claimed++;
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
    return { claimed, started, skipped };
  }

  // ── Stop request: set the flag (the menu dot stops ops) ──────────────────
  function requestStop() {
    state.stop = true;
    log('stop — halting', 'warn');
    setUI(); // immediate 'STOPPING…' feedback — loops wind down on their own schedule
  }

  // ── Auto-merge pass: run the merge planner (5/10/15 chains) to free cells
  //    and build higher tiers. Used by the Auto Orders / Auto Clear loops
  //    when they hit the board-full / nothing-to-do wall — the clear's loot
  //    (wood/tools) and order products are mergeable, so merging both frees
  //    space and makes orders affordable. Returns merges done (0 = no-op).
  async function autoMergePass() {
    assertFMV();
    const board = readBoard();
    if (board.error) return 0;
    const { naturals, groups } = window.FMVPlan.planAll(board);
    if (!naturals.length && !groups.length) return 0;
    const result = await executeBatch(naturals, groups);
    const movesOk = (result.moves || []).filter((m) => m && m.ok).length;
    const swapsOk = (result.swaps || []).filter((m) => m && m.ok).length;
    const mergesOk = (result.merges || []).filter((m) => m && m.ok).length;
    stats.merged += mergesOk;
    stats.moved += movesOk;
    stats.swapped += swapsOk;
    return mergesOk;
  }

  // ── Auto Orders loop (toggle): claim + start orders, then INSTANTLY
  //    finish their production timers (the game's own completion path) so
  //    orders complete immediately — no waiting. NEVER stops on its own:
  //    on a wall (nothing claimed/started or board full) it merges chains
  //    to free space / build tiers, and if merging changes nothing it
  //    idle-waits and retries — order slots can open later (building
  //    refresh, harvests, merges). The ■ STOP dot / button ends it anytime.
  async function runOrdersLoop() {
    let cycle = 0;
    while (state.running && !state.stop) {
      cycle++;
      // exception safety: a single transient throw must never end this loop
      // (its contract is never-self-stop) — log and retry with the idle wait
      try {
        let r = await orders();
        // skip the production wait: complete every Order_* timer right away
        let ff = null;
        try { ff = window.FMV.finishTimers('Order_'); } catch (e) {}
        if (ff && ff.ok && ff.finished > 0) {
          await settleSleep(); // let the game process the completions
          const r2 = await orders(); // claim the freshly completed orders
          r = { claimed: r.claimed + r2.claimed, started: r.started + r2.started };
        }
        const board = readBoard();
        if (board.error) throw new Error(board.error);
        // wall: nothing claimed/started (orders unaffordable or no free slots)
        // or the board is full — merge chains to free cells and build the
        // higher tiers orders may need, then retry.
        if (!r.claimed && !r.started || board.empties.length === 0) {
          let merged = 0;
          try { merged = await autoMergePass(); } catch (e) { log('merge fail: ' + (e && e.message), 'warn'); }
          if (merged > 0) {
            log('orders wall — merged ' + merged + ' chains to free space / build tiers', 'warn');
            await settleSleep();
            continue;
          }
          // nothing mergeable and nothing claimed/started right now — keep the
          // loop alive (orders can open later) instead of stopping; log an
          // idle heartbeat instead of a line per cycle (long hidden-tab
          // sessions used to flood the log buffer with no-op cycles)
          if (cycle % 5 === 0) log('orders cyc ' + cycle + ' — idle, retrying', 'ok');
          await sleep(ORDERS_IDLE_WAIT_MS);
          continue;
        }
        log('orders cyc ' + cycle, 'ok');
        const wait = await adaptSettle(SETTLE_MIN, SETTLE_MAX) * 2;
        const deadline = Date.now() + Math.min(2000, Math.max(800, wait));
        while (!state.stop && Date.now() < deadline) await sleep(250);
      } catch (e) {
        log('orders cyc ' + cycle + ' fail: ' + (e && e.message ? e.message : e), 'warn');
        await sleep(ORDERS_IDLE_WAIT_MS);
      }
    }
  }

  // ── Auto Clear (toggle): clear sources as FAST as possible until a
  //    terminal reason — energy out or board full. Cooldowns are skipped
  //    inside clearOnce; transient 'nothing ready'/'collected only' gaps
  //    retry automatically because source release/collection can take a game tick.
  async function autoClearFast() {
    let cycle = 0;
    let idleRetries = 0;
    let boardFullRetries = 0;
    let notFocusedRetries = 0;
    while (state.running && !state.stop) {
      cycle++;
      let reason;
      try {
        reason = await clearOnce();
        // every cycle merges chains — like Auto Orders does at its wall. The
        // clear's own loot (wood/tools/stone) is mergeable, so consolidating
        // it each cycle frees cells, keeps the board from flooding and the
        // adaptive payment cap high. No-op fast when nothing is chainable.
        let merged = 0;
        try { merged = await autoMergePass(); } catch (e) { log('merge fail: ' + (e && e.message), 'warn'); }
        if (merged > 0) {
          log('merged ' + merged + ' chains', 'ok');
          await settleSleep(); // let the merges settle before the next cycle
        }
      } catch (e) {
        // transient throws must not kill the loop — retry like an idle gap
        log('clear cyc ' + cycle + ' fail: ' + (e && e.message ? e.message : e), 'warn');
        idleRetries++;
        await sleep(CLEAR_IDLE_WAIT_MS);
        continue;
      }
      if (reason === 'nothing ready' || reason === 'collected only') {
        idleRetries++;
        if (idleRetries >= CLEAR_IDLE_RETRIES) {
          log('auto clear stop: nothing ready after ' + idleRetries + ' retries', 'warn');
          break;
        }
        if (cycle % 10 === 0) log('clear cyc ' + cycle + ' — waiting (' + reason + ')', 'ok');
        await sleep(CLEAR_IDLE_WAIT_MS);
        continue;
      }
      if (reason === 'board full') {
        // transient — the per-cycle merge above frees cells as loot lands;
        // keep retrying until the board genuinely can't hold more
        boardFullRetries++;
        if (boardFullRetries >= CLEAR_BOARD_FULL_RETRIES) {
          log('auto clear stop: board full', 'warn');
          break;
        }
        await sleep(1000);
        continue;
      }
      if (reason === 'not focused') {
        // only reachable when the pause protection is missing/stale — retry
        // a while (Chrome may just be slow to unfreeze), then stop with a hint
        notFocusedRetries++;
        if (notFocusedRetries >= CLEAR_IDLE_RETRIES) {
          log('auto clear stop: not focused (pause protection missing?) — re-run install.mjs', 'warn');
          break;
        }
        await sleep(1000);
        continue;
      }
      if (reason === 'no tap services' || reason === 'no router' ||
          reason === 'no free workers') {
        await sleep(1000); // transient — retry shortly
        continue;
      }
      if (reason) { log('auto clear stop: ' + reason, 'warn'); break; }
      idleRetries = 0;
      boardFullRetries = 0;
      notFocusedRetries = 0;
      await sleep(250); // as fast as the game's tick allows
    }
  }

  // ── BUY ALL FLASH DEALS: refresh the deals (re-roll picks + refill stock,
  //    the game's own refresh path), then purchase every unit of stock of
  //    each deal. SKIPPED: harvest-product deals (crops + animal produce the
  //    farm already makes — no need to buy). reward_crate_* rewards (keys +
  //    chests/crates) are bought and placed DIRECTLY into empty cells — they
  //    must never ride the storage-bubble path (moveContentToCell never
  //    completes for them; 40+ crates froze the game loop once). Everything
  //    else lands in storage bubbles via the game's own delivery; use the
  //    separate Tap Bubbles action to collect them safely. If gems/coins run
  //    short, the deficit is granted. filterIds (Set of
  //    flash-deal ids) restricts the purchase to selected deal types — the
  //    auto toggle passes the menu's checkbox selection; omitted = buy all.
  function marketplaceServicesReady() {
    try {
      const S = window.FMV.services();
      const R = window.FMV.rootServices();
      const m = S && S.marketplaceService;
      const fds = m && m.flashDealsService;
      return !!(S && S.mapGrid && S.storageBubble && R && R.inventory &&
        R.blueprintCollection && m && typeof m._resetFlashDealStock === 'function' &&
        typeof m.getStockItem === 'function' && fds &&
        typeof fds.getFlashDealItem === 'function' &&
        typeof fds._onFlashDealTimerFinished === 'function' && fds._model &&
        typeof fds._model.getStock === 'function' &&
        typeof fds._model.setStock === 'function');
    } catch (e) { return false; }
  }
  async function refundMarketPayment(key, amount) {
    if (!key || !amount) return false;
    try {
      const r = await window.FMV.grant([{ key: key, amount: amount }]);
      if (r && r.ok) return true;
      log('market refund failed for ' + amount + ' ' + key, 'err');
    } catch (e) { log('market refund failed: ' + (e && e.message), 'err'); }
    return false;
  }
  async function buyAllMarketplace(filterIds) {
    assertFMV();
    const S = window.FMV.services();
    const R = window.FMV.rootServices();
    const m = S.marketplaceService;
    const fds = m.flashDealsService;
    // reward_crate_* family = keys (reward_crate_key_*) and crates/chests
    // (reward_crate_bronze_gazebo, reward_crate_gold_gazebo ...) — Mergeable
    // board items that must be placed DIRECTLY (never the bubble tap path)
    const CRATE_FAMILY_RE = /^reward_crate_/;
    // harvest/farm products (the 'ingredient' deal pool — crops + animal
    // produce): the one-shot skips these, while the auto toggle buys them
    // only when the ingredient checkbox is explicitly selected
    const HARVEST_PRODUCTS = new Set(['sugarcane', 'tomato', 'sunflower', 'corn', 'soybeans',
      'carrot', 'wheat', 'coffeebeans', 'goatmilk', 'milk', 'egg', 'fur', 'wool', 'bacon',
      'pumpkin', 'potato', 'strawberry', 'blueberry', 'grape', 'melon', 'rice']);
    const buyHarvestProducts = !!(filterIds && filterIds.has('flash_deal_ingredient'));
    const refillModelStock = (id) => {
      try {
        const e = fds.getFlashDealItem(id);
        if (e && e.renewableStock && Number.isFinite(Number(e.renewableStock.amount))) {
          fds._model.setStock(id, Number(e.renewableStock.amount));
        }
      } catch (e2) {}
    };

    // 1) refresh the deals BEFORE buying — same path the game runs when the
    //    flash-deal timer expires (re-roll picks, reorder, re-arm the 4h
    //    timer) plus the stock refill (both the marketplace stock items and
    //    the flash-deals model stock)
    log('market: refreshing flash deals', 'ok');
    try {
      fds._onFlashDealTimerFinished();
      m._resetFlashDealStock();
      for (const id of FLASH_DEAL_IDS) refillModelStock(id);
      await settleSleep(); // let the game process the re-roll before buying
    } catch (e) {
      const msg = e && e.message ? e.message : e;
      log('market refresh fail: ' + msg, 'warn');
      const refreshError = new Error('market refresh failed: ' + msg);
      refreshError.code = 'FMV_MARKET_REFRESH';
      throw refreshError;
    }

    let bought = 0, failed = 0, granted = 0, skipped = 0, placed = 0;
    for (const id of FLASH_DEAL_IDS) {
      if (state.stop) break;
      if (filterIds && !filterIds.has(id)) continue; // deselected deal type
      let paymentDeducted = false;
      let delivered = 0;
      let payKey = null;
      let need = 0;
      try {
        const entry = fds.getFlashDealItem(id); // real payment/reward for the fresh pick
        if (!entry) { skipped++; continue; }
        const reward = entry.reward || {};
        if (reward.type !== 'object' && reward.type !== 'inventory') { skipped++; continue; }
        const rewardBps = Array.isArray(reward.data) ? reward.data : [reward.data];
        if (!buyHarvestProducts && rewardBps.some((bp) => HARVEST_PRODUCTS.has(String(bp)))) {
          log('market skip ' + id + ': harvest product (' + rewardBps.join(',') + ') — farm makes it for free', 'warn');
          skipped++;
          continue;
        }
        // split the reward: reward_crate_* goes to the board via direct
        // placement, everything else rides the storage-bubble path
        const crateBps = rewardBps.filter((bp) => CRATE_FAMILY_RE.test(String(bp)));
        const normalBps = rewardBps.filter((bp) => !CRATE_FAMILY_RE.test(String(bp)));
        if (reward.type === 'object' && !crateBps.length && !normalBps.length) {
          skipped++;
          continue;
        }
        // Resolve every ordinary blueprint before payment so malformed deal
        // data cannot consume currency before delivery is attempted.
        let normalBubbleDefs = [];
        if (reward.type === 'object') {
          try {
            normalBubbleDefs = normalBps.map((bp) => ({
              blueprint: bp,
              data: R.blueprintCollection.getBlueprint(bp).components
            }));
          } catch (e3) {
            skipped++;
            log('market skip ' + id + ': invalid reward blueprint', 'warn');
            continue;
          }
        }
        let stock = fds._model.getStock(id);
        if (!Number.isFinite(stock) || stock < 0) stock = 1; // unset = single purchase
        if (stock === 0) { skipped++; continue; }
        if (crateBps.length) {
          // direct placement needs free cells — check BEFORE paying so a
          // full board never burns gems on unplaceable rewards
          let empties = 0;
          try {
            for (const cell of S.mapGrid._cells.values()) if (cell && !cell.content) empties++;
          } catch (e3) {}
          const needCells = crateBps.length * stock;
          if (empties < needCells) {
            log('market skip ' + id + ': need ' + needCells + ' free cells for ' + crateBps.join(',') + ' (have ' + empties + ')', 'warn');
            skipped++;
            continue;
          }
        }
        need = Number(entry.payment.amount) || 0;
        payKey = entry.payment.key || 'gems';
        for (let n = 0; n < stock && !state.stop; n++) {
          paymentDeducted = false;
          delivered = 0;
          const have = R.inventory.getAmount(payKey);
          if (have < need) {
            const deficit = need - have;
            const g = await window.FMV.grant([{ key: payKey, amount: deficit }]);
            if (!g || !g.ok) { failed++; break; }
            granted += deficit;
            if (state.stop) break;
          }
          if (state.stop) break;
          R.inventory.deductItems([{ key: payKey, amount: need }]);
          paymentDeducted = true;
          if (reward.type === 'inventory') {
            const rewardResult = await window.FMV.grant(rewardBps.map((r) => (typeof r === 'object' && r.key ? r : { key: String(r), amount: 1 })));
            if (!rewardResult || !rewardResult.ok) throw new Error('inventory reward delivery failed');
            delivered = rewardBps.length;
          } else {
            if (normalBubbleDefs.length) {
              S.storageBubble.createBubbleAndShowContent(normalBubbleDefs);
              delivered += normalBubbleDefs.length;
            }
            // keys/chests: direct placement — the same machinery FMV.spawn
            // uses for crates (factory + GridPosition + setContent); never
            // the bubble tap path (verified: moveContentToCell hangs for
            // this family and 40+ unplaced crates froze the game loop)
            for (const bp of crateBps) {
              const sp = window.FMV.spawn([{ key: String(bp), amount: 1 }]);
              if (!(sp && sp.ok && sp.placed && sp.placed[0] && sp.placed[0].amount > 0))
                throw new Error('no free cell for ' + bp);
              placed++;
              delivered++;
            }
          }
          // keep BOTH stock stores consistent (the popup reads the stock
          // items, the flash-deals model is the other half of the book)
          try { const si = m.getStockItem(id); if (si && Number.isFinite(si.amount) && si.amount > 0) si.amount--; } catch (e2) {}
          try { const s2 = fds._model.getStock(id); if (Number.isFinite(s2) && s2 > 0) fds._model.setStock(id, s2 - 1); } catch (e2) {}
          bought++;
          // purchases are safe-fast (the freeze was the bubble-TAP step,
          // not bubble creation): breathe between buys, settle every 5
          await sleep(120);
          if (bought % 5 === 0) await settleSleep();
        }
        log('market: ' + id + ' x' + (stock - Math.max(0, fds._model.getStock(id))) + ' (' + need + ' ' + payKey + ' each)', 'ok');
      } catch (e2) {
        failed++;
        if (paymentDeducted && delivered === 0 && await refundMarketPayment(payKey, need))
          log('market: refunded failed ' + need + ' ' + payKey, 'warn');
        else if (paymentDeducted && delivered > 0)
          log('market: partial reward delivery; payment kept', 'warn');
        log('market fail ' + id + ': ' + (e2 && e2.message), 'warn');
      }
    }

    // 2) the delivered goods sit in storage bubbles — NOT auto-collected
    //    here: tapping many bubbles in a row is what froze the game (each
    //    tap runs multiple full-grid scans + spawns every item with a move
    //    animation; bursts stall the main thread into a watchdog restart).
    //    Use the separate 📦 Tap Bubbles button (slow by design) or tap them
    //    in-game at your own pace.
    const bub = (() => {
      try {
        const S2 = window.FMV.services();
        const I2 = window.FMV.I();
        let n = 0;
        for (const e of (S2.world._gameObjects || [])) {
          try {
            if (!e.hasBehavior || !e.hasBehavior(I2.StorageBubble)) continue;
            const content = e.getBehavior(I2.StorageBubble).content;
            if (content && content.length) n++;
          } catch (e2) {}
        }
        return n;
      } catch (e) { return 0; }
    })();
    if (bub) log('market: ' + bub + ' storage bubbles left — tap them with 📦 Tap Bubbles or in-game', 'warn');

    log('market done: ' + bought + ' bought' + (placed ? ' · ' + placed + ' placed' : '') +
      (granted ? ' · +' + granted + ' granted' : '') +
      (skipped ? ' · ' + skipped + ' skipped' : '') + (failed ? ' · ' + failed + ' failed' : ''), bought ? 'ok' : 'warn');
    return { bought: bought, failed: failed, skipped: skipped, granted: granted, placed: placed, bubbles: bub };
  }

  // ── AUTO FLASH DEALS (toggle): every cycle refreshes the deals (re-roll
  //    picks + refill stock — the game's own refresh path) and buys the
  //    stock of the deal types selected in the menu checkboxes. Because the
  //    refresh re-rolls picks and refills renewable stock each cycle, the
  //    loop keeps buying effectively forever — the ■ STOP dot / button ends
  //    it anytime. Mid-run checkbox changes apply on the next cycle.
  function selectedMarketDealIds() {
    const f = marketDealFilter || MARKET_DEAL_DEFAULT;
    const ids = new Set();
    for (const id of FLASH_DEAL_IDS) if (f[id]) ids.add(id);
    return ids;
  }
  async function runAutoMarketplaceLoop() {
    let cycle = 0;
    while (state.running && !state.stop) {
      const selected = selectedMarketDealIds();
      if (!selected.size) {
        log('auto market stop: no deal types selected — tick a checkbox first', 'warn');
        break;
      }
      if (!marketplaceServicesReady()) {
        log('auto market stop: marketplace services unavailable', 'warn');
        break;
      }
      cycle++;
      state.rounds = cycle;
      try {
        await buyAllMarketplace(selected);
      } catch (e) {
        log('auto market fail: ' + (e && e.message), 'warn');
        if (e && (e.code === 'FMV_MARKET_REFRESH' || !marketplaceServicesReady())) break;
      }
      // breathe between cycles: let the game settle so the delivered goods
      // (bubbles + board placements) land before the next refresh
      for (let i = 0; i < 10 && state.running && !state.stop; i++) await sleep(200);
    }
  }

  // ── toggle-loop runner: click starts the loop, click again stops it ─────
  async function runToggleLoop(fn, mode) {
    if (state.running) { requestStop(); return; } // toggle OFF
    if (state.busy) return; // a one-shot op is running
    state.running = true;
    state.mode = mode;
    state.stop = false;
    state.rounds = 0;
    state.opStart = Date.now();
    setUI();
    try {
      await fn();
    } catch (e) {
      log('ERR: ' + (e && e.message ? e.message : e), 'err');
    }
    state.running = false;
    state.mode = null;
    state.stop = false;
    state.opStart = null;
    setUI();
    refreshStatus(true);
    log(mode + ' loop off');
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
  let paySvc = null, lootSvc = null, paySvcFor = null, tapSvcValidatedAt = 0;
  const TAP_SVC_VALID_MS = 10000;
  function cachedTapServicesValid(S) {
    if (!paySvc || !lootSvc || paySvcFor !== S) return false;
    // the registries only rebuild when subsystems spawn/die or the farm
    // changes — re-validating with a full grid walk on EVERY call (once per
    // clear turn) doubled the per-turn scan cost; check at most every 10s
    if (Date.now() - tapSvcValidatedAt < TAP_SVC_VALID_MS) return true;
    let payFound = false;
    let lootFound = false;
    FMVUtil.forEachCell(S, (cell) => {
      if (!cell.content) return;
      let ev = null;
      try { ev = cell.content.onBehaviorAdded; } catch (e) { return; }
      FMVUtil.walkBehaviorRegistries(ev, (reg, types, sub) => {
        if (sub === paySvc) payFound = true;
        if (sub === lootSvc) lootFound = true;
        if (payFound && lootFound) return false;
      });
      if (payFound && lootFound) return false;
    });
    tapSvcValidatedAt = Date.now();
    return payFound && lootFound;
  }
  // ResourceGate ctor (module 10295 'sh' in this build, discovered
  // structurally): the payment consumes the gate (removeBehavior) and the
  // game's re-arm path never fires in this build, so we re-add it ourselves
  let rgCtorCache = null;
  function resourceGateCtor() {
    if (rgCtorCache) return rgCtorCache;
    try { rgCtorCache = FMVUtil.findCtorByType(window.FMV.req(window.__FMV_hcId), 'resourceGate'); } catch (e) {}
    return rgCtorCache;
  }
  function reAddResourceGate(e, cost, workers) {
    try {
      const Ctor = resourceGateCtor();
      const I2 = window.FMV.I();
      if (!Ctor || !e || e.hasBehavior(I2.ResourceGate)) return;
      e.addBehavior(new Ctor({ cost: [{ key: 'energy', amount: Math.max(1, Number(cost) || 5) }], workers: Number(workers) || 1 }));
    } catch (e2) {}
  }
  // Delete every cooldown timer for one source cell BY LABEL. The game
  // registers the MapSourceCooldown timer on the tick AFTER the payment's
  // synchronous effects, so a timerId-based delete (which reads the tile
  // record too early) leaves orphans behind; label matching catches the
  // timer whenever it appears.
  function dropSourceCooldownTimers(col, row) {
    try {
      const prefix = 'MapSourceCooldown:' + col + ':' + row;
      const timers = window.FMV.rootServices().timer._timerModel._timers;
      for (const [k, v] of timers.entries()) {
        try {
          if (String(v._label || v._type || '').indexOf(prefix) !== -1) timers.delete(k);
        } catch (e) {}
      }
    } catch (e) {}
  }
  // sweep EVERY MapSourceCooldown timer (hygiene: payments register their
  // timer a tick AFTER the cleanup, so stale ones pile up across runs; they
  // fire into the void — the cooldown processor finds no entity hook)
  function dropAllSourceCooldownTimers() {
    try {
      const timers = window.FMV.rootServices().timer._timerModel._timers;
      for (const [k, v] of timers.entries()) {
        try {
          if (String(v._label || v._type || '').indexOf('MapSourceCooldown:') === 0) timers.delete(k);
        } catch (e) {}
      }
    } catch (e) {}
  }
  function findTapServices() {
    const S = window.FMV.services();
    // registries are rebuilt as subsystems spawn/die / the farm changes —
    // the cached contexts are only valid while the services object is the
    // same one they were discovered on (never trust a cross-farm cache)
    if (cachedTapServicesValid(S)) return true;
    let pay = null, loot = null;
    FMVUtil.forEachCell(S, (cell) => {
      if (!cell.content) return;
      let ev = null;
      try { ev = cell.content.onBehaviorAdded; } catch (e) { return; }
      FMVUtil.walkBehaviorRegistries(ev, (reg, types, sub) => {
        if (!pay && typeof sub._attemptPayment === 'function') pay = sub;
        if (!loot && types.indexOf('interactionTap') !== -1 && types.indexOf('lootable') !== -1 &&
            typeof sub._onInteractionAdded === 'function') loot = sub;
        if (pay && loot) return false;
      });
      if (pay && loot) return false;
    });
    paySvc = pay;
    lootSvc = loot;
    paySvcFor = S;
    tapSvcValidatedAt = Date.now();
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
    const tiles = FMVUtil.tileModel();
    // hygiene: payments' cooldown timers register a tick AFTER our cleanup,
    // so stale MapSourceCooldown timers can pile up across runs; they fire
    // into the void (the cooldown processor finds no entity hook), but a
    // sweep per turn keeps the timer table lean
    dropAllSourceCooldownTimers();
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
        if (!id || !isClearSource(id)) continue;
        const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
        if (!hp || typeof hp.current !== 'number' || hp.current <= 0) continue;
        let tile = null;
        if (tiles) tile = FMVUtil.tileAt(tiles, cell.column, cell.row);
        if (tile && (tile.cooldown || tile.workerData)) {
          // Stuck or mid-chop: force-release the worker via the game's own
          // path (releaseForObject removes the entity's WorkerData behavior;
          // the game then clears the tile and marks the source lootable).
          // Finishing the cooldown timer alone does NOT release the worker in
          // this build — the cooldown processor finds no entity hook, so the
          // tile stays stuck forever (the 20s 'no free workers' stalls).
          try { S.gameWorkers.releaseForObject(e); } catch (e2) {}
          // The release path does NOT clean the tile model or cancel the
          // cooldown timer — delete both directly (client-authoritative save;
          // the orphan timer would otherwise fire later and pile up).
          try {
            dropSourceCooldownTimers(cell.column, cell.row);
            delete tile.cooldown;
            delete tile.workerData;
          } catch (e2) {}
          if (tiles) tile = FMVUtil.tileAt(tiles, cell.column, cell.row);
          if (tile && (tile.cooldown || tile.workerData)) continue;
        }
        if (tile && tile.lootable) {
          lootables.push({ entity: e, col: cell.column, row: cell.row });
          continue;
        }
        let rg = e.hasBehavior(I.ResourceGate) ? e.getBehavior(I.ResourceGate) : null;
        // previously-paid sources lost their ResourceGate (the payment consumes
        // it and the game's re-arm never fires in this build) — re-add it from
        // the mapSource steps config so the source is payable again
        if (!rg) {
          try {
            const ms = e.getBehavior(I.MapSource);
            const st = ms && ms._data && ms._data.steps ? ms._data.steps['1'] : null;
            const cost = st && Number.isFinite(Number(st.cost)) ? Number(st.cost) : 5;
            reAddResourceGate(e, cost, 1);
            rg = e.hasBehavior(I.ResourceGate) ? e.getBehavior(I.ResourceGate) : null;
          } catch (e2) {}
        }
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
    if (empties === 0) {
      // board looks full — unpicked ground loot (Collectable bubbles) is
      // usually the filler; sweep it first, then recheck before bailing
      const f0 = FMVUtil.collectablesOnBoard(S, I);
      for (const e of f0) {
        try { tapRouter._simulateClick(e); } catch (e2) {}
      }
      if (f0.length) await settleSleep();
      ({ cands, lootables, empties } = scan());
      if (empties === 0) return 'board full';
    }

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
      return 'energy out';
    }
    let tapped = 0;
    let noWorkers = 0;
    const paidCells = []; // deferred cooldown-timer re-drop for this turn
    // wave size follows the free cells (~4-6 loot items per payment), so the
    // board never floods into a premature stop; min 4 keeps progress on a
    // nearly-full board (overflow parks safely in the source's tile record)
    const tapCap = Math.max(4, Math.min(CLEAR_TAP_CAP, Math.floor(empties / 4)));
    for (const c of cands) {
      if (state.stop) break;
      if (tapped >= tapCap) break;
      energy = readEnergy();
      if (energy < c.cost) break;
      let free = true;
      try { free = !!S.gameWorkers.hasEnoughWorkers(c.workers); } catch (e2) {}
      if (!free) { noWorkers++; continue; }
      try {
        await paySvc._attemptPayment(c.entity, 'fmv-' + c.col + ':' + c.row, c.entity.getBehavior(I.ResourceGate));
        // Payment marks the source lootable synchronously; the collector IS the
        // game's tap on a lootable (spawns loot, hp -1). The game never
        // auto-fires on the lootable flag, so there is no double-process risk;
        // if the source is not yet lootable (async window), the collect no-ops
        // and the next cycle's step-1 scan picks it up.
        try { lootSvc._onInteractionAdded(c.entity); } catch (e3) {}
        // Release the worker immediately — the game's own release path clears
        // the WorkerData behavior and the tile (source becomes lootable with
        // its loot). Without this, the worker stays held until the source is
        // destroyed and the loop stalls in 'no free workers' retries. The
        // release does NOT clean the tile model / cooldown timer, so delete
        // those directly too — otherwise the next scan sees a stale cooldown
        // and skips the source (and timers pile up).
        try { S.gameWorkers.releaseForObject(c.entity); } catch (e4) {}
        try {
          const t2 = FMVUtil.tileAt(tiles, c.col, c.row);
          if (t2) {
            dropSourceCooldownTimers(c.col, c.row);
            delete t2.cooldown;
            delete t2.workerData;
          }
        } catch (e4) {}
        // the game registers the cooldown timer a tick AFTER the payment's
        // synchronous effects — the immediate drop above can run too early,
        // so queue this cell for the turn's single deferred re-drop
        paidCells.push(c.col + ':' + c.row);
        // re-add the ResourceGate the payment consumed, so the source is
        // payable again on the next cycle (the game's re-arm never fires here)
        reAddResourceGate(c.entity, c.cost, c.workers);
        tapped++;
      } catch (e2) {
        log('pay fail ' + c.col + ':' + c.row, 'warn');
        stats.failed++;
      }
      if (tapped % 10 === 0) await sleep(0);
    }
    if (tapped === 0 && noWorkers > 0) return 'no free workers';

    // one deferred sweep for every cell paid this turn (a setTimeout per
    // payment used to pile up unbounded timers in long clear sessions)
    if (paidCells.length) {
      setTimeout(() => {
        for (const ck of paidCells) {
          const sep = ck.indexOf(':');
          dropSourceCooldownTimers(+ck.slice(0, sep), +ck.slice(sep + 1));
        }
      }, 1500);
    }

    // 3) collect ground collectables — produced items land on empty cells as
    //    bubbles (Collectable behavior) and need a tap to be picked up; only
    //    PRODUCT bubbles (reward key is a real blueprint) — coin/gem/energy
    //    reward bubbles are not ours to click. ONE sweep after the settle:
    //    loot that lands after the sweep is caught by the next turn's sweep
    //    (the loop rescans every turn, so stragglers never accumulate — no
    //    multi-round settles stalling the turn on hidden tabs).
    let ground = 0;
    if (tapped) await settleSleep(); // let this turn's fresh loot land
    const found = FMVUtil.collectablesOnBoard(S, I);
    for (const e of found) {
      if (state.stop) break;
      try { tapRouter._simulateClick(e); ground++; } catch (e2) {}
      if (ground % 20 === 0) await sleep(0);
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

  // ── HALF CRATES: shovel-remove half of the gold reward crates ───────────
  // Uses FMV.remove() (the game's own objectRemoval chain — takes a few
  // seconds per crate), so ops run in settle rounds and rescan between
  // rounds to catch stragglers.
  async function removeHalfCrates() {
    assertFMV();
    const S = window.FMV.services();
    const target = 'reward_crate_gold_gazebo';
    const find = () => {
      const out = [];
      for (const cell of S.mapGrid._cells.values()) {
        if (!cell || !cell.content) continue;
        try {
          if (cell.content.getBlueprintID() === target) {
            out.push({ col: cell.column, row: cell.row });
          }
        } catch (e) {}
      }
      return out;
    };
    const total = find().length;
    const want = Math.floor(total / 2);
    if (!want) { log('half-crates: no gold crates'); return null; }
    log('half-crates: ' + total + ' gold crates — removing ' + want);
    let removed = 0, failed = 0;
    for (let round = 0; round < 8 && removed < want && !state.stop; round++) {
      const cur = find();
      for (const c of cur) {
        if (state.stop || removed >= want) break;
        try {
          const res = window.FMV.remove(c.col, c.row);
          if (res && res.ok) removed++;
          else if (res && res.reason !== 'empty cell') failed++;
        } catch (e) { failed++; }
        if (removed % 5 === 0) await sleep(0);
      }
      await settleSleep();
      log('half-crates r' + round + ': ' + removed + '/' + want + ' removed · ' + find().length + ' left');
    }
    log('half-crates done: ' + removed + '/' + want + ' removed' +
      (failed ? ' · ' + failed + ' failed' : '') + (state.stop ? ' · stop' : ''));
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
    const ctx = { visitorProc: null, visitorSim: null, ownerProc: null, ownerSim: null, visitorFarm: false };
    // Farm-level classification: every entity's onBehaviorAdded is the SAME
    // shared event, so whether this farm routes taps through the visitorAction
    // family (a friend's farm) or the friendReward family (your farm) can be
    // decided once per call from a single entity's registry list.
    FMVUtil.forEachCell(S, (cell) => {
      if (!cell.content) return;
      try {
        FMVUtil.walkBehaviorRegistries(cell.content.onBehaviorAdded, (reg, types) => {
          if (types.indexOf('visitorAction') !== -1) { ctx.visitorFarm = true; return false; }
        });
      } catch (e) {}
      return false; // shared event — one entity is enough
    });
    FMVUtil.forEachCell(S, (cell) => {
      if (!cell.content) return;
      let ev = null;
      try { ev = cell.content.onBehaviorAdded; } catch (e) { return; }
      FMVUtil.walkBehaviorRegistries(ev, (reg, types, sub) => {
        const isVisitor = types.indexOf('visitorAction') !== -1;
        const isOwner = types.indexOf('friendReward') !== -1;
        if (!isVisitor && !isOwner) return;
        if (isVisitor) {
          if (!ctx.visitorProc && typeof sub._onActivityTapped === 'function' && typeof sub._createVisitorReward === 'function') ctx.visitorProc = sub;
          if (!ctx.visitorSim && typeof sub._simulateClick === 'function') ctx.visitorSim = sub;
        }
        if (isOwner) {
          if (!ctx.ownerProc && typeof sub._onInteractionTap === 'function' && typeof sub._processReward === 'function') ctx.ownerProc = sub;
          if (!ctx.ownerSim && typeof sub._simulateClick === 'function') ctx.ownerSim = sub;
        }
        if (ctx.visitorProc && ctx.visitorSim && ctx.ownerProc && ctx.ownerSim) return false;
      });
      if (ctx.visitorProc && ctx.visitorSim && ctx.ownerProc && ctx.ownerSim) return false;
    });
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

  // which tap path applies to an entity = which behavior family is attached
  // to the (shared) onBehaviorAdded event — decided once per call in
  // findVisitServices (ctx.visitorFarm), then per entity by its live behavior.
  async function collectVisits() {
    assertFMV();
    if (!findVisitServices()) throw new Error('friend reward services not found — game version changed?');
    const S = window.FMV.services();
    const I = window.FMV.I();
    const C = window.__FMV_visitCtx;
    const isVisitFarm = !!C.visitorFarm;
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
        if (isVisitFarm && !va) continue;
        cands.push({ e: e, col: cell.column, row: cell.row, visitor: va || isVisitFarm });
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
      await settleSleep();
    }
    let claimed = 0;
    if (visitRewardSvc) {
      // the reward pipeline lands asynchronously (a few ticks after the tap) —
      // settle before reading the pending list so nothing is missed
      await settleSleep();
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
    refreshStatus(true);
  }

  // ── UI ───────────────────────────────────────────────────────────────────
  let dot, sortBtn, fillBtn, harvestBtn, planBtn, visitBtn;
  let autoOrdersBtn, autoClearBtn, autoMarketBtn;
  let marketChecks = [];
  let cheatBtns = [];
  // idle status is served from a short-lived cache: the full board read
  // (~1000 cells × neighbor scans) used to run on every 2.5s interval tick
  // even when nothing was happening, competing with the farm's own loop in
  // throttled tabs. Ops still read live, and force a fresh read on exit.
  let statusCache = null, statusCacheAt = 0;
  function refreshStatus(force) {
    const el = document.getElementById('fmv-status');
    if (!el) return;
    let text;
    let cls = 'status';
    try {
      assertFMV();
      let items, empty, crates, err = false;
      const active = state.running || state.busy;
      if (!active && !force && statusCache && Date.now() - statusCacheAt < 10000) {
        items = statusCache.items; empty = statusCache.empty;
        crates = statusCache.crates; err = statusCache.err;
      } else {
        const b = readBoard();
        crates = cratesLeft();
        items = b.error ? '-' : b.items.length;
        empty = b.error ? '-' : b.empties.length;
        err = !!b.error;
        statusCache = { items: items, empty: empty, crates: crates, err: err };
        statusCacheAt = Date.now();
      }
      let extra = '';
      if (active) {
        if (state.rounds) extra += ' · r' + state.rounds;
        if (state.opStart) extra += ' · ' + Math.floor((Date.now() - state.opStart) / 1000) + 's';
      }
      text = 'items ' + items + ' · empty ' + empty + ' · crates ' + crates + extra;
      cls = 'status' + (err ? ' err' : '');
    } catch (e) {
      text = 'FMV not ready — re-run install.mjs';
      cls = 'status err';
    }
    // only touch the DOM when the rendered state actually changed
    if (el.textContent !== text || el.className !== cls) {
      el.textContent = text;
      el.className = cls;
    }
  }
  function setUI() {
    if (!dot) return;
    dot.className = 'dot' + (state.running || state.busy ? ' busy' : '');
    const dis = state.busy || state.running;
    const stopping = state.stop;
    // toggle buttons: stay clickable while THEIR loop runs (click = STOP)
    if (autoOrdersBtn) {
      autoOrdersBtn.textContent = state.mode === 'orders'
        ? (stopping ? '■ STOPPING…' : '■ STOP')
        : '▶ Auto Orders';
      autoOrdersBtn.classList.toggle('on', state.mode === 'orders');
      autoOrdersBtn.classList.toggle('stopping', stopping && state.mode === 'orders');
      autoOrdersBtn.disabled = state.busy || (state.running && state.mode !== 'orders');
    }
    if (autoClearBtn) {
      autoClearBtn.textContent = state.mode === 'clear'
        ? (stopping ? '■ STOPPING…' : '■ STOP')
        : '⚡ Auto Clear';
      autoClearBtn.classList.toggle('on', state.mode === 'clear');
      autoClearBtn.classList.toggle('stopping', stopping && state.mode === 'clear');
      autoClearBtn.disabled = state.busy || (state.running && state.mode !== 'clear');
    }
    if (autoMarketBtn) {
      autoMarketBtn.textContent = state.mode === 'market'
        ? (stopping ? '■ STOPPING…' : '■ STOP')
        : '▶ Auto Flash Deals';
      autoMarketBtn.classList.toggle('on', state.mode === 'market');
      autoMarketBtn.classList.toggle('stopping', stopping && state.mode === 'market');
      autoMarketBtn.disabled = state.busy || (state.running && state.mode !== 'market');
    }
    // deal-type checkboxes: locked while a DIFFERENT op runs (the market
    // loop itself re-reads them every cycle, so they stay editable mid-run)
    const marketLocked = state.busy || (state.running && state.mode !== 'market');
    for (const cb of marketChecks) cb.disabled = marketLocked;
    sortBtn.disabled = dis;
    fillBtn.disabled = dis;
    harvestBtn.disabled = dis;
    planBtn.disabled = dis;
    if (visitBtn) visitBtn.disabled = dis;
    for (const b of cheatBtns) b.disabled = dis;
  }

  function buildUI() {
    const oldMenu = document.getElementById('fmv-menu');
    if (oldMenu) oldMenu.remove();
    const oldStyle = document.getElementById('fmv-menu-style');
    if (oldStyle) oldStyle.remove();
    // a previous install may have left its status interval running against
    // the (now dead) old closure — drop it so only this build updates the UI
    if (window.__FMV_statusTimer) {
      clearInterval(window.__FMV_statusTimer);
      window.__FMV_statusTimer = null;
    }
    const style = document.createElement('style');
    style.id = 'fmv-menu-style';
    style.textContent = 
      '#fmv-menu{position:fixed;top:12px;right:12px;z-index:2147483647;width:244px;'
      + '  background:rgba(9,11,9,.94);color:#b8c4b8;font:10px/1.4 ui-monospace,Consolas,Menlo,monospace;'
      + '  border:1px solid #1f2a1f;border-radius:6px;user-select:none;overflow:hidden;}'
      + '#fmv-menu .head{display:flex;align-items:center;gap:6px;padding:5px 8px;cursor:move;touch-action:none;'
      + '  border-bottom:1px solid #1f2a1f;}'
      + '#fmv-menu .title{font-weight:700;font-size:10.5px;color:#ffd700;flex:1;letter-spacing:.5px;}'
      + '#fmv-menu .fold{color:#5a6a5a;font-size:10px;width:14px;height:14px;display:flex;align-items:center;'
      + '  justify-content:center;border:1px solid transparent;border-radius:3px;cursor:pointer;}'
      + '#fmv-menu .fold:hover{color:#b8c4b8;border-color:#2a362a;}'
      + '#fmv-menu .dot{width:6px;height:6px;border-radius:50%;background:#ffd700;position:relative;cursor:pointer;}'
      + '#fmv-menu .dot::after{content:"";position:absolute;left:-7px;top:-7px;width:20px;height:20px;}' // hitbox: 6px visual, 20px target
      + '#fmv-menu .dot.busy{background:#ff5a4e;animation:pulse 1s infinite;}'
      + '@keyframes pulse{50%{opacity:.3}}'
      + '#fmv-menu .body{padding:5px 6px 6px;}'
      + '#fmv-menu .status{padding:3px 6px;background:#0c0f0c;border:1px solid #1a221a;border-radius:4px;'
      + '  margin-bottom:5px;color:#8aa08a;font-size:9px;}'
      + '#fmv-menu .status.err{color:#ff7a6e;border-color:#3a2018;}'
      + '#fmv-menu .btns{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:5px;}' // flex: 1-2 button rows fill the full width (auto-fit grid left half-width dead space)
      + '#fmv-menu .btns:last-of-type{margin-bottom:0;}'
      + '#fmv-menu .tabs{display:flex;gap:2px;margin-bottom:5px;border-bottom:1px solid #1f2a1f;}'
      + '#fmv-menu .tabs button{flex:1;font:inherit;padding:2px 0 4px;border:none;background:none;color:#5f6f5f;'
      + '  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;'
      + '  transition:color .12s,border-color .12s;}'
      + '#fmv-menu .tabs button:hover:not(:disabled){color:#b8c4b8;}'
      + '#fmv-menu .tabs button.on{color:#ffd700;border-bottom-color:#ffd700;}'
      + '#fmv-menu button{flex:1;font:inherit;padding:3px 0;border:1px solid #223022;border-radius:3px;'
      + '  background:#101410;color:#b8c4b8;cursor:pointer;transition:color .12s,border-color .12s,background .12s;}'
      + '#fmv-menu button.toggle{color:#ffd700;border-color:#2a4a2a;}'
      + '#fmv-menu button.toggle:hover:not(:disabled){background:#0f1a0f;}'
      + '#fmv-menu button.toggle.on{color:#ff5a4e;border-color:#5a2a26;background:#1a0f0e;}'
      + '#fmv-menu .lbl{padding:3px 4px 1px;font-size:8px;color:#5f6f5f;letter-spacing:1px;text-transform:uppercase;}'
      + '#fmv-menu .chks{display:flex;flex-direction:column;gap:2px;margin-bottom:5px;}'
      + '#fmv-menu .chk{display:flex;align-items:center;gap:6px;padding:1px 4px;border-radius:3px;'
      + '  cursor:pointer;color:#9aab9a;user-select:none;}'
      + '#fmv-menu .chk:hover{background:#101410;}'
      + '#fmv-menu .chk input{accent-color:#ffd700;margin:0;cursor:pointer;}'
      + '#fmv-menu .chk input:disabled{cursor:default;opacity:.5;}'
      + '#fmv-menu .chk input:disabled + span{opacity:.5;}'
      + '#fmv-menu .chkrow{display:flex;align-items:center;gap:8px;padding:1px 4px 3px 22px;}'
      + '#fmv-menu .chkmini{display:flex;align-items:center;gap:3px;font-size:8.5px;color:#7a8a7a;'
      + '  cursor:pointer;user-select:none;}'
      + '#fmv-menu .chkmini input{accent-color:#ffd700;margin:0;cursor:pointer;}'
      + '#fmv-menu .chkmini input:disabled{cursor:default;opacity:.5;}'
      + '#fmv-menu button:hover:not(:disabled){border-color:#3a4a3a;color:#d8e4d8;}'
      + '#fmv-menu button:active:not(:disabled){transform:translateY(1px);}'
      + '#fmv-menu button:disabled{opacity:.35;cursor:default;}'
      + '#fmv-menu .logwrap{position:relative;}'
      + '#fmv-menu .log{height:16px;overflow:hidden;scrollbar-width:thin;background:#080a08;'
      + '  border:1px solid #1a221a;border-radius:4px;padding:1px 18px 1px 5px;font-size:8.5px;line-height:1.35;'
      + '  white-space:pre-wrap;word-break:break-word;margin-top:5px;}'
      + '#fmv-menu .log.open{height:96px;overflow:auto;padding:2px 18px 2px 5px;}'
      + '#fmv-menu .log:not(.open) .l{display:none;}#fmv-menu .log:not(.open) .l:last-child{display:block;}' // collapsed view = last line only, pure CSS
      + '#fmv-menu .log::-webkit-scrollbar{width:6px;}'
      + '#fmv-menu .log::-webkit-scrollbar-thumb{background:#223022;border-radius:3px;}'
      + '#fmv-menu #fmv-log-toggle{position:absolute;top:1px;right:1px;width:16px;height:13px;padding:0;'
      + '  font-size:9px;line-height:1;border:1px solid #223022;border-radius:3px;background:#101410;'
      + '  color:#5f6f5f;cursor:pointer;z-index:2;}'
      + '#fmv-menu #fmv-log-toggle:hover{color:#ffd700;}'
      + '#fmv-menu .l{color:#9aa89a;}#fmv-menu .l.warn{color:#d8c46a;}'
      + '#fmv-menu .l.ok{color:#ffd700;}#fmv-menu .l.err{color:#ff6a5e;}'
      + '#fmv-menu button.on{color:#ffd700;border-color:#2a4a2a;}'
      + '#fmv-menu button.toggle.stopping{animation:pulse 1s infinite;}' // stop requested, loop winding down
      + '#fmv-menu button:focus-visible{outline:1px solid #ffd700;outline-offset:1px;}'
      + '/* why: the game renders an #input-field text-entry overlay that can cover the menu — keep it hidden */'
      + '#input-field{display:none !important;}';


    document.head.appendChild(style);

    const el = document.createElement('div');
    el.id = 'fmv-menu';
    el.innerHTML = '<div class="head"><span class="dot" title="stop current op"></span><span class="title">FMV Bot v' + (window.FMV && window.FMV.version ? window.FMV.version : '?') + '</span>'
      + '<span class="fold">-</span></div>'
      + '<div class="body">'
      + '<div class="status" id="fmv-status">installing...</div>'
      + '<div class="tabs" role="tablist">'
      + '<button id="fmv-tab-farm" class="tab on" role="tab" aria-selected="true">Farm</button>'
      + '<button id="fmv-tab-cheat" class="tab" role="tab" aria-selected="false" tabindex="-1">Cheat</button>'
      + '</div>'
      + '<div class="tabpane" id="fmv-pane-farm">'
      + '<div class="lbl">Board</div>'
      + '<div class="btns">'
      + '<button id="fmv-plan" title="plan + merge all 5/10/15 chains (moves only families with mergeable members)">◆ Merge</button>'
      + '<button id="fmv-sort" title="regroup items by family; money/energy/gems to the bottom strip">⇅ Sort</button>'
      + '<button id="fmv-harvest" title="harvest ready crops/animals + collect drops (game machinery)">✦ Harvest</button>'
      + '<button id="fmv-fill" title="spawn crates on every empty cell until the map is full">▦ Fill</button>'
      + '</div>'
      + '<div class="lbl">Work</div>'
      + '<div class="btns">'
      + '<button id="fmv-auto-orders" class="toggle" title="toggle: claim + start orders, finish production instantly until board full / nothing to do">▶ Auto Orders</button>'
      + '<button id="fmv-auto-clear" class="toggle" title="toggle: clear sources fast (cooldowns skipped) until energy out / board full">⚡ Auto Clear</button>'
      + '</div>'
      + '<div class="lbl">Social</div>'
      + '<div class="btns">'
      + '<button id="fmv-visit" title="collect friend-reward bubbles (own farm or friend farm)">☕ Visit</button>'
      + '</div>'
      + '</div>'
      + '<div class="tabpane" id="fmv-pane-cheat" style="display:none">'
      + '<div class="lbl">Currency</div>'
      + '<div class="btns">'
      + '<button id="fmv-cheat-coins" title="grant +100k coins (client-authoritative — persists)">💰 Coins +100k</button>'
      + '<button id="fmv-cheat-gems" title="grant +1k gems">💎 Gems +1k</button>'
      + '<button id="fmv-cheat-energy" title="grant +1000 energy">⚡ Energy +1000</button>'
      + '<button id="fmv-cheat-crates" title="grant +1000 crates">📦 Crates +1000</button>'
      + '</div>'
      + '<div class="lbl">Market</div>'
      + '<div class="btns">'
      + '<button id="fmv-buy-all" title="refresh flash deals, buy all non-harvest stock (keys/chests placed on the board; other goods land in storage bubbles)">🛒 Flash Deals</button>'
      + '<button id="fmv-tap-bubbles" title="collect storage bubbles slowly — one tap per 1.5s (tapping many bubbles fast froze the game)">📦 Tap Bubbles</button>'
      + '</div>'
      + '<div class="btns">'
      + '<button id="fmv-auto-market" class="toggle" title="toggle: every cycle refresh flash deals + buy the stock of the deal types ticked below (keys/chests placed on the board; other goods land in storage bubbles)">▶ Auto Flash Deals</button>'
      + '</div>'
      + '<div class="chkrow">'
      + '<label class="chkmini" title="crops + animal produce — the farm makes these for free"><input type="checkbox" data-deal="flash_deal_ingredient"><span>Ingred</span></label>'
      + '<label class="chkmini" title="generators (seed bags, tools, animals…)"><input type="checkbox" data-deal="flash_deal_generator"><span>Gen</span></label>'
      + '<label class="chkmini" title="materials (wood, stone, building goods…)"><input type="checkbox" data-deal="flash_deal_material"><span>Mat</span></label>'
      + '</div>'
      + '<div class="chkrow">'
      + '<label class="chkmini" title="chests/crates — placed directly on the board"><input type="checkbox" data-deal="flash_deal_chest"><span>Chest</span></label>'
      + '<label class="chkmini" title="keys — placed directly on the board"><input type="checkbox" data-deal="flash_deal_key"><span>Key</span></label>'
      + '<label class="chkmini" title="greenhouse goods"><input type="checkbox" data-deal="flash_deal_greenhouse"><span>Greenhouse</span></label>'
      + '</div>'
      + '<div class="lbl">Speed</div>'
      + '<div class="btns">'
      + '<button id="fmv-cheat-regen" title="instantly finish energy/gems/crates regen timers">⏩ Finish Regen</button>'
      + '</div>'
      + '</div>'
      + '<div class="logwrap">'
      + '<div class="log"></div>'
      + '<button id="fmv-log-toggle" title="expand log">▾</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(el);


    dot = el.querySelector('.dot');
    dot.title = 'stop current op';
    dot.style.cursor = 'pointer';
    dot.addEventListener('pointerdown', (e) => e.stopPropagation());
    dot.addEventListener('click', (e) => {
      e.stopPropagation();
      if (window.FMV && window.FMV.menu) window.FMV.menu.stop();
    });
    autoOrdersBtn = el.querySelector('#fmv-auto-orders');
    autoClearBtn = el.querySelector('#fmv-auto-clear');
    autoMarketBtn = el.querySelector('#fmv-auto-market');
    sortBtn = el.querySelector('#fmv-sort');
    fillBtn = el.querySelector('#fmv-fill');
    harvestBtn = el.querySelector('#fmv-harvest');
    planBtn = el.querySelector('#fmv-plan');
    visitBtn = el.querySelector('#fmv-visit');
    const cheatCoins = el.querySelector('#fmv-cheat-coins');
    const cheatGems = el.querySelector('#fmv-cheat-gems');
    const cheatEnergy = el.querySelector('#fmv-cheat-energy');
    const cheatCrates = el.querySelector('#fmv-cheat-crates');
    const cheatRegen = el.querySelector('#fmv-cheat-regen');
    const buyAllBtn = el.querySelector('#fmv-buy-all');
    const tapBubblesBtn = el.querySelector('#fmv-tap-bubbles');
    // the auto-market toggle is handled like the other toggle loops (stays
    // clickable while its own loop runs = STOP) — exclude it from the
    // generic cheat-button lock
    cheatBtns = [...el.querySelectorAll('#fmv-pane-cheat button')]
      .filter((b) => b.id !== 'fmv-auto-market');
    marketChecks = [...el.querySelectorAll('#fmv-pane-cheat .chkmini input[type=checkbox]')];
    // restore the persisted deal-type selection (defaults on first run)
    marketDealFilter = { ...MARKET_DEAL_DEFAULT };
    try {
      const saved = JSON.parse(localStorage.getItem('fmv-market-filter') || 'null');
      if (saved && typeof saved === 'object') {
        for (const id of FLASH_DEAL_IDS) {
          if (typeof saved[id] === 'boolean') marketDealFilter[id] = saved[id];
        }
      }
    } catch (e) {}
    for (const cb of marketChecks) {
      cb.checked = !!marketDealFilter[cb.dataset.deal];
      cb.addEventListener('change', () => {
        marketDealFilter[cb.dataset.deal] = cb.checked;
        try { localStorage.setItem('fmv-market-filter', JSON.stringify(marketDealFilter)); } catch (e) {}
      });
    }
    logEl.current = el.querySelector('.log');
    const body = el.querySelector('.body');
    const fold = el.querySelector('.fold');
    const head = el.querySelector('.head');
    // persist position + fold across reinstalls (localStorage on the
    // activity origin — the game never touches our keys)
    let savedMenu = null;
    try { savedMenu = JSON.parse(localStorage.getItem('fmv-menu-state') || 'null'); } catch (e) {}
    if (savedMenu) {
      if (typeof savedMenu.left === 'number') {
        el.style.left = savedMenu.left + 'px';
        el.style.top = savedMenu.top + 'px';
        el.style.right = 'auto';
      }
      if (savedMenu.folded) {
        body.style.display = 'none';
        fold.textContent = '+';
      }
    }
    let dragMoved = false;
    head.addEventListener('pointerdown', (e) => {
      if (state.running || state.busy) return; // no yanking the panel mid-op
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
    const saveMenuState = () => {
      try {
        const r = el.getBoundingClientRect();
        localStorage.setItem('fmv-menu-state', JSON.stringify({
          left: r.left, top: r.top,
          folded: body.style.display === 'none'
        }));
      } catch (e2) {}
    };
    const endDrag = (e) => {
      if (!el.__dragging) return;
      el.__dragging = false;
      try { head.releasePointerCapture(e.pointerId); } catch (e2) {}
      saveMenuState();
    };
    head.addEventListener('pointerup', endDrag);
    head.addEventListener('pointercancel', endDrag);
    head.addEventListener('click', () => {
      if (dragMoved) return;
      body.style.display = body.style.display === 'none' ? '' : 'none';
      fold.textContent = body.style.display === 'none' ? '+' : '-';
      saveMenuState();
    });
    autoOrdersBtn.addEventListener('click', () => runToggleLoop(runOrdersLoop, 'orders'));
    autoClearBtn.addEventListener('click', () => runToggleLoop(autoClearFast, 'clear'));
    const cheatGrant = (key, amount) => () => runOp(async () => {
      const r = await window.FMV.grant([{ key: key, amount: amount }]);
      log('cheat ' + key + ': ' + (r.ok ? 'granted +' + amount : 'FAIL ' + r.reason), r.ok ? 'ok' : 'warn');
    });
    cheatCoins.addEventListener('click', cheatGrant('coins', 100000));
    cheatGems.addEventListener('click', cheatGrant('gems', 1000));
    cheatEnergy.addEventListener('click', cheatGrant('energy', 1000));
    cheatCrates.addEventListener('click', cheatGrant('crates', 1000));
    buyAllBtn.addEventListener('click', () => runOp(buyAllMarketplace));
    autoMarketBtn.addEventListener('click', () => runToggleLoop(runAutoMarketplaceLoop, 'market'));
    tapBubblesBtn.addEventListener('click', () => runOp(async () => {
      try {
        const cb = await window.FMV.collectBubbles();
        log('bubbles: ' + ((cb && cb.tapped || 0) + (cb && cb.salvagedN || 0)) + ' tapped' +
          (cb && cb.stuck ? ' · ' + cb.stuck + ' still spawning' : '') +
          (cb && !cb.ok ? ' · ' + cb.reason : ''), cb && cb.ok ? 'ok' : 'warn');
      } catch (e) { log('bubble collect fail: ' + (e && e.message), 'warn'); }
    }));
    cheatRegen.addEventListener('click', () => runOp(() => {
      const r = window.FMV.finishTimers('regenerate_');
      log('regen: ' + (r.ok ? 'finished ' + r.finished + ' regen-timers' : 'FAIL ' + r.reason), r.ok ? 'ok' : 'warn');
    }));
    sortBtn.addEventListener('click', () => runOp(sortBoard));
    fillBtn.addEventListener('click', () => runOp(phaseFill));
    visitBtn.addEventListener('click', () => runOp(collectVisits));
    harvestBtn.addEventListener('click', () => runOp(harvestAll));
    planBtn.addEventListener('click', () => runOp(phasePlanMerge));
    const logToggle = el.querySelector('#fmv-log-toggle');
    logToggle.addEventListener('click', () => {
      const open = logEl.current.classList.toggle('open');
      logToggle.textContent = open ? '▴' : '▾';
      logToggle.title = open ? 'collapse log' : 'expand log';
      updateLogView();
    });
    const tabFarm = el.querySelector('#fmv-tab-farm');
    const tabCheat = el.querySelector('#fmv-tab-cheat');
    const paneFarm = el.querySelector('#fmv-pane-farm');
    const paneCheat = el.querySelector('#fmv-pane-cheat');
    const selectTab = (name) => {
      const isCheat = name === 'cheat';
      paneFarm.style.display = isCheat ? 'none' : '';
      paneCheat.style.display = isCheat ? '' : 'none';
      tabFarm.classList.toggle('on', !isCheat);
      tabCheat.classList.toggle('on', isCheat);
      tabFarm.setAttribute('aria-selected', isCheat ? 'false' : 'true');
      tabCheat.setAttribute('aria-selected', isCheat ? 'true' : 'false');
      tabFarm.tabIndex = isCheat ? -1 : 0;
      tabCheat.tabIndex = isCheat ? 0 : -1;
    };
    tabFarm.addEventListener('click', () => selectTab('farm'));
    tabCheat.addEventListener('click', () => selectTab('cheat'));
    el.querySelector('.tabs').addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const go = e.key === 'ArrowRight' ? 'cheat' : 'farm';
      selectTab(go);
      (go === 'cheat' ? tabCheat : tabFarm).focus();
    });
  }

  // ── install ──────────────────────────────────────────────────────────────
  buildUI();
  window.FMV.menu = {
    orders: () => runOp(orders),
    sort: () => runOp(sortBoard),
    harvest: () => runOp(harvestAll),
    fill: () => runOp(phaseFill),
    planMerge: () => runOp(phasePlanMerge),
    autoOrders: () => runToggleLoop(runOrdersLoop, 'orders'),
    autoClear: () => runToggleLoop(autoClearFast, 'clear'),
    buyAll: () => runOp(buyAllMarketplace),
    autoMarket: () => runToggleLoop(runAutoMarketplaceLoop, 'market'),
    visit: () => runOp(collectVisits),
    removeHalfCrates: () => runOp(removeHalfCrates),
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
    busy: () => state.busy || state.running,
    version: window.__FMV_version || '1.14.1'
  };
  setUI();
  log('menu v' + (window.__FMV_version || '1.14.1') + ' installed', 'ok');
  refreshStatus();
  if (!window.__FMV_statusTimer) window.__FMV_statusTimer = setInterval(refreshStatus, 2500);
  return { ok: true };
})();`);
