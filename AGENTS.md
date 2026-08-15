# AGENTS.md — FMV injection (auto-farm)

Guide for AI agents and maintainers working on this codebase.

Project version: 1.13.0 (see `CHANGELOG.md`).

## What this is

CDP-based bot for Farm Merge Valley, injected into the game when it runs inside a
Discord Activity (`https://<app-id>.discordsays.com/?instance_id=...`). All bot
logic runs IN-FRAME by calling the game's own webpack modules — no pixel automation.

## Layout

| File | Purpose |
|---|---|
| `src/cdp_lib.mjs` | CDP client (Node built-in WebSocket); port discovery via `DevToolsActivePort` + `/json/version` fallback; `findGameTarget()` matches the `discordsays.com` iframe |
| `src/poller.js` | In-frame poller; captures every webpack runtime require into `window.__FMV_rt`; prepends the pause-protection patch so fresh game loads are covered |
| `src/hunter.js` | In-frame module hunter; picks the main runtime and re-discovers root container / farm services / component map / MergeTrigger ctor structurally (no hardcoded ids) |
| `src/fmv_helper.js` | `window.FMV` v4: `board` / `merge` / `move` / `swap` / `spawnCrate` / `services` / `req` / `I` / `root` / `rootServices` |
| `src/menu.js` | In-game FMV Bot overlay (top-right): Farm/Cheat tabs — Farm groups: Board (Merge/Sort/Harvest/Fill), Work (Orders / Auto Orders with instant production finish / Clear / Auto Clear), Social (Visit/½ Gold); Cheat groups: Currency grants + Speed (Finish Regen); toggle loops show ■ STOP; exposes `window.FMV.menu`; prepends `plan.js` + `util.js` sources |
| `src/util.js` | In-frame shared game-access helpers (`window.FMVUtil`): `readBoard` / `tileModel` / `tileAt` / `getTapRouter` / `walkBehaviorRegistries` / `isProductCollectable` / `collectablesOnBoard` / `forEachCell` |
| `src/install.mjs` | One-shot installer — poller → hunter → FMV → menu, evaluated live in the frame; exports `VERSION` |
| `src/eval.mjs` | One-off `Runtime.evaluate` in the game frame |
| `src/pause_protect.js` | Background-tab protection: fakes `document.visibilityState`/`hasFocus`, swallows `visibilitychange`, bridges `requestAnimationFrame` with a timer watchdog (the game's Pixi Ticker resolves bare rAF at call time, so the bridge is picked up on the next tick) |
| `src/plan.js` | Merge planner (5/10/15 chain grouping, move/swap ops) |
| `src/hunter.js` / `src/fmv_helper.js` | See above |
| `auto-farm-install.bat` | Launcher for `src/install.mjs` |
| `CHANGELOG.md` | Version history |

## Key facts (Discord build, verified 2026-08-06)

## Exploit surfaces (verified live 2026-08-15, client-authoritative backend)

- **Currency print**: `rewardService._parseAndClaimRewards([{key,amount},...])`
  (farm service `window.FMV.services().rewardService`) grants inventory
  currency and calls `autosave.forceSave()`. Verified keys: `coins`, `gems`,
  `energy`, `crates`, `wood`, `stone`. Grants persist through game restarts
  (server stores the client's save as-is). Wrapped as `FMV.grant(rewards)`.
- **Object spawn**: `rewardService._claimObjectRewards([{key,amount},...])`
  creates a storage bubble (world entity, `id='__UNIQUE__'`, NOT a grid cell)
  via `storageBubble.createBubble`; blueprint keys verified: `reward_crate_gold`,
  `reward_crate_gold_gazebo`, `cow_1..3`, `wood_1`, `stone_1`, `coin_1`,
  `gem_1`, `energy_1`, `ticket`. Bubbles are SAVED (`StorageBubbleModel`) and
  restored on reload. **Collect (verified)**: `tapRouter._simulateClick` does
  NOT work (bubbles have no valid GridPosition — off-map, the router
  rejects them). The correct path is the `storageBubbleTap` family processor
  `_onStorageBubbleTapped(bubble)` (spawns each content item via
  `_spawnObject` → world + `moveContentToCell` behaviors; items land on the
  grid after ~6-20s in hidden tabs). Never tap a bubble twice — the content
  is consumed only when the pop completes, so double-taps duplicate the
  items. Do NOT call `_initiateBubblePop` directly (its async destroy
  crashed the game loop once). Wrapped as `FMV.spawn` / `FMV.collectBubbles`
  (settle rounds + 90s cross-call double-tap guard).
- **CRATE SPAWN CAVEAT (verified, caused a freeze)**: crate blueprints must
  NEVER go through the bubble path — `_onStorageBubbleTapped` adds them to
  the world but `moveContentToCell` never completes for crates, so they pile
  up as broken world objects with Cooldown behaviors (40+ of them froze the
  game loop). Crates are placed DIRECTLY instead: factory object +
  GridPosition ctor (`new gpCtor({column,row})`, ctor obtained from any
  board entity's `getBehavior(I.GridPosition).constructor`) + world.addGameObject
  + mapGrid.setContent + position.copyFrom. Produces real crates
  (crateReward + cooldown + RewardCrateCooldown timer). The farm's gold
  crates are the GAZEBO family (`reward_crate_gold_gazebo` → object id
  `reward_crate_gold:gazebo` — what the ½ Gold button targets), NOT the
  plain `reward_crate_gold` (`reward_crate:gold`).
- **Timer finishing**: `timer._timerModel._timers` entries with `_state==='ACTIVE'`
  → `_remaining=0; _onFinish()` fires the game's own completion path. Labels:
  `RewardCrateCooldown` (3-day crates), `Order_*` (productions), `regenerate_*`
  (energy/gems/crates), `Cooldown:col,row` (source chops, used by Clear).
  Wrapped as `FMV.finishTimers(labelPrefix)`.
- **Misc**: `cheats.allowSave` defaults true; `gameObjectFactory` has
  `createById/createFromBlueprint/createFromSerializedData` (no grid-placement
  helper — placement must reuse the move/swap machinery); storage slots are
  serialized `{data, blueprint}` (removeFromStorage does NOT spawn);
  `detachedObjects` model `_store` is empty in normal play; `IAP` service
  exists but is off-limits (real money).

- Build is heavily obfuscated: runtime requires are `_0x552fd9`/`_0x34ae8b`; string
  literals and some property names are mangled. Everything is discovered
  structurally at runtime — do NOT hardcode module ids or runtime names.
- Reference module ids (unreliable, may change): 69358 root container (export `H`,
  services under `_nonCriticalServices`), 84511 component map `I`, 10295 `HC`
  MergeTrigger ctor, 28464 drop system `B`, 60307 merge executor, 19376 crate spawn.
- Farm services: `req(69358).H._nonCriticalServices.timer._updatableGroup._members[i]._services`
  (first member with `.mapGrid`). Contains mapGrid, world, gridFilter,
  axonometricProjection, crateQueue, crateContent, shovelService, interactionService…
- Board: `S.mapGrid._cells` (Map, ~1006 cells). Entity API (unmangled):
  `getObjectIdAndTier()`, `getBlueprintID()`, `hasBehavior(I.Mergeable)`,
  `getBehavior(I.Mergeable)`.
- Crate spawn event: `rootServices().hudServiceRegistry._activeService._commonEvents.spawnCrates`.
- **Source tap machinery** (Clear button uses this, no click simulation): every
  entity's `onBehaviorAdded` is a shared event with ~177 behavior-family
  registries; each registry (`_filter._behaviorTypes`) exposes
  `onGameObjectAdded._subscribers[0].context`. The resource-gate payment
  service (context has `_attemptPayment`) does: worker check
  (`gameWorkers.hasEnoughWorkers`), energy deduction (`inventory.deductItems`),
  then marks the source `lootable` (tile save model `lootable.loot`). The
  lootable collector (registry filter contains `interactionTap`+`lootable`,
  context has `_onInteractionAdded`) spawns the loot objects and -1 hp.
  `tapRouter._simulateClick` only works for harvestables (animals) — sources
  need the popout→confirm flow, so call `_attemptPayment` directly.
- **Source chop = cooldown timer**: paying a source starts a
  `MapSourceCooldown:col,row` timer in `FMV.rootServices().timer.
  _timerModel._timers` (Map keyed by timerId; entries have `_state`,
  `_remaining`, `_onFinish` — the game's own completion path) plus a worker
  hold (tile `workerData` + entity `WorkerData` behavior). The source is only
  `lootable` when the timer expires (worker released). **VERIFIED 2026-08-15:
  finishing the timer alone (`_remaining=0; _onFinish()`) does NOT release
  the worker or clear the tile in this build** — the cooldown processor
  (`_getContentByTimerID`) finds no entity with a matching `Cooldown`
  behavior, so the tile stays stuck with `workerData`+`cooldown` and the
  farm's ~6 workers block every payment ('no free workers' stall loops).
  The working skip is `gameWorkers.releaseForObject(entity)`: it removes the
  entity's `WorkerData` behavior and the game then clears the tile and marks
  the source `lootable` WITH its loot (e.g. `tool_2 x3 + tool_1`). Clear
  calls release right after each payment, so workers are instantly free and
  the loop pays continuously until energy out / board full.
- **Harvest machinery** (Harvest button): plain taps do NOT harvest crops —
  the game's harvest runs when a `LootReceived` behavior lands on the entity
  (ctor = the trigger-module export whose instance `type === 'lootReceived'`,
  module 10295 alongside MergeTrigger). Harvest = add `LootReceived` (hp -1,
  cooldown, product becomes a `lootable` bubble on the crop). Collect = tap the
  lootable (spawns the loot as **ground `Collectable` bubbles** on empty
  cells) then tap those Collectables to pick them up. The Harvest button does
  all three in iterative rounds (the ~1 fps background loop needs settle time).
- **Game pauses while the tab is hidden** (`document.visibilityState`): taps
  queue in the entity `_behaviorQueue` and all fire on refocus — Clear
  refuses to tap while hidden. The pause-protection patch (`pause_protect.js`,
  installed first by install.mjs and embedded in the poller) fakes the
  visibility state and bridges `requestAnimationFrame` with a timer watchdog,
  so the game keeps ticking in background tabs. Without Chrome flags
  (`--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows`) background tabs throttle timers
  to ~1/s, so hidden mode runs at ~1 fps (bot ops still work; game time
  advances ~100 ms per tick).
- `window.FMV.I` is a getter FUNCTION — always call it: `FMV.I().Mergeable`.
  Bare `FMV.I` yields `undefined` and makes everything look non-mergeable.

## Critical rules / gotchas

- **Never navigate or reload the game frame.** The Discord activity restarts on
  reload (new `instance_id`) and the injection is lost. Always evaluate in the live
  frame (`Runtime.evaluate`), never `Page.navigate`.
- **The activity also restarts if the main thread stalls for seconds.** Heavy
  in-page work must be batched with event-loop breathing (`await` between batches).
- **Injection does not survive** Chrome restart / activity restart / game reload —
  re-run `node src\install.mjs`.
- **Never-move rule (family-based)**: an item moves only if its family has a
  mergeable member on the board. No-id buildings (trainstation/dairy/bbq/market/
  bakery/loom…) and static families (tree, rock, area, premium, traintrack,
  delivery, decorative, decorative_timelimitedevent, blocker, toolbox) never move.
  Applied everywhere items move: sort AND plan groups (both menu and any CLI flow).
- Poller fake chunk ids are `0x7ff00000 + n`; id `0` is permanently consumed after
  one use — never reuse it.
- Only one game session should be open (`findGameTarget` matches by URL).
- CLI evaluate helpers return CDP response objects — unwrap `.result.value`.

## How to run / verify

```powershell
node src\install.mjs            # full install (poller → hunter → FMV → menu)
node src\install.mjs poller     # poller only (debug)
node src\install.mjs fmv        # hunter + FMV only (debug)
node src\eval.mjs "window.FMV.board().filter(i => i.mergeable)"
node src\eval.mjs "window.FMV.merge(68, 68, 68, 69)"
node src\eval.mjs "window.FMV.spawnCrate(73, 70)"
```

Prerequisites: Chrome running with `--remote-debugging-port=9222` and
`IsolateSandboxedIframes` — normally launched by `auto-farm-install.bat`, which
also passes the background flags (`--disable-background-timer-throttling`,
`--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows`)
so the farm keeps running while the window is hidden; the Discord activity
open; Node.js ≥ 22.

## Conventions

- JS modules (`.mjs`) for Node-side tooling; `.js` for in-frame injected sources
  (exported as `X_SOURCE` string constants).
- No test framework — verification is live CDP runs + the session logs; keep code
  side-effect-free for the hunter's stubbed enumeration.
- No lint/typecheck config in this repo (nothing to run).
- Never commit secrets: session tokens / nakama backend tokens must not be committed.
- **No auto commits or changelog updates**: never commit and never touch
  `CHANGELOG.md` / the `Project version` line unless the user explicitly asks.
  When the user does ask to commit and push code, first update `CHANGELOG.md`
  (Keep a Changelog format; patch bump for fixes, minor for features — add an
  `Added`/`Fixed` section describing the change), bump the `Project version`
  line in this file to match, and include both files in the commit.
- Commit messages use conventional prefixes (`fix:`, `feat:`, `refactor:`, `release:`).

## Session history

Prior work is logged in the git history and summarized in `CHANGELOG.md`
(1.0.0 covers the full Discord-build feature set: menu, sort, harvest, fill,
plan+merge, orders, auto orders). Old STATUS.md session logs were removed in favor
of this file.
