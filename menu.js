// In-game bot menu overlay (FMV Bot). Installed by install_menu.mjs.
// All bot logic runs INSIDE the game frame — the menu buttons drive it:
//   [Fill]         spawn crates on every empty cell until the map is full
//   [Plan+Merge]   plan ALL groups (natural 5/10/15 + move/swap grouping)
//                  from one snapshot, then execute them in one batched pass
//   [Orders]       claim completed orders, then start every affordable order
//   [Auto Farm]    toggle: fill -> plan+merge -> repeat until out of crates
//                  or no groups; click again to stop after the current op
//   [Refresh]      update the items/empty/crates status line
// Options: crate auto-open wait (ms) and post-merge animation wait (ms).
// Exposes window.FMV.menu = { orders, fill, planMerge, autoFarm, stop, status, running }.

// Shared planner (plan.js) is prepended to the injected source, so the menu
// IIFE below can use window.FMVPlan (same logic as auto_farm.mjs / bench.mjs).
import { readFileSync } from "node:fs";

export const MENU_SOURCE = readFileSync(new URL("./plan.js", import.meta.url), "utf8") + "\n" + `(function(){
  if (window.FMV && window.FMV.menu && window.FMV.menu.running && window.FMV.menu.running()) {
    return { ok: false, reason: 'menu running' };
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = { busy: false, running: false, stop: false };
  const MAX_FILL_ROUNDS = 40;
  const MAX_PLAN_ROUNDS = 60;
  const MAX_CYCLE_ROUNDS = 12;

  // ── logging ──────────────────────────────────────────────────────────────
  function ts() {
    const d = new Date(), p = (n) => (n < 10 ? '0' : '') + n;
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
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
      logEl.current.scrollTop = logEl.current.scrollHeight;
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
    for (const nat of naturals) merges.push([[nat.cells[0].col, nat.cells[0].row], [nat.cells[1].col, nat.cells[1].row]]);
    for (const g of groups) merges.push([[g.group[0].col, g.group[0].row], [g.group[1].col, g.group[1].row]]);

    const out = { moves: [], swaps: [], merges: [] };
    const FMV = window.FMV;
    for (let i = 0; i < moves.length; i += 30) {
      for (const m of moves.slice(i, i + 30)) out.moves.push(FMV.move(m[0][0], m[0][1], m[1][0], m[1][1]));
      await sleep(0);
    }
    for (let i = 0; i < swaps.length; i += 30) {
      for (const m of swaps.slice(i, i + 30)) out.swaps.push(FMV.swap(m[0][0], m[0][1], m[1][0], m[1][1]));
      await sleep(0);
    }
    for (let i = 0; i < merges.length; i += 20) {
      for (const m of merges.slice(i, i + 20)) out.merges.push(FMV.merge(m[0][0], m[0][1], m[1][0], m[1][1]));
      await sleep(0);
    }
    return out;
  }

  // ── Phase 1: FILL ────────────────────────────────────────────────────────
  async function phaseFill() {
    const spawnWait = opt('spawnWait', 4000);
    let round = 0;
    let spawnedTotal = 0;
    while (round < MAX_FILL_ROUNDS) {
      round++;
      assertFMV();
      const board = readBoard();
      if (board.error) throw new Error(board.error);
      if (!board.empties.length) {
        log('map is full — no empty cells left', 'ok');
        return { filled: true, spawned: spawnedTotal };
      }
      const crates = cratesLeft();
      log('fill ' + round + ': ' + board.empties.length + ' empty cells, ' + crates + ' crates left');
      if (crates <= 0) { log('out of crates — stopping fill', 'warn'); return { filled: false, spawned: spawnedTotal }; }
      let spawned = 0;
      for (const e of board.empties) {
        if (state.stop) break;
        const r = window.FMV.spawnCrate(e.col, e.row);
        if (r && r.ok) spawned++;
        if (spawned % 50 === 0) await sleep(0);
      }
      spawnedTotal += spawned;
      log('spawned ' + spawned + '/' + board.empties.length + ' crates, waiting for auto-open...');
      await sleep(spawnWait);
    }
    log('fill hit round cap');
    return { filled: false, spawned: spawnedTotal };
  }

  // ── Phase 2+3: PLAN + MERGE ──────────────────────────────────────────────
  async function phasePlanMerge() {
    const mergeWait = opt('mergeWait', 1200);
    let round = 0;
    while (round < MAX_PLAN_ROUNDS) {
      round++;
      assertFMV();
      const board = readBoard();
      const { naturals, groups } = window.FMVPlan.planAll(board);
      if (!naturals.length && !groups.length) {
        log('no 5/10/15 group possible this round — done');
        return false;
      }
      log('round ' + round + ': ' + naturals.length + ' natural + ' + groups.length + ' grouped, executing...');
      const result = await executeBatch(naturals, groups);
      const movesOk = (result.moves || []).filter((m) => m && m.ok).length;
      const swapsOk = (result.swaps || []).filter((m) => m && m.ok).length;
      const mergesOk = (result.merges || []).filter((m) => m && m.ok).length;
      log('  moves ' + movesOk + '/' + result.moves.length +
        ', swaps ' + swapsOk + '/' + result.swaps.length +
        ', merges ' + mergesOk + '/' + result.merges.length);
      if (mergesOk === 0) { log('no merge succeeded — stopping plan phase', 'warn'); return false; }
      await sleep(mergeWait);
    }
    log('plan/merge hit round cap');
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
    // block order: ALPHABETICAL by id, then tier low → high (vault last)
    const tierCmp = (x, y) => {
      const nx = Number(x), ny = Number(y);
      if (!isNaN(nx) && !isNaN(ny)) return nx - ny;
      if (isNaN(nx) && isNaN(ny)) return String(x).localeCompare(String(y));
      return isNaN(nx) ? 1 : -1;
    };
    const byKey = (a, b) => a.id.localeCompare(b.id) || tierCmp(a.tier, b.tier);
    const normal = [...groups.values()].filter((g) => !g.vault).sort(byKey);
    const vault = [...groups.values()].filter((g) => g.vault).sort(byKey);
    const vaultN = vault.reduce((s, g) => s + g.items.length, 0);

    const free = Object.values(board.cells)
      .filter((c) => !fixedCells.has(c.col + ':' + c.row))
      .sort((a, b) => a.row - b.row || a.col - b.col);

    // far-end zone: connected components of free cells, taken from the corner
    // with the highest (row+col) reach — the vault block sits there as a solid
    // "456" block instead of being scattered through the crop area
    const freeMap = new Map(free.map((c) => [c.col + ':' + c.row, c]));
    const seenCells = new Set();
    const comps = [];
    for (const c of free) {
      const ck = c.col + ':' + c.row;
      if (seenCells.has(ck)) continue;
      const comp = [];
      const queue = [c];
      seenCells.add(ck);
      while (queue.length) {
        const cur = queue.shift();
        comp.push(cur);
        for (const d of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
          const nk = (cur.col + d[0]) + ':' + (cur.row + d[1]);
          const n = freeMap.get(nk);
          if (n && !seenCells.has(nk)) { seenCells.add(nk); queue.push(n); }
        }
      }
      comps.push(comp);
    }
    const maxRC = (comp) => comp.reduce((m, c) => Math.max(m, c.row + c.col), -1);
    const farRC = Math.max(...comps.map(maxRC));
    const zone = [];
    for (const comp of [...comps].sort((a, b) => maxRC(b) - maxRC(a))) {
      if (zone.length >= vaultN) break;
      if (maxRC(comp) < farRC - 8) break; // never swallow the main farm area
      zone.push(...comp);
    }
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
        log('sort: vault zone too small — ' + rest.missing.length + ' groups overflow into crop area', 'warn');
      }
    }
    const total = plan.reduce((s, p) => s + p.cells.length, 0);
    log('sort: ' + plan.length + ' groups, ' + total + ' items to place' +
      ' (' + fixedCells.size + ' cells fixed — no merge chain/static families stay in place' +
      (vaultN ? '; ' + vaultN + ' to vault zone: ' + zone.length + ' cells at the far corner' : '') + ')', 'ok');

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
      if (moves + swaps >= cap) break;
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
        if (moves + swaps >= cap) break;
        const t = c.col + ':' + c.row;
        const cur = mirror.get(t);
        if (cur && cur.key === p.key) { placed.add(t); continue; }
        const s = findItem(p.key);
        if (!s) { fails++; continue; }
        if (!cur) await mv(s, t); else await sw(s, t);
        if (mirror.get(t) && mirror.get(t).key === p.key) placed.add(t);
      }
    }

    log('sort done: moves ' + moves + ', swaps ' + swaps + ', fails ' + fails +
      (moves + swaps >= cap ? ' (op cap hit)' : ''));
  }

  // ── HARVEST: tap every READY harvestable via the game's own tap path ─────
  // Uses the game's own click simulator (_simulateClick on the tap router), so
  // loot, cooldowns, animations and saves are all handled by the game itself.
  // Readiness = no cooldown entry in the tile save model (the game writes it
  // on harvest) + hitpoints remaining. Tapping a cooling animal is a harmless
  // no-op (game-enforced), but we skip them to keep the log accurate.
  // Sources (tree/rock, which cost energy to produce) are not tapped yet —
  // their ready-state needs a follow-up.
  async function harvestAll() {
    assertFMV();
    const S = window.FMV.services();
    const I = window.FMV.I();
    const tapRouter = (function () {
      try { return S.interactionService.onGestureTap._subscribers[0].context; } catch (e) { return null; }
    })();
    if (!tapRouter || typeof tapRouter._simulateClick !== 'function') {
      throw new Error('tap router not found — game version changed?');
    }
    let tiles = null;
    try { tiles = window.FMV.rootServices().playerData._dataContainers['0']._data; } catch (e) {}
    let tapped = 0, cooling = 0, blocked = 0, depleted = 0;
    for (const cell of S.mapGrid._cells.values()) {
      if (!cell || !cell.content) continue;
      const e = cell.content;
      if (!e.hasBehavior || !e.hasBehavior(I.Harvestable)) continue;
      const hp = e.hasBehavior(I.Hitpoints) ? e.getBehavior(I.Hitpoints) : null;
      if (hp && typeof hp.current === 'number' && hp.current <= 0) { depleted++; continue; }
      let onCooldown = false;
      if (tiles) {
        try {
          const m = tiles['TilesStateModel_' + cell.column + ':' + cell.row];
          const tile = m && m.data && m.data.state ? m.data.state.data : null;
          onCooldown = !!(tile && tile.cooldown);
        } catch (e2) {}
      }
      if (onCooldown) { cooling++; continue; }
      let valid = true;
      try { valid = !!S.interactionWhitelistService.isTapValid({ column: cell.column, row: cell.row }); } catch (e2) {}
      if (!valid) { blocked++; continue; }
      try { tapRouter._simulateClick(e); tapped++; } catch (e2) {
        log('tap fail ' + cell.column + ':' + cell.row + ': ' + e2.message, 'warn');
      }
      if (tapped % 20 === 0) await sleep(0);
    }
    log('harvest: tapped ' + tapped + ' ready, skipped ' + cooling + ' cooling' +
      (depleted ? ', ' + depleted + ' depleted' : '') + (blocked ? ', ' + blocked + ' blocked by UI' : ''));
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
    const label = (o) => o.buildingID + '/' + o.recipe;
    const claiming = () => typeof O.isClaiming === 'function' ? O.isClaiming() : !!O.isClaiming;
    const waitForClaim = async () => {
      const deadline = Date.now() + 8000;
      while (claiming() && Date.now() < deadline) await sleep(100);
    };

    // Claim first so the reward animation can free the building and refresh its order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
      if (!order || order.state !== COMPLETE) continue;
      O.rewardOrder(order.buildingID);
      await waitForClaim();
      claimed++;
      log('claimed ' + label(order), 'ok');
    }

    // Re-read after claims because the service may replace a claimed order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
      if (!order || order.state !== STARTABLE) continue;
      const affordable = typeof O._canAffordOrder === 'function' && O._canAffordOrder(order);
      if (!affordable) {
        skipped++;
        log('skipped ' + label(order) + ' — missing ingredients', 'warn');
        continue;
      }
      O.startOrder(order.buildingID);
      started++;
      log('started ' + label(order), 'ok');
    }
    log('orders: claimed ' + claimed + ', started ' + started +
      (skipped ? ', skipped ' + skipped : ''));
  }

  // ── Auto-farm loop: FILL -> PLAN+MERGE -> repeat ─────────────────────────
  async function autoFarm() {
    if (state.running) { state.stop = true; log('stop requested — finishing current op...'); return; }
    if (state.busy) return;
    state.running = true;
    state.stop = false;
    setUI();
    let cycle = 0;
    try {
      while (state.running && !state.stop && cycle < MAX_CYCLE_ROUNDS) {
        cycle++;
        log('=== cycle ' + cycle + ': fill ===', 'ok');
        const fill = await phaseFill();
        if (!state.running || state.stop) break;
        await sleep(1500);
        log('=== cycle ' + cycle + ': plan+merge ===', 'ok');
        const progressed = await phasePlanMerge();
        if (!progressed && fill.spawned === 0) { log('no further progress — stopping'); break; }
      }
      if (cycle >= MAX_CYCLE_ROUNDS) log('hit max cycles');
    } catch (e) {
      log('ERROR: ' + (e && e.message ? e.message : e), 'err');
    }
    state.running = false;
    state.stop = false;
    setUI();
    refreshStatus();
    log('auto-farm stopped');
  }

  // ── one-shot op wrapper ──────────────────────────────────────────────────
  async function runOp(fn) {
    if (state.busy || state.running) return;
    state.busy = true;
    setUI();
    try { await fn(); } catch (e) { log('ERROR: ' + (e && e.message ? e.message : e), 'err'); }
    state.busy = false;
    setUI();
    refreshStatus();
  }

  // ── UI ───────────────────────────────────────────────────────────────────
  let dot, autoBtn, sortBtn, fillBtn, harvestBtn, planBtn, orderBtn, refreshBtn;
  function opt(id, dflt) {
    const el = document.getElementById(id);
    if (!el) return dflt;
    const v = parseInt(el.value, 10);
    return isNaN(v) || v < 0 ? dflt : v;
  }
  function refreshStatus() {
    const el = document.getElementById('fmv-status');
    if (!el) return;
    try {
      assertFMV();
      const b = readBoard();
      const crates = cratesLeft();
      const items = b.error ? '-' : b.items.length;
      const empty = b.error ? '-' : b.empties.length;
      el.textContent = 'items ' + items + ' · empty ' + empty + ' · crates ' + crates;
      el.className = 'status' + (b.error ? ' err' : '');
    } catch (e) {
      el.textContent = 'FMV not ready — re-run install_menu.mjs';
      el.className = 'status err';
    }
  }
  function setUI() {
    if (!dot) return;
    dot.className = 'dot' + (state.running || state.busy ? ' busy' : '');
    autoBtn.textContent = state.running ? 'STOP' : 'Auto Farm';
    const dis = state.busy || state.running;
    sortBtn.disabled = dis;
    fillBtn.disabled = dis;
    harvestBtn.disabled = dis;
    planBtn.disabled = dis;
    orderBtn.disabled = dis;
    refreshBtn.disabled = dis;
  }

  function buildUI() {
    const oldMenu = document.getElementById('fmv-menu');
    if (oldMenu) oldMenu.remove();
    const oldStyle = document.getElementById('fmv-menu-style');
    if (oldStyle) oldStyle.remove();
    const style = document.createElement('style');
    style.id = 'fmv-menu-style';
    style.textContent = '#fmv-menu{position:fixed;top:12px;right:12px;z-index:2147483647;width:330px;'
      + 'background:rgba(14,14,20,.93);color:#d7d7e0;font:11px/1.45 ui-monospace,Consolas,monospace;'
      + 'border:1px solid #3a3a4a;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,.5);user-select:none;}'
      + '#fmv-menu .head{display:flex;align-items:center;gap:8px;padding:6px 10px;cursor:move;touch-action:none;}'
      + '#fmv-menu .title{font-weight:700;font-size:12px;color:#9ad0ff;flex:1;}'
      + '#fmv-menu .fold{color:#7a7a88;font-size:11px;}'
      + '#fmv-menu .dot{width:8px;height:8px;border-radius:50%;background:#3d3;}'
      + '#fmv-menu .dot.busy{background:#fa0;animation:pulse 1s infinite;}'
      + '@keyframes pulse{50%{opacity:.35}}'
      + '#fmv-menu .body{padding:8px 10px 10px;border-top:1px solid #2c2c38;}'
      + '#fmv-menu .status{padding:4px 6px;background:#101018;border-radius:4px;margin-bottom:8px;}'
      + '#fmv-menu .status.err{color:#ff9a9a;}'
      + '#fmv-menu .btns{display:flex;gap:4px;margin-bottom:8px;}'
      + '#fmv-menu button{flex:1;font:inherit;padding:5px 1px;border:1px solid #3a3a4a;border-radius:4px;'
      + 'background:#1e1e2a;color:#e8e8f0;cursor:pointer;}'
      + '#fmv-menu button:hover:not(:disabled){background:#2c2c3c;}'
      + '#fmv-menu button:disabled{opacity:.45;cursor:default;}'
      + '#fmv-menu .opts{display:flex;gap:10px;margin-bottom:8px;color:#9a9aa8;}'
      + '#fmv-menu .opts label{display:flex;align-items:center;gap:4px;}'
      + '#fmv-menu .opts input{width:50px;font:inherit;background:#101018;color:#e8e8f0;'
      + 'border:1px solid #3a3a4a;border-radius:3px;padding:2px 4px;}'
      + '#fmv-menu .log{height:130px;overflow:auto;background:#0b0b12;border:1px solid #2c2c38;'
      + 'border-radius:4px;padding:4px 6px;white-space:pre-wrap;word-break:break-word;}'
      + '#fmv-menu .l{color:#b8b8c8;}#fmv-menu .l.warn{color:#ffd479;}'
      + '#fmv-menu .l.ok{color:#7ed67e;}#fmv-menu .l.err{color:#ff8f8f;}'
      + '#input-field{display:none !important;}';
    document.head.appendChild(style);

    const el = document.createElement('div');
    el.id = 'fmv-menu';
    el.innerHTML = '<div class="head"><span class="dot"></span><span class="title">FMV Bot</span>'
      + '<span class="fold">-</span></div>'
      + '<div class="body">'
      + '<div class="status" id="fmv-status">installing...</div>'
      + '<div class="btns">'
      + '<button id="fmv-sort">Sort</button>'
      + '<button id="fmv-fill">Fill</button>'
      + '<button id="fmv-harvest">Harvest</button>'
      + '</div>'
      + '<div class="btns">'
      + '<button id="fmv-plan">Plan+Merge</button>'
      + '<button id="fmv-auto">Auto Farm</button>'
      + '<button id="fmv-orders">Orders</button>'
      + '</div>'
      + '<div class="btns">'
      + '<button id="fmv-refresh">Refresh</button>'
      + '</div>'
      + '<div class="opts">'
      + '<label>spawn wait <input id="fmv-spawnWait" type="number" value="4000"></label>'
      + '<label>merge wait <input id="fmv-mergeWait" type="number" value="1200"></label>'
      + '</div>'
      + '<div class="log"></div>'
      + '</div>';
    document.body.appendChild(el);

    dot = el.querySelector('.dot');
    autoBtn = el.querySelector('#fmv-auto');
    sortBtn = el.querySelector('#fmv-sort');
    fillBtn = el.querySelector('#fmv-fill');
    harvestBtn = el.querySelector('#fmv-harvest');
    planBtn = el.querySelector('#fmv-plan');
    orderBtn = el.querySelector('#fmv-orders');
    refreshBtn = el.querySelector('#fmv-refresh');
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
    autoBtn.addEventListener('click', autoFarm);
    sortBtn.addEventListener('click', () => runOp(sortBoard));
    fillBtn.addEventListener('click', () => runOp(phaseFill));
    harvestBtn.addEventListener('click', () => runOp(harvestAll));
    planBtn.addEventListener('click', () => runOp(phasePlanMerge));
    orderBtn.addEventListener('click', () => runOp(orders));
    refreshBtn.addEventListener('click', refreshStatus);

    const hiddenInput = document.getElementById('input-field');
    if (hiddenInput) hiddenInput.style.display = 'none';
  }

  // ── install ──────────────────────────────────────────────────────────────
  buildUI();
  window.FMV.menu = {
    orders: () => runOp(orders),
    sort: () => runOp(sortBoard),
    harvest: () => runOp(harvestAll),
    fill: () => runOp(phaseFill),
    planMerge: () => runOp(phasePlanMerge),
    autoFarm,
    stop: () => { state.stop = true; log('stop requested'); },
    status: refreshStatus,
    running: () => state.running,
    version: '1.1'
  };
  setUI();
  log('menu installed — FMV ' + window.FMV.version, 'ok');
  refreshStatus();
  setInterval(refreshStatus, 2500);
  return { ok: true };
})();`;
