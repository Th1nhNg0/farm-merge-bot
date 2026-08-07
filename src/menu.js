// In-game bot menu overlay (FMV Bot). Installed by install_menu.mjs.
// All bot logic runs INSIDE the game frame — the menu buttons drive it:
//   [Fill]         spawn crates on every empty cell until the map is full
//   [Plan+Merge]   plan ALL groups (natural 5/10/15 + move/swap grouping)
//                  from one snapshot, then execute them in one batched pass
//   [Orders]       claim completed orders, then start every affordable order
  //   [Auto Orders]  toggle: claim completed + start affordable orders in a loop
  //                  every few seconds until stopped; when the board fills up,
  //                  runs plan+merge to merge items and free space
//   [Refresh]      update the items/empty/crates status line
// Exposes window.FMV.menu = { orders, fill, planMerge, autoOrders, stop, status, running }.

// Shared planner (plan.js) is prepended to the injected source, so the menu
// IIFE below can use window.FMVPlan (same logic as the CLI scripts).
import { readFileSync } from "node:fs";

export const MENU_SOURCE = readFileSync(new URL("./plan.js", import.meta.url), "utf8") + "\n" + `(function(){
  if (window.FMV && window.FMV.menu && window.FMV.menu.running && window.FMV.menu.running()) {
    return { ok: false, reason: 'menu running' };
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = { busy: false, running: false, stop: false, rounds: 0, opStart: null };
  const MAX_FILL_ROUNDS = 40;
  const MAX_PLAN_ROUNDS = 60;
  const ORDERS_WAIT_MS = 5000;

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
    for (const nat of naturals) merges.push([[nat.cells[0].col, nat.cells[0].row], [nat.cells[1].col, nat.cells[1].row]]);
    for (const g of groups) merges.push([[g.group[0].col, g.group[0].row], [g.group[1].col, g.group[1].row]]);

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
      for (const m of merges.slice(i, i + 20)) out.merges.push(FMV.merge(m[0][0], m[0][1], m[1][0], m[1][1]));
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
      log('+' + spawned + '/' + board.empties.length + ' crates, opening…');
      await sleep(spawnWait);
    }
    log('fill cap');
    return { filled: false, spawned: spawnedTotal };
  }

  // ── Phase 2+3: PLAN + MERGE ──────────────────────────────────────────────
  async function phasePlanMerge() {
    const mergeWait = 300;
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
      if (state.stop) break;
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
        log('tap fail ' + cell.column + ':' + cell.row, 'warn');
      }
      if (tapped % 20 === 0) await sleep(0);
    }
    log('harvest: ' + tapped + ' tap · ' + cooling + ' cd' +
      (depleted ? ' · ' + depleted + ' dep' : '') + (blocked ? ' · ' + blocked + ' blk' : ''));
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
      while (claiming() && Date.now() < deadline) await sleep(100);
    };

    // Claim first so the reward animation can free the building and refresh its order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
      if (!order || order.state !== COMPLETE) continue;
      O.rewardOrder(order.buildingID);
      await waitForClaim();
      claimed++;
    }

    // Re-read after claims because the service may replace a claimed order.
    for (const order of (O.getCurrentOrders() || []).slice()) {
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
  }

  // ── Board-full guard: when no empty cells are left, merge to free space ───
  async function freeBoardSpace() {
    assertFMV();
    const board = readBoard();
    if (board.error) throw new Error(board.error);
    if (board.empties.length > 0) return false;
    log('board full — merging', 'warn');
    await phasePlanMerge();
    return true;
  }

  // ── Auto-orders loop: claim + start orders every few seconds; when the
  //    board fills up, run plan+merge to merge items and free space ─────────
  async function autoOrders() {
    if (state.running) { state.stop = true; log('stop — finishing op…'); return; }
    if (state.busy) return;
    state.running = true;
    state.stop = false;
    state.rounds = 0;
    state.opStart = Date.now();
    setUI();
    let cycle = 0;
    try {
      while (state.running && !state.stop) {
        cycle++;
        log('cyc ' + cycle, 'ok');
        await orders();
        await freeBoardSpace();
        await sleep(ORDERS_WAIT_MS);
      }
    } catch (e) {
      log('ERR: ' + (e && e.message ? e.message : e), 'err');
    }
    state.running = false;
    state.stop = false;
    state.opStart = null;
    setUI();
    refreshStatus();
    log('auto off');
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
  let dot, autoBtn, sortBtn, fillBtn, harvestBtn, planBtn, orderBtn;
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
    autoBtn.textContent = state.running ? 'STOP' : 'Auto Orders';
    const dis = state.busy || state.running;
    sortBtn.disabled = dis;
    fillBtn.disabled = dis;
    harvestBtn.disabled = dis;
    planBtn.disabled = dis;
    orderBtn.disabled = dis;
  }

  function buildUI() {
    const oldMenu = document.getElementById('fmv-menu');
    if (oldMenu) oldMenu.remove();
    const oldStyle = document.getElementById('fmv-menu-style');
    if (oldStyle) oldStyle.remove();
    const style = document.createElement('style');
    style.id = 'fmv-menu-style';
    style.textContent = '#fmv-menu{position:fixed;top:12px;right:12px;z-index:2147483647;width:260px;'
      + 'background:rgba(13,14,22,.82);color:#d7d7e0;font:10.5px/1.4 ui-monospace,Consolas,monospace;'
      + 'border:1px solid rgba(130,150,255,.18);border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.55);'
      + 'user-select:none;overflow:hidden;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);}'
      + '#fmv-menu .head{display:flex;align-items:center;gap:6px;padding:5px 9px;cursor:move;touch-action:none;'
      + 'background:linear-gradient(180deg,rgba(255,255,255,.05),transparent);}'
      + '#fmv-menu .title{font-weight:700;font-size:11px;color:#9ad0ff;flex:1;letter-spacing:.4px;}'
      + '#fmv-menu .fold{color:#7a7a88;font-size:10px;}'
      + '#fmv-menu .dot{width:7px;height:7px;border-radius:50%;background:#3d3;box-shadow:0 0 6px #3d3;}'
      + '#fmv-menu .dot.busy{background:#fa0;animation:pulse 1s infinite;}'
      + '@keyframes pulse{50%{opacity:.35}}'
      + '#fmv-menu .body{padding:6px 7px 8px;}'
      + '#fmv-menu .status{padding:3px 6px;background:rgba(255,255,255,.04);border-radius:6px;'
      + 'margin-bottom:6px;color:#9a9aa8;}'
      + '#fmv-menu .status.err{color:#ff9a9a;}'
      + '#fmv-menu .btns{display:flex;gap:3px;margin-bottom:6px;}'
      + '#fmv-menu button{flex:1;font:inherit;padding:4px 0;border:1px solid rgba(130,150,255,.14);'
      + 'border-radius:6px;background:rgba(255,255,255,.05);color:#e8e8f0;cursor:pointer;'
      + 'transition:background .15s,transform .05s;}'
      + '#fmv-menu button:hover:not(:disabled){background:rgba(130,150,255,.16);}'
      + '#fmv-menu button:active:not(:disabled){transform:translateY(1px);}'
      + '#fmv-menu button:disabled{opacity:.4;cursor:default;}'
      + '#fmv-menu .logwrap{position:relative;}'
      + '#fmv-menu .log{height:19px;overflow:hidden;scrollbar-width:thin;background:rgba(0,0,0,.32);'
      + 'border:1px solid rgba(255,255,255,.06);border-radius:6px;padding:2px 22px 2px 6px;'
      + 'font-size:9px;line-height:1.35;white-space:pre-wrap;word-break:break-word;}'
      + '#fmv-menu .log.open{height:100px;overflow:auto;padding:3px 22px 3px 6px;}'
      + '#fmv-menu .log::-webkit-scrollbar{width:6px;}'
      + '#fmv-menu .log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px;}'
      + '#fmv-menu #fmv-log-toggle{position:absolute;top:2px;right:2px;width:16px;height:14px;padding:0;'
      + 'font-size:9px;line-height:1;border-radius:4px;background:rgba(255,255,255,.06);'
      + 'border:1px solid rgba(130,150,255,.15);color:#8a8a99;cursor:pointer;z-index:2;}'
      + '#fmv-menu .log.open + #fmv-log-toggle{right:10px;}'
      + '#fmv-menu #fmv-log-toggle:hover{background:rgba(130,150,255,.22);color:#e8e8f0;}'
      + '#fmv-menu .l{color:#b8b8c8;}#fmv-menu .l.warn{color:#ffd479;}'
      + '#fmv-menu .l.ok{color:#7ed67e;}#fmv-menu .l.err{color:#ff8f8f;}'
      + '#input-field{display:none !important;}';
    document.head.appendChild(style);

    const el = document.createElement('div');
    el.id = 'fmv-menu';
    el.innerHTML = '<div class="head"><span class="dot" title="stop current op"></span><span class="title">FMV Bot v' + (window.FMV && window.FMV.version ? window.FMV.version : '?') + ' · weepingangel89</span>'
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
      + '<button id="fmv-auto-orders">Auto Orders</button>'
      + '<button id="fmv-orders">Orders</button>'
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
    dot.addEventListener('click', () => { if (window.FMV && window.FMV.menu) window.FMV.menu.stop(); });
    autoBtn = el.querySelector('#fmv-auto-orders');
    sortBtn = el.querySelector('#fmv-sort');
    fillBtn = el.querySelector('#fmv-fill');
    harvestBtn = el.querySelector('#fmv-harvest');
    planBtn = el.querySelector('#fmv-plan');
    orderBtn = el.querySelector('#fmv-orders');
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
    autoBtn.addEventListener('click', autoOrders);
    sortBtn.addEventListener('click', () => runOp(sortBoard));
    fillBtn.addEventListener('click', () => runOp(phaseFill));
    harvestBtn.addEventListener('click', () => runOp(harvestAll));
    planBtn.addEventListener('click', () => runOp(phasePlanMerge));
    orderBtn.addEventListener('click', () => runOp(orders));
    const logToggle = el.querySelector('#fmv-log-toggle');
    logToggle.addEventListener('click', () => {
      const open = logEl.current.classList.toggle('open');
      logToggle.textContent = open ? '▴' : '▾';
      logToggle.title = open ? 'collapse log' : 'expand log';
      updateLogView();
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
    autoOrders,
    stop: () => {
      if (state.running || state.busy) {
        state.stop = true;
        log('stop — halting', 'warn');
      }
    },
    status: refreshStatus,
    running: () => state.running,
    version: '1.0.0'
  };
  setUI();
  log('menu v' + window.FMV.version + ' installed', 'ok');
  refreshStatus();
  if (!window.__FMV_statusTimer) window.__FMV_statusTimer = setInterval(refreshStatus, 2500);
  return { ok: true };
})();`;
