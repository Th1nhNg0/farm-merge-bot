# Changelog

All notable changes to the FMV auto-farm injection project are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.11.0] — 2026-08-15

### Changed

- **Menu restyled (hacker-terminal theme)**: compact dark monospace panel with a
  single gold accent (`#ffd700`) — flat surfaces, thin borders, minimal color;
  tabs use a proper underline for the active state; button rows now auto-fit
  (`repeat(auto-fit, minmax(42px, 1fr))`) so 1- and 2-button rows fill the full
  width instead of leaving dead space.
- **Auto Clear pacing**: max 5 payments per cycle (`CLEAR_TAP_CAP` 40 → 5) so
  clearing no longer lags the game.
- **Cheat amounts**: Coins +50k → +100k, Energy +200 → +1000, Crates +20 →
  +1000.

### Removed

- **Analyze feature**: the Analyze tab, session popup, sprite-capture item grid
  and 2s refresh timer are gone (session counters remain available via
  `FMV.menu.stats()`).

## [1.10.0] — 2026-08-15

### Added

- **Flash Deals buy-all** (Cheat tab → Market group): refreshes the marketplace
  flash deals via the game's own refresh path (re-roll picks, reorder, re-arm
  the 4h timer, refill stock in both stores), then buys every remaining unit of
  stock of each deal. Harvest-product deals (crops + animal produce — the farm
  makes them for free) and crate deals (crates must never ride the
  storage-bubble path — documented freeze) are skipped; gem/coin deficits are
  auto-granted; goods land in storage bubbles. Exposed as
  `window.FMV.menu.buyAll`.
- **Tap Bubbles button** (Market group): collects storage bubbles at a paced
  rate (user-tunable `tapDelay` inside `collectBubbles`) — separated from the
  buy flow so purchases stay fast and freeze-free.
- **Auto-merge in Auto Clear**: every clear cycle runs a merge pass (like Auto
  Orders at its wall), consolidating the clear's own loot (wood/tools/stone),
  freeing cells and keeping the payment cap high.
- **Auto-merge fallback in Auto Orders**: on the nothing-to-do / board-full
  wall the loop merges chains; if merging changes nothing it idle-waits and
  retries instead of stopping.

### Changed

- **Clear is faster**: payment cap raised 10 → 40 and made adaptive to free
  cells (`floor(empties/4)`, min 4) so a wave never floods the board into a
  premature stop; single ground sweep per turn (stragglers caught by the next
  turn's sweep) instead of up to 4 settle rounds; `board full` is transient
  with a ground pre-sweep; idle retries at 500ms.
- **Auto Orders never self-stops** — only the ■ STOP dot/button ends it.

### Fixed

- **Clear missed tiered sources**: `tree_small/medium/large`, `rock_*` and
  `_moveable` variants (e.g. the shop's `tree_small_moveable`) were skipped by
  the exact-id scan; matching is now family-prefix based.
- **Orphan cooldown timers piled up**: the game registers `MapSourceCooldown`
  timers a tick AFTER the payment's synchronous effects, so the timerId-based
  delete ran too early (~600 orphans accumulated); now label-based delete +
  a delayed re-drop + a per-turn hygiene sweep.
- **Bubble tapping froze the game**: `_onStorageBubbleTapped` runs several
  full-grid scans and spawns every content item with a move animation;
  bursts (or fast-paced tapping on a heavy board) stalled the main thread
  into a Discord watchdog restart. Taps are now paced (`tapDelay`), and the
  crash-prone auto-collect was removed from the buy flow.

### Removed

- One-shot **Orders**, **½ Gold** and **Clear** buttons from the menu (Auto
  Orders / Auto Clear / Flash Deals / Tap Bubbles remain).

## [1.9.1] — 2026-08-15

### Changed

- **Lower-lag harvest cycles**: harvest now queues small crop batches,
  settles, collects loot and ground drops, then continues with the next batch
  instead of filling the board with pending harvest drops first.

### Fixed

- **Key/chest merges**: planner item IDs containing underscores (such as
  `reward_crate_key`) are now parsed using the final underscore as the tier
  separator, so key and chest families can merge.
- **Auto Clear retries**: temporary `nothing ready` / `collected only` gaps now
  retry with a bounded delay instead of stopping immediately; ResourceGate
  re-arming and tap-service cache validation are fixed.
- **State safety**: failed merges, moves, swaps, crate placement, and bubble
  salvage now restore or retain their content instead of leaving partial or
  lost game objects.
- **Runtime discovery and UI**: webpack runtimes are deduplicated by identity,
  and the stop control no longer collapses the menu.

### Removed

- **FF Free**: removed the Cheat-tab control and `FMV.freeFastForward()` API.

## [1.9.0] — 2026-08-15

### Added

- **Auto Orders toggle with instant production finish**: the Farm-tab toggle
  now claims + starts orders AND immediately finishes their `Order_*`
  production timers via the game's own completion path (verified: order
  state 2 → 3 instantly), so orders complete with no waiting (+5 start →
  +5 claim in the same cycle). Stops when the board is full or nothing can
  be claimed/started.
- **Auto Clear toggle (one-shot fast)**: clears tree/rock/toolbox as fast as
  possible (cooldowns skipped) until energy out / board full / nothing
  ready, paced at max 10 payments per turn (`CLEAR_TAP_CAP`).
- **Toggle UI**: running loops show ■ STOP with a red active state; the two
  loops exclude each other; labels + grouped rows (Board/Work/Social on
  Farm, Currency/Speed on Cheat).

### Fixed

- **Bubble collect was completely broken**: `tapRouter._simulateClick`
  cannot tap storage bubbles (no valid GridPosition — the router rejects
  them). Collect now uses the game's own `storageBubbleTap` family
  processor `_onStorageBubbleTapped` with settle rounds and a 90s
  double-tap guard (double-taps would duplicate the items).
- **Crate spawn froze the game loop**: crates spawned through the bubble
  path were added to the world but never placed (moveContentToCell never
  completes for crates), piling up as broken world objects with Cooldown
  behaviors — 40+ of them crashed the game's async loop. Crates are now
  placed DIRECTLY into cells (factory + GridPosition ctor + setContent),
  and `collectBubbles` salvages leftover crate bubbles by direct placement.
- **Auto Clear stalled ~20s per batch**: finishing the cooldown timer
  (`_remaining=0; _onFinish()`) removes the timer but never releases the
  worker in this build (the cooldown processor finds no entity hook), so
  the farm's ~6 workers stayed held and every payment blocked. Clear now
  calls the game's own `gameWorkers.releaseForObject(entity)` right after
  each payment (releases the worker, clears the tile, marks the source
  lootable WITH its loot) and deletes stale tile cooldown/workerData +
  orphan timers directly. Result: 50+ continuous taps per turn instead of
  6 taps + 20s stalls.
- **Sources lost their ResourceGate forever**: `_attemptPayment` consumes
  the gate and the game's re-arm never fires in this build — sources
  became unpayable ('nothing ready'). Clear now re-adds the gate
  (`req(__FMV_hcId).sh` ctor, discovered structurally, config from the
  mapSource steps) after each payment and for any hp>0 gate-less source.
- **Hidden/blurred tab could freeze the farm**: the game's own visibility
  listeners can reset its pageVisibility service to hidden (pausing ALL
  systems); pause_protect now re-applies the repair every 5s.
- **Direct `_initiateBubblePop` calls removed** (its async destroy crashed
  the game loop once); the game's own pop trigger handles bubble cleanup.

### Removed

- Gold ×5 / Open Crates cheat buttons (crate spawn/open via the Cheat tab),
  the Auto tab (Auto Orders + Auto Clear moved to Farm as toggles), and the
  old autoAll/prefs machinery.

## [1.8.0] — 2026-08-15

### Added

- **Cheat tab + exploit helpers** (research-verified 2026-08-15: the backend
  is client-authoritative — grants survive autosaves and full game restarts):
  - `FMV.grant(rewards)` — print inventory currency via the game's own
    `rewardService._parseAndClaimRewards` (validated keys: coins, gems,
    energy, crates, wood, stone) + `autosave.forceSave()`.
  - `FMV.spawn(blueprints)` / `FMV.collectBubbles()` — spawn any blueprint as
    a storage bubble (saved to `StorageBubbleModel`, restored on reload;
    collect via tap, works when the tab is visible).
  - `FMV.rigCrates(blueprint, n)` — queue the next crate opens to yield a
    chosen blueprint (`CrateContentModel._queue`, persisted).
  - `FMV.finishTimers(labelPrefix)` — instantly finish production (`Order_*`),
    regen (`regenerate_*`) and crate (`RewardCrateCooldown`) timers via the
    game's own completion path.
  - `FMV.freeFastForward(on)` — the game's dev flag
    (`fastForward.setFreeTrackerStatus`) makes fast-forward buttons free.
  - Menu Cheat tab: Coins/Gems/Energy/Crates grants, Gold crate spawn ×5,
    Rig Gold ×3, Open Crates, Finish Prod, Finish Regen, FF Free.

### Removed

- **Auto Clear automation**: the Auto-tab checkbox + Tree/Rock/Toolbox
  selectors and the `runClearLoop` loop are gone. The manual ⛏ Clear button
  stays (one-shot, now scans all three source types per press — the per-type
  checkboxes were the auto-loop's selector).

## [1.7.3] — 2026-08-15

### Fixed

- **Merge can no longer destroy items**: `FMV.merge` now verifies the source
  object is part of the live flood-fill chain AND that the chain is a legal
  merge size (5/10/15 — the game never fires off-multiple merges) BEFORE
  removing the object from the world; off-size chains are skipped and
  re-planned next round instead of losing the item. The planned chunk size is
  passed as the flood-fill cap, so a natural chunk can no longer swallow a
  neighbouring group's same-key items.
- **move/swap can no longer leave the board half-changed**: world positions
  are computed (and `gp._data` guarded) before the grid is mutated, so a
  failed position lookup can't return `ok:false` after the board already
  moved.
- **Hunter resolves the root container by full export key path** instead of
  only the first key (1.7.2 regression risk when the services collection sits
  deeper than depth 1) and clears a stale legacy `__FMV_rootKey` on install.
- **Auto Clear uses the discovered services key** (`FMV.rootServices()`) for
  the cooldown-timer skip instead of a hardcoded `_nonCriticalServices`.
- **Tap-service cache is keyed on the services object identity**: behavior-
  family registries are rebuilt as subsystems spawn/die or the farm changes,
  so a cross-rebuild cache no longer goes stale.
- **Auto Clear retries transient blockers**: `'no router'` (subscriber list
  mid-rebuild) is retried instead of stopping the loop, `'nothing ready'`
  waits for sources to regrow (with a stall cap so empty maps still auto-off),
  and tap-services absence auto-offs after 60 retries.
- **CDP client fails fast instead of hanging**: a dropped websocket rejects
  all pending requests, `send()` rejects on not-open/throwing sockets, and
  late closes from failed connect candidates can't reject live requests.
- **Fill stops when crates run out mid-pass** instead of firing
  `spawnCrate` events into the void (and over-counting stats).
- **Menu reinstall is blocked during one-shot ops** too (new `busy()` API),
  so an in-flight op can't lose its STOP control to a rebuild.

### Changed

- **Visit classification is farm-level**: every entity's `onBehaviorAdded` is
  one shared event, so visitor-vs-owner farm routing is decided with a single
  registry walk per call instead of ~177 registry visits per entity per round.
- **Removed dead settle-cache invalidation**: pause_protect swallows
  `visibilitychange` registrations by design, so the menu no longer tries to
  register one (30s expiry + focus/blur still invalidate).

## [1.7.2] — 2026-08-14

### Changed

- **Faster install (payload −12%, less in-frame CPU)**: the injected menu
  payload no longer carries full-line comments — they are stripped from the
  embedded copies (plan/util/menu) at build time, while the annotated source
  files stay untouched (no behavior change; same code, fewer bytes over CDP
  and less to parse in the game frame).
- **Hunter discovery restructured**: the component-map and MergeTrigger scans
  are fused into a single pass over executed modules, and the export walk
  carries a single `topKey` string instead of allocating a path array per
  visited object. Same discoveries, less main-thread work during install.

## [1.7.1] — 2026-08-12

### Fixed

- **Launcher works from any checkout location**: `auto-farm-install.bat`
  hardcoded `FARM_DIR` to another machine's path, so `cd /d` failed ("The
  system cannot find the path specified.") before `install.mjs` ran. The farm
  dir now resolves from the batch file's own location (`%~dp0`).
- **CDP connect survives stale/missing DevToolsActivePort**: the client picked
  the first `DevToolsActivePort` file it found and failed hard when the
  websocket URL was stale (browser uuid from a previous/dead Chrome) or the
  file was missing (Chrome still initializing / pipe mode) — even though
  `http://127.0.0.1:9222/json/version` was live. `connect()` now tries every
  candidate URL (`FMV_WS` / `FMV_DEVPORT_FILE` overrides, the default Chrome
  profile file, the HTTP fallback) with up to 3 retries before giving up.

### Changed

- **Public-release preparation**: the README was rewritten for a public
  audience (generic Chrome/CDP setup instructions instead of personal MCP
  tooling, features overview, architecture table and disclaimer); the launcher
  no longer hardcodes a personal Discord channel or the MCP profile (it opens
  `discord.com/app` with a dedicated local Chrome profile); the CDP client
  dropped the chrome-devtools-mcp profile candidate from port discovery; the
  personal handle was removed from the in-game menu header. Version strings
  synced to 1.7.1 (README / `install.mjs` were stale at 1.6.0).

## [1.7.0] — 2026-08-08

### Added

- **Auto Clear skips the chop cooldown**: paying a tree/rock/toolbox source
  starts a `MapSourceCooldown:col,row` timer in
  `FMV.root()._nonCriticalServices.timer._timerModel._timers` (a Map keyed by
  timerId — up to ~12 minutes on large trees) that holds a worker and blocks
  re-payment until it expires. The clear scan previously skipped cooling
  sources and waited the timer out; it now completes the timer instantly via
  the game's own finish path (`_remaining = 0` + `_onFinish()`), which releases
  the worker and marks the source lootable immediately — verified live
  (`66:61`: cooldown → instant lootable + worker freed, hp −1 on collect).
  Sources in the async window between payment and timer materialization
  (tile `workerData` without `cooldown`) are treated as mid-chop and skipped,
  so a source can never be paid twice before its loot is collected.

## [1.6.0] — 2026-08-08

### Added

- **`FMV.remove(col, row)`** (`fmv_helper.js`): shovel-style removal driven by the
  game's own `objectRemoval` behavior chain — the ctor is discovered
  structurally (function export of the trigger module whose instance
  `type === 'objectRemoval'`, same scan as `lootReceived`), and the entity's
  behavior-queue drains through the game's registry chain
  (`removeBehavior` → `world.removeGameObject` → destroy + cell clear, a few
  seconds per item). Guards mirror the in-game shovel: only entities with the
  `shovelable` behavior are accepted (crops, wood, stone, coins, tools, crates,
  keys…); trees, rocks, areas, buildings and blockers are rejected.
- **Farm tab `½ Gold` button** (`menu.js` `removeHalfCrates`): removes half
  (`floor(count/2)`) of the gold reward crates (`reward_crate_gold_gazebo`) via
  `FMV.remove()`, in settle rounds with rescan-between-rounds for stragglers and
  stop support; logs per-round progress. Exposed as
  `window.FMV.menu.removeHalfCrates`.

## [1.5.4] — 2026-08-08

### Changed

- **Shared game-access helpers extracted** (`src/util.js` → `window.FMVUtil`):
  `readBoard`, `tileModel`, `tileAt`, `getTapRouter`, `walkBehaviorRegistries`,
  `isProductCollectable`, `collectablesOnBoard` and `forEachCell` now live in one
  in-frame module prepended to the menu injection (same pattern as `plan.js`),
  replacing duplicated copies in `menu.js`: the identical `isProductCollectable`
  definitions, both tile save-model readers, both ground-collect scans, the
  tap-router lookup and the three behavior-registry walks (Auto Clear payment/
  loot discovery, visit tap-path discovery, visitor-entity detection). Purely
  structural — no behavior change (verified live: install, plan+merge, harvest,
  clear all pass).
- **Version is single-sourced**: `install.mjs` injects `window.__FMV_version`
  once; `fmv_helper.js` and `menu.js` read it (with a fallback) instead of
  hardcoding. The menu's stale `1.5.2` label also now matches the project
  version. README §7 updated: one place to bump instead of five.

## [1.5.3] — 2026-08-08

### Fixed

- **Harvest no longer double-harvests on-cooldown crops in a quick repeat run**:
  the game writes the cooldown (tile save model entry, `cooldown` behavior,
  timer) only when it *processes* a queued `LootReceived` — ~1 tick per item,
  ~1s/tick in a throttled background tab. A second Harvest click inside that
  lag window re-sent `LootReceived` to crops the game still saw as ready, and
  the direct path does not gate on cooldown, so the crops got harvested again
  (hp −1, loot produced, cooldown restarted). Phase 1 now keeps a session
  registry of entities it already sent `LootReceived` to and skips any of them
  for a 6s pending window (counted as `cd` in the harvest log), in addition to
  the existing tile-model cooldown check — entries are pruned lazily.

## [1.5.2] — 2026-08-08

### Fixed

- **Harvest respects the cooldown wait again** (reverts the 1.5.1 cooldown
  skip): phase 1 once more skips harvestables whose tile save model
  (`TilesStateModel_col:row`) carries a `cooldown` entry, so the game's wait
  between harvests is honored; the `tileAt` helper and the `cooling` counter
  in the harvest log are back.
- **Adaptive settles across all buttons** (shared `adaptSettle` helper):
  measures the game's real tick rate once (~300ms window, cached 30s,
  invalidated on visibility/focus changes) and derives a ~4-tick settle
  clamped to 150–1500ms. Applied to Merge (300ms fixed → adaptive per round),
  Clear (1500ms fixed → adaptive + multi-round ground-collect retry so late
  loot isn't missed), Visit (both 1500ms settles → adaptive), and the Auto
  Orders / Auto Clear cycle waits (`min(5000, settle×8)` / `min(4000,
  settle×4)`) — ~3–4× faster cycles when the tab is visible, identical pacing
  when throttled in a background tab. Harvest reuses the helper. Fill, Sort
  and the Orders button are unchanged (game-bound or already fast).

## [1.5.1] — 2026-08-08

### Fixed

- **Harvest now skips the cooldown wait**: the game does NOT enforce the tile
  cooldown on the direct `LootReceived` harvest path (verified live: harvesting
  an on-cooldown item works, hp −1, loot produced) — the cooldown entry in the
  tile save model only gates the normal tap path. The bot-side cooldown check
  was removed, so the Harvest button always harvests everything with hp left
  (previously you could only skip the wait via Sort, which moved items to cells
  without a cooldown entry). The dead `tileAt` helper and the `cooling` counter
  were dropped.
- **Harvest speedup**: the collect loop slept a fixed 1500ms before every round
  (up to 6 rounds ≈ 9s worst case). The settle time is now measured adaptively
  from the game's actual tick rate (~150ms when visible, up to 1500ms in
  throttled background tabs), and rounds scan first and only sleep when work was
  found — a full harvest finishes in ~1s instead of 4–9s.

## [1.5.0] — 2026-08-08

### Added

- **Analysis popup redesign**: the window grew from 210px to 700px with a modern
  spacious layout — 4-column stat cards with uppercase labels, pill-style
  Summary/Items tabs, and a larger header. Merges-by-item moved to an 8-column
  sprite grid: each merge now captures the source entity's atlas texture at
  merge time, crops it to a canvas dataURL (same-origin atlas PNG), and the
  Items tab shows the item sprites with count badges and hover highlight
  (hover/title shows the item key).

## [1.4.2] — 2026-08-08

### Fixed

- **Menu version text was stale**: the in-game header, `window.FMV.version` and the
  installer banner still reported 1.2.0 while the project was at 1.4.1. All three
  now read the current version, and README §7 documents the five places that must
  be kept in sync on every release bump.
- **Auto All button width**: the button sat alone in the 4-column `.btns` grid and
  only filled one cell; lone buttons now span the full row.

## [1.4.1] — 2026-08-08

### Fixed

- **Visit on friend farms**: the button only worked on your own farm. The
  friend-visit flow was reversed — when on a friend's farm the game routes
  bubble taps through the `visitorAction` behavior family (not `friendReward`),
  and the behavior-family registries are rebuilt per farm, so the cached
  owner-side processor silently did nothing ("visit: 162 bubble" with no
  rewards). Discovery now runs fresh per call and captures both tap paths
  (visitor: family simulator → `interactionHelper._createClick`, processor
  `_onActivityTapped`; owner: `_onInteractionTap` → `_processReward`), and each
  entity is dispatched by which family registry is attached to it. Also, a tap
  consumes `VisitorAction` but leaves `FriendReward` on the entity — spent
  bubbles are now filtered out (they were being re-tapped forever), and
  bubbles that carry only `VisitorAction` (no `FriendReward`) are recognized.
  The reward claim step waits for the async pipeline to land before reading
  pending rewards.

## [1.4.0] — 2026-08-08

### Added

- **Visit auto-collect** (`☕ Visit` on the Farm tab): when a friend visits the
  farm, `FriendReward` bubbles land on random board entities (train station,
  animals, plants) — the button scans every entity with the `FriendReward`
  behavior and taps them via the game's own friend-reward processor family
  (`_onInteractionTap` → `_processReward`, `_simulateClick` fallback), in
  iterative rounds with settle delays and stale-reference guards. Leftover
  rewards are claimed through the `visitorReward` service. Counted in the
  Analysis popup (`visits`), exposed as `window.FMV.menu.visit`.
- **One-shot Clear button** (`⛏ Clear` on the Farm tab): runs a single Auto
  Clear pass (collect loot → pay ready sources cheapest-first → pick up ground
  collectables) using the Tree/Rock/Toolbox prefs from the Auto tab, without
  starting the loop.

### Changed

- **Farm tab layout**: buttons now sit in a 4-column grid — row 1
  `▦ Fill` `◆ Merge` `✦ Harvest` `⇅ Sort`, row 2 `⚑ Orders` `⛏ Clear` `☕ Visit`
  — one compact screen instead of three cramped rows.

## [1.3.0] — 2026-08-08

### Added

- **Analysis popup** (`◉ Analyze` tab): a second draggable in-frame panel showing
  session counters for everything the bot has done — merges, moves, swaps,
  crates spawned, harvests, loot/ground picked, orders claimed/started, sources
  cleared, energy spent by Auto Clear, failures, and elapsed time (mm:ss). Live
  refresh every 2s while open, reset button, and the counters are exposed as
  `window.FMV.menu.stats()` / `resetStats()` for CLI reads.
- **One-shot Clear button** (`⛏ Clear` on the Farm tab): runs a single Auto
  Clear pass (collect loot → pay ready sources cheapest-first → pick up ground
  collectables) using the Tree/Rock/Toolbox prefs from the Auto tab, without
  starting the loop.

## [1.2.0] — 2026-08-07

### Added

- **Background-tab protection** (`pause_protect.js`): the game no longer freezes
  when the tab is hidden. The patch fakes `document.visibilityState`/`hidden`/
  `hasFocus`, swallows `visibilitychange`, and bridges `requestAnimationFrame`
  with a timer watchdog (the game's Pixi Ticker resolves bare rAF at call time,
  so the bridge is picked up on the next tick) — entity behavior queues keep
  draining in background tabs. Installed first by `install.mjs` and embedded in
  the poller for fresh game loads; `auto-farm-install.bat` now also passes
  `--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows` so hidden mode runs at full speed.
- **Auto All + checkboxes**: the Auto tab was redesigned — Auto Orders / Auto
  Clear are now checkboxes that select which automation loops run, and the
  master **Auto All** button starts every checked loop in parallel (each stops
  independently). Selections persist across menu reinstalls.
- **Clear source-type selection**: Auto Clear has per-type checkboxes (Tree /
  Rock / Toolbox) — only the checked source families are paid.
- **Clear wait-and-retry**: transient blockers (`energy out`, `no free
  workers`, `collected only`) no longer turn the loop off — it logs
  "waiting", polls, and resumes automatically when energy regenerates or
  workers free up. Permanent blockers (`board full`, `nothing ready`) still
  auto-off.
- **Harvest overhaul**: the Harvest button now runs the game's real harvest
  machinery — adding the game's own `LootReceived` trigger behavior harvests
  ready crops/animals (plain click simulation never did), then lootable
  harvestables are tapped to collect, and finally ground **Collectable**
  bubbles (produced items that land on empty cells) are tapped to pick up.
  All in iterative rounds with settle delays for the ~1 fps background loop.
- **Menu polish**: icon + renamed buttons (`⇅ Sort` `◆ Merge` `✦ Harvest`
  `⚑ Orders` `▦ Fill`, `▶ Auto All`/`■ STOP`), underline-style tabs, compact
  layout.

### Fixed

- **Accidental taps in Harvest/Auto Clear**: stale entity references (the board
  shifts while the bot runs) are re-verified against the scanned cell before
  every tap, and the ground-collect sweep only touches product bubbles (reward
  is a real blueprint) — coin/gem/energy reward bubbles are never clicked.

## [1.1.0] — 2026-08-07

### Added

- **Auto Clear toggle**: spends energy clearing `tree` / `rock` / `toolbox`
  sources by driving the game's own tap functions directly — the resource-gate
  payment service (`_attemptPayment`, checks workers, deducts energy, makes the
  source lootable) and the lootable collector (spawns the loot objects and
  damages hp). No click simulation, no popouts, camera-independent. One pass
  per cycle collects pending loot first, then pays ready sources cheapest-first
  (cooldown-aware via the tile save model); stops automatically when energy is
  below the cheapest tap, no workers are free, or the board has no room for
  drops. Never taps while the game tab is hidden (the paused game loop would
  queue taps up and fire them all on refocus). Exposed as
  `window.FMV.menu.autoClear`.
- **Menu Auto tab**: the overlay now has a Farm/Auto tab bar. The Auto tab
  holds the two toggles (Auto Orders, Auto Clear); the one-shot ops (Sort,
  Fill, Harvest, Plan+Merge, Orders) live in the Farm tab. Status line and log
  panel stay visible on both tabs.

### Fixed

- **Toggle labels**: the Auto Orders / Auto Clear buttons now show "STOP" only
  for the toggle that is actually running; `requestStop()` marks the active
  toggle with "Stopping…" instead of unconditionally disabling Auto Orders.

## [1.0.2] — 2026-08-07

### Fixed

- **Auto Orders toggle stuck**: turning Auto Orders off disabled the button for
  good — `requestStop()` set `autoBtn.disabled = true` but `setUI()` never
  re-enabled it, so the toggle could not be turned back on. `setUI()` now resets
  `autoBtn.disabled` on every refresh.

## [1.0.1] — 2026-08-07

### Fixed

- **Auto-orders stop**: turning off Auto Orders now halts within ~1s instead of
  running on for many seconds. Previously `orders()` kept claiming/starting
  orders after a stop request (claim waits up to 8s each), the board-full
  plan+merge still ran, and the 5s cycle wait was uninterruptible — the STOP
  click was only noticed at the next loop-top check. Now a `requestStop()`
  helper flips the button to "Stopping…" and disables it immediately, the claim
  wait and order loops exit on stop, the board-full merge guard skips when
  stopping, and the cycle wait polls `state.stop` (250ms ticks).

## [1.0.0] — 2026-08-07

Initial release. The first stable version of the Discord Activity build.

### Added

- **CDP connectivity** (`cdp_lib.mjs`): connects to the chrome-devtools MCP Chrome
  via `DevToolsActivePort` discovery + `/json/version` fallback on port 9222;
  attaches to the `discordsays.com` game iframe; evaluate helpers.
- **Runtime poller** (`poller.js`): captures every webpack runtime require into
  `window.__FMV_rt` with no hardcoded runtime name.
- **Module hunter** (`hunter.js`): picks the main runtime and structurally
  re-discovers root container, farm services, component map (`I`) and
  MergeTrigger ctor — survives obfuscation, no hardcoded module ids.
- **FMV helper** (`fmv_helper.js`): `window.FMV` API — board, merge, move, swap,
  spawnCrate, services, `req`, `I()`, `root()`, `rootServices()`.
- **In-game bot menu** (`menu.js`): draggable top-right overlay with Sort, Fill,
  Harvest, Plan+Merge, Orders, Auto Orders, Refresh + status line and log panel;
  exposes `window.FMV.menu`.
- **Auto-orders**: claims completed orders and starts every affordable order via
  the game's own `ordersService`; runs plan+merge when the board is full
  (`Auto Orders` toggle).
- **One-shot installer** (`install.mjs`): poller → hunter → FMV → menu, evaluated
  live in the frame (no reload — reloads restart the Discord activity).
- **Sort**: regroups movable items alphabetically (id, tier low→high), vault items
  (`coin`, `gem`, `energy`, `greenhouse`, `gazebo`) as a solid block at the map's
  far corner; two-phase execution over a live grid mirror.
- **Never-move rule (family-based)**: no-id buildings and families with no
  mergeable member (tree, rock, area/premium land, traintrack, delivery,
  decorative, blocker, toolbox) are never moved; families with mergeable members
  are fully movable, including max-level items (e.g. `corn_4`).
- **Harvest**: taps ready harvestables via the game's own click simulator
  (`tapRouter._simulateClick`), skips cooling/depleted items.
- **Fill**: spawns crates on every empty cell until the map is full.
- **Plan+Merge**: plans all natural 5/10/15 groups plus move/swap grouping from one
  snapshot, executes in a batched pass with event-loop breathing, repeats until no
  groups are possible.
- **Global stop**: the status dot in the menu header is a stop button (click while
  an op runs); halt halts fill/harvest/sort mid-cell and plan+merge between
  batches/rounds, with log feedback.
- **Menu polish**: header shows the bot version; the status line shows the running
  round (`rN`) and elapsed time while an op is active.
- **Docs**: README (Discord build), CHANGELOG, AGENTS.md agent guide,
  `auto-farm-install.bat` launcher. The earlier CLI `auto_farm` flow was replaced
  by in-frame bot logic + menu buttons (CLI `auto_farm`/`merge_demo` removed);
  the CrazyGames integration was dropped (`findGameTarget` matches discordsays
  only).

### Known limitations

- Injection lives in the frame and does not survive a Chrome restart / Discord
  activity restart / game reload — re-run `node src\install.mjs`.
- The Discord activity restarts if the main thread stalls for seconds — heavy work
  must stay batched with event-loop breathing.
- Trees/rocks/toolboxes (energy-cost sources) are now covered by the Auto Clear
  toggle (see 1.1.0).
- Only one game session should be open (target matched by URL).
- `window.FMV.I` is a getter function and must be called (`FMV.I()`) — bare
  `FMV.I` yields `undefined` and makes everything look non-mergeable.
- Poller fake chunk ids are `0x7ff00000 + n`; id `0` is permanently consumed
  after one use — never reuse it.
