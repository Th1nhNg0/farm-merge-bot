# FMV injection — drive Farm Merge Valley merges by direct function calls

Date: 2026-08-06 · Discord Activity build (served from `1187013846746005515.discordsays.com`, CrazyGames proxy)

**Status: WORKING (Discord embed).** Live merges are executed by calling the game's own webpack modules over CDP — no pyautogui, no drag simulation. The scripts re-discover the webpack internals at runtime, so they work against the game embedded in Discord as an Activity (voice channel → Activities → Farm Merge Valley).

---

## 1. How the Discord embed works

- The game runs inside the Discord Activities iframe
  (`https://<app-id>.discordsays.com/?instance_id=...&discord_proxy_ticket=...`).
  `findGameTarget()` matches the `discordsays.com` frame.
- The embedded build is a **heavily obfuscated** runtime: runtime require names
  (`_0x552fd9`/`_0x34ae8b`), most string literals AND some property names are mangled, and
  the root container module is 69358 (export `H`, services under `_nonCriticalServices`).
  Most module ids survived (I map 84511, behaviors 10295, drop system 28464, merge
  executor 60307) but are NOT relied upon.
- Fix: everything is discovered structurally at runtime:
  - `poller.js` captures the require of **every** webpack runtime that processes a fake
    chunk into `window.__FMV_rt` — no hardcoded runtime name.
  - `hunter.js` picks the runtime whose module store has the most executed modules, then
    inspects the executed exports (side-effect-free): root container = export subtree with a
    services collection (`.services` / `._nonCriticalServices`) holding timer
    (`_updatableGroup._members`), inventory (`getAmount`) and hudServiceRegistry
    (`_activeService`); farm services = first timer member's `_services` with `.mapGrid`;
    component map = export with `.Mergeable` + `.GridPosition`; MergeTrigger ctor = function
    export whose instance has `.cell` + `.chain`.
  - `fmv_helper.js` (v4) builds `window.FMV` from the discovered layout.

## 2. CDP connectivity (MCP Chrome)

The chrome-devtools MCP browser (profile `~/.cache/chrome-devtools-mcp/chrome-profile`) is
configured in `~/.config/opencode/opencode.jsonc` with
`--chrome-arg=--remote-debugging-port=9222` (+ `IsolateSandboxedIframes` so the sandboxed
Discord activity iframe gets its own CDP target). `cdp_lib.mjs` reads the MCP profile's
`DevToolsActivePort` (override: `FMV_DEVPORT_FILE` or `FMV_WS`). The MCP itself keeps using
its pipe connection — the port is additive.

## 3. The merge pipeline (all client-side)

```
player drop → entity gains InteractionDropStart (+Dropable)
  → Drop System _performDrop → _performDropMerge
  → reads entity's MergeDetection behavior {cell, chain}
  → world.removeGameObject(draggedEntity)
  → targetCell.content.addBehavior(new MergeTrigger({cell: targetCell.position, chain}))
  → Merge executor auto-fires:
      count = chain.length + 1 → 3→1, 5→2+1 leftover (bonus merge math)
      destroys chain contents, spawns result, XP, sounds, onMerge event, saves
```

The flood-fill `gridFilter.getAdjacentObjectsWithSameID(cell, spec, undefined, [I.Mergeable])`
**includes the start cell**; when calling directly from the bot, the `from` cell is filtered
out manually (the game gets it free because the cell is already empty mid-drag).

---

## 4. Files

| File | Purpose |
|---|---|
| `cdp_lib.mjs` | CDP client over Node's built-in WebSocket; finds Chrome's debug port (`DevToolsActivePort` candidates); attach + evaluate helpers; `findGameTarget()` (discordsays iframe) |
| `poller.js` | Generic poller injected into the game frame; captures all webpack runtime requires into `window.__FMV_rt` |
| `hunter.js` | In-frame module hunter: picks the main runtime, re-discovers root container / farm services / component map / MergeTrigger ctor for the current build |
| `fmv_helper.js` | `window.FMV` v4 (board / merge / move / swap / spawnCrate / services / req / I / root / rootServices) |
| `menu.js` | In-game bot menu (FMV Bot overlay, top-right of the game window): draggable panel with Sort / Fill / Harvest / Plan+Merge / Auto Orders / Orders / Refresh + status + log + wait-time options. All bot logic runs in-frame; exposes `window.FMV.menu` |
| `install.mjs` | One-shot installer — poller (if missing) → hunter (if missing) → FMV helper → menu overlay |
| `eval.mjs` | One-off `Runtime.evaluate` of a JS expression in the game frame |
| `merge_demo.mjs` | Prints mergeable clusters and fires one merge `fromCol fromRow toCol toRow` |

## 5. How to run

### Prerequisites (once per Chrome boot)

1. The chrome-devtools MCP Chrome must be running **with the port flag** (see §2 — restart
   opencode after changing the config) and Discord must be open with the activity running:
   join a voice channel → Activities → Farm Merge Valley.
2. Requires Node.js ≥ 22 (built-in `WebSocket`).

### Recommended: one-shot install + in-game menu

```powershell
node install.mjs
```

- Evaluates poller → hunter → FMV helper → menu overlay in the LIVE frame (no reload).
- Debug stages: `node install.mjs poller` (poller only) / `node install.mjs fmv` (hunter + FMV only).
- After it prints `DONE`, control everything from the **FMV Bot menu at the top-right of the
  game window** — no more node scripts per action:

| Button | What it does |
|---|---|
| `Sort` | Regroups movable items into adjacent blocks in **alphabetical order (by id, then tier low→high)**. **Never-move rule (family-based):** no-id buildings, and families with no mergeable level (tree, rock, `area`/`premium` land, traintrack, delivery, decorative, blocker, toolbox) stay in place; families WITH mergeable levels (incl. merge-chain targets like `reward_crate_gold`) are fully movable including their max-level items (e.g. `corn_4`). Vault items (`coin`, `gem`, `energy`, `greenhouse`, `gazebo`) go to a **solid block at the map's far corner** (farthest connected free region). The same rule applies to Plan+Merge's moves/swaps |
| `Harvest` | Taps every READY harvestable item (animals + max-level crops) via the game's own click simulator (`tapRouter._simulateClick`), so loot, cooldowns, animations and saves are handled by the game. Skips items on cooldown (read via the tile save model's `cooldown` field) and depleted ones. Loot goes straight to the inventory. Trees/rocks (sources, cost energy) not yet included |
| `Fill` | Spawns a crate on every empty cell until the map is full (crate contents ignored) |
| `Plan+Merge` | Plans ALL groups from one snapshot (natural 5/10/15 components + move/swap grouping), then executes them in one batched pass; repeats until no groups possible |
| `Orders` | Claims completed orders, then starts every affordable available order through the game's own `ordersService`; ingredients are deducted and the normal order timer/save path is used |
| `Auto Orders` | Toggle: claims completed orders and starts every affordable order every few seconds until stopped; click again (label becomes `STOP`) to stop after the current cycle |
| `Refresh` | Updates the `items · empty · crates` status line |

- Options row: `spawn wait` (crate auto-open wait, ms) and `merge wait` (post-merge
  animation wait, ms). Click the header bar to collapse the menu; drag the header to
  move it anywhere.
- The log panel shows every action (fill rounds, group counts, move/swap/merge success rates).
- **Re-run `install.mjs` after any Chrome restart / Discord activity restart / game
  reload** — the injection lives in the frame and does not survive a reload.

### CLI-only alternatives (debugging / scripting)

- `node eval.mjs "…"` / `node merge_demo.mjs c r c r` — one-off evals / single merge.

### Read the board

```powershell
node eval.mjs "window.FMV.board().filter(i => i.mergeable)"
```

Each entry: `{col, row, id, tier, mergeable}` (e.g. `{col: 69, row: 67, id: "wheat", tier: "2", mergeable: true}`).

### Fire a merge

```powershell
node merge_demo.mjs 68 68 68 69
```

Drops the item at (68,68) onto (68,69). Result `{ok: true, chainLen: n, total: n+1}`.

**Merge rules enforced by the game itself** (helper mirrors them):
- both cells must have content, different objects
- target must have `Mergeable` behavior
- chain (same `targetSpecification` flood-fill from the target, `from` cell filtered out, incl. the target cell) must be ≥ 2 → total ≥ 3 including the dropped item
- 2 identical items DO NOT merge (chain would be 1 after filtering)

### Spawn a box (crate)

```powershell
node eval.mjs "window.FMV.spawnCrate(73, 70)"
```

Places a crate via the game's crate-spawn system, costs 1 crate from inventory, auto-opens
into content (coins/energy/items) within ~1-2 s.

## 6. How the hunter discovers things (structural, survives obfuscation)

| Target | Discovery |
|---|---|
| Main runtime | the captured require with the most executed modules in its store (stubbed enumeration — side-effect-free) |
| Root container | executed export subtree with a services collection (`.services` or `._nonCriticalServices`) holding timer (`_updatableGroup._members`), inventory (`getAmount`) and hudServiceRegistry (`_activeService`) |
| Farm services | first timer member's `_services` with `.mapGrid` (46 entries: mapGrid, world, gridFilter, axonometricProjection, crateQueue, crateContent, shovelService, interactionService, …) |
| Component map (`I`) | executed export with `.Mergeable` and `.GridPosition` keys (`I.Mergeable === 'mergeable'` in this build) |
| MergeTrigger ctor | executed function export whose instance has `.cell` and `.chain` (module 10295 → `HC`) |
| Crate spawn event | `rootServices().hudServiceRegistry._activeService._commonEvents.spawnCrates` |

## 7. Operational notes / gotchas

- **The game target is re-discovered by URL** (`discordsays.com`) — only one game session
  should be open.
- The Discord activity **restarts** if the game frame is reloaded/navigated or the main
  thread stalls for seconds — so: never navigate the frame, and keep heavy in-page work
  batched with event-loop breathing (the hunter and the batched plan+merge do this).
- `window.FMV.I` is a getter **function** — call it: `FMV.I().Mergeable` (using `FMV.I` bare
  yields `undefined` and silently makes everything look non-mergeable).
- If the Discord Activity iframe is not exposed as a CDP target, the MCP Chrome must be
  restarted with `IsolateSandboxedIframes` enabled (already in the config).
- Module ids change on game updates — the hunter re-discovers them every run, no config.
- Run `install.mjs` only after the farm is fully loaded; the menu shows a clear error if
  the activity restarts mid-run (re-run the installer).
- The poller's fake chunk ids (`0x7ff00000 + n`) are safe: real chunk ids are small hex; the
  handler skips runtime callbacks for already-loaded ids. id `0` is permanently consumed after
  one use — never reuse it.
- Sensitive data: none stored. If session tokens from the nakama backend surface during later
  CDP work, don't commit them.
