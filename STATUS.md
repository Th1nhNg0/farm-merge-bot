# Status — FMV injection work (session log)

Date: 2026-08-06 · Discord Activity build (discordsays.com proxy)

## What was accomplished this session

### 1. Discord-embedded game — WORKING END-TO-END
The game runs inside Discord (voice channel → Activities → Farm Merge Valley) in an
iframe served from `https://<app-id>.discordsays.com/?instance_id=...`. The whole injection
stack was adapted:

- **CDP connectivity**: the chrome-devtools MCP Chrome runs with
  `--chrome-arg=--remote-debugging-port=9222` (+ `IsolateSandboxedIframes`) — see
  `~/.config/opencode/opencode.jsonc`. `cdp_lib.mjs` auto-discovers the WS via
  DevToolsActivePort candidates + an HTTP `/json/version` fallback on port 9222.
  `findGameTarget()` matches the `discordsays.com` iframe target.
- **This build is heavily obfuscated**: runtime require names `_0x552fd9`/`_0x34ae8b`,
  the root container moved (module **69358**, export `H`), services live under
  `_nonCriticalServices` (timer/inventory/hudServiceRegistry), the farm container is
  `root()._navigation._currentMapScene` or any timer member's `_services` with `.mapGrid`.
  Most string literals AND some property names are obfuscator-mangled; the module ids for
  the I map (84511), behaviors bundle (10295 → `HC` MergeTrigger), drop system (28464),
  merge executor (60307) survived.
- **Generic hunter** (`hunter.js`): structurally re-discovers everything at runtime from
  the executed module cache — no hardcoded ids. It stubs module factories temporarily to
  enumerate executed modules without side effects, in batches with event-loop breathing
  (Discord's activity watchdog kills the frame on long main-thread stalls).
- **Live poller** (`install.mjs (poller stage)`): evaluates the poller in the live frame instead of
  navigating it — `Page.navigate` reloads restart the Discord activity (new instance_id)
  and lose the injection.
- **FMV v4** (`fmv_helper.js`): `window.FMV.board/merge/move/swap/spawnCrate/services`,
  plus `FMV.I` (getter — call it!), `FMV.mergeCtor`, `FMV.root()`, `FMV.rootServices()`.

### 2. Auto-farm loop (per user spec) — VERIFIED LIVE
`auto_farm.mjs` now does:
1. **FILL** — spawn crates on every empty cell until the map is full (crate contents ignored).
2. **PLAN** — group items into connected groups of 5 / 10 / 15 via `FMV.move`/`FMV.swap`
   (empty cells → move; full board → swap).
3. **MERGE** — merge components that are multiples of 5 in one call (5→2, 10→4, 15→6 bonus).
4. Repeats (re-fill after merges) until out of crates or no groups possible.

Verified run on the live Discord embed: 4× 10-groups, 1× 15-group, ~80× 5-groups merged;
board consolidated to higher tiers; clean stop at 0 crates.

### 3. Notable bugs found & fixed
- `window.FMV.I` is a **getter function** — `readBoard`/`merge_demo` used it without calling
  (`I.Mergeable` → undefined → everything looked non-mergeable). Fixed to `FMV.I()`.
- `auto_farm` logged the eval response object instead of the value for the crate count.
- The Discord activity **restarts** when the frame is navigated/reloaded or the main thread
  stalls for seconds — hence live poller injection + batched hunter + `waitBoard` guard.

### 4. In-game bot menu (FMV Bot overlay) — VERIFIED LIVE
`menu.js` + `install.mjs`: one command installs poller → hunter → FMV → menu overlay.
Everything is then controlled from the menu at the top-right of the game window:

- Buttons: `Sort` (regroup by key + far-corner vault), `Fill` (spawn crates on every empty
  cell), `Harvest` (tap ready harvestables), `Plan+Merge` (plan ALL 5/10/15 groups from one
  snapshot + batched execution, repeats until none possible), `Auto Farm`
  (fill → plan+merge → repeat, toggles to `STOP`), `Refresh` (items/empty/crates status).
- Options: spawn wait (crate auto-open, ms) + merge wait (animation, ms); collapsible header.
- Log panel with per-round move/swap/merge success counts; all bot logic runs IN-FRAME
  (no per-action CDP round trips).
- Verified live: menu rendered (fixed, z-index max, top-right), Plan+Merge clicked from CDP
  found 13+6+2 groups across 3 rounds — moves 48/48, swaps 14/16, merges 20/21 (the 2 failed
  swaps + 1 failed merge are expected batch artifacts: an earlier merge moved the swap
  target's cell contents). User also used Fill directly (10 crates → 0).
- `window.FMV.menu` API: `fill/planMerge/autoFarm/stop/status/running`.

### 5. Sort button — VERIFIED LIVE
- New `Sort` button regroups every movable item by id+tier into adjacent blocks.
  Trees/rocks (`tree_*`, `rock_*`), farm land tiles (`area_*`, `premium_*`) and
  no-id buildings (trainstation/dairy/bbq/market/bakery/loom/...) are never touched.
- Vault items (`coin`, `gem`/crystal, `energy`, `greenhouse`, `gazebo`) are placed as a
  SOLID block at the map's far corner — the far corner is found via connected components
  of free cells, taking the component(s) with the highest (row+col) reach (guard: never
  swallow the main farm area). Crops get the remaining free cells.
- Two-phase execution over a live grid mirror (O(1) per op, no stale snapshots):
  phase 1 vacates target cells occupied by a different key using empty cells as
  buffers; phase 2 places any same-key item into its block (items of a key are
  equivalent); op cap guards against loops; breathes every 50 ops.
- Verified live: 121 groups / 333 items — `moves 438, swaps 125, fails 0`; board dump
  confirmed same-key adjacency, fixed families unchanged.
- Far-corner vault verified live: vault zone = 71 free cells at bottom-right corner
  (rows 58-74, cols 58-74) ↔ exactly 71 vault items; after sort all 71 in the corner,
  0 crops inside the zone, 0 fails.
- Bug fixed during dev: phase 2 iterated cell OBJECTS while the op helpers expected
  `'col:row'` strings (`k.split is not a function`) — convert to string keys.

### 6. Never-move rule (family-based) — implemented, unit-tested
- User report: some non-mergeable items (train/traintrack, ground tiles) were sorted
  accidentally and must never move again. User rule: "if level 1..N can merge, the last
  level can move too" → the decision is per FAMILY, not per item.
- `computeNeverMove(board)` (menu.js + auto_farm.mjs):
  - no item id (buildings: train station, dairy, bbq, bakery, market, loom, ...) → static
  - family has NO mergeable member on the board (tree, rock, area, premium, traintrack,
    delivery, decorative, decorative_timelimitedevent, blocker, toolbox — KNOWN_STATIC) → static
  - family HAS mergeable members → movable, including non-mergeable MAX levels (corn_4, ...)
- Applied everywhere items move: sortBoard (targets/sources) AND planGroup/planAll
  (group steps, swap targets, sources) in both the menu and the CLI auto_farm.
- Unit test: corn_4→movable, traintrack/no-id/decorative/tree→static, reward_crate_key
  gold (family merges)→movable, coin_9→static only when no mergeable coin on board.
- REVERT: the accidentally-sorted static items (train/tiles) could NOT be restored — the
  game's autosave captured the sorted layout (reload didn't revert) and no history exists.
  The never-move rule prevents it from ever happening again; sort can be re-run safely.
- Cleanup: crazygames integration removed (findGameTarget = discordsays only), TODO.md and
  the extraction artifacts deleted; docs trimmed to the Discord build only.

### 7. Harvest button — VERIFIED LIVE
- Game internals mapped (Discord build):
  - Harvestable items carry behaviors: `harvestable` (config: harvests/duration/
    harvestReward), `lootable` (loot list), `hitpoints` (current/max), tile save model
    gains `data.cooldown.timerId` after a harvest (readiness signal — absence = ready).
  - Tap pipeline: pointer → `interactionService.onGestureTap` event → tap-router
    subscriber (subscribers[0].context) — `_simulateClick(gameObject)` is the game's own
    click simulator: validates `isTapValid` (whitelist), creates `req(41537).q`
    (interactionTap behavior — a marker), add+remove on the entity; the behavior engine
    reacts and the game performs the harvest. Loot drops on the board as collectable
    `_<item>` items (e.g. `_bacon`, `_wool`) that auto-collect on a short timer or on
    tap; collected amounts show in the inventory.
  - Sources (tree/rock = `source`+`mapSource` behaviors, 134 = tree 78 + rock 56) have
    energy-cost steps (resourceGate) — NOT tapped yet (needs ready-state follow-up).
- `menu.js` Harvest button: taps every ready harvestable (no tile cooldown + hp > 0),
  skips cooling/depleted, respects the tap whitelist, logs `tapped X ready, skipped Y
  cooling`. Verified live: 26-item batch harvested (cooldowns written), single pig
  harvest +6 bacon, button reported `tapped 0 ready, skipped 8 cooling` afterwards.

## Key facts / reference (Discord build)

| Id | Export | Meaning |
|---|---|---|
| 69358 | `H` | Root container: `H._nonCriticalServices` (timer/inventory/hudServiceRegistry/…), `H._navigation._currentMapScene` = farm scene |
| 84511 | `I` | Component map: `I.Mergeable='mergeable'`, `I.GridPosition='gridPosition'` (string values) |
| 10295 | `HC` | MergeTrigger ctor (`new HC({cell, chain})` → `.cell`, `.chain`) |
| 28464 | `B` | Drop system (`_performDropMerge`) |
| 60307 | — | Merge executor |
| 19376 | `H` | Crate spawn system (lazy — not always executed) |

- Farm services (46 entries incl. mapGrid, world, gridFilter, axonometricProjection,
  crateQueue, crateContent, shovelService, interactionService, …):
  `req(69358).H._nonCriticalServices.timer._updatableGroup._members[i]._services` (first with `.mapGrid`).
- Crate spawn event: `rootServices().hudServiceRegistry._activeService._commonEvents.spawnCrates`.
- Board: `S.mapGrid._cells` (Map, ~1006 cells). Entity API unmangled: `getObjectIdAndTier()`,
  `getBlueprintID()`, `hasBehavior(I.Mergeable)`, `getBehavior(I.Mergeable)`.
- Runtime selection: `_0x552fd9` owns the live store (loader `_0x34ae8b` has a near-empty
  cache). Both process fake chunks; the hunter picks by executed-module count + board size.

## Sensitive data
None stored. Nakama backend URL in decoded sources; don't commit session tokens if surfaced.
