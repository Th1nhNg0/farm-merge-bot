# AGENTS.md — FMV injection (auto-farm)

Guide for AI agents and maintainers working on this codebase.

Project version: 1.3.0 (see `CHANGELOG.md`).

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
| `src/menu.js` | In-game FMV Bot overlay (top-right): Farm/Auto tabs — Sort / Fill / Harvest / Plan+Merge / Orders (Farm) and Auto Orders / Auto Clear toggles (Auto) + log panel; exposes `window.FMV.menu` |
| `src/install.mjs` | One-shot installer — poller → hunter → FMV → menu, evaluated live in the frame; exports `VERSION` |
| `src/eval.mjs` | One-off `Runtime.evaluate` in the game frame |
| `src/pause_protect.js` | Background-tab protection: fakes `document.visibilityState`/`hasFocus`, swallows `visibilitychange`, bridges `requestAnimationFrame` with a timer watchdog (the game's Pixi Ticker resolves bare rAF at call time, so the bridge is picked up on the next tick) |
| `src/plan.js` | Merge planner (5/10/15 chain grouping, move/swap ops) |
| `src/hunter.js` / `src/fmv_helper.js` | See above |
| `auto-farm-install.bat` | Launcher for `src/install.mjs` |
| `CHANGELOG.md` | Version history |

## Key facts (Discord build, verified 2026-08-06)

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
- **Source tap machinery** (Auto Clear uses this, no click simulation): every
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
- **Harvest machinery** (Harvest button): plain taps do NOT harvest crops —
  the game's harvest runs when a `LootReceived` behavior lands on the entity
  (ctor = the trigger-module export whose instance `type === 'lootReceived'`,
  module 10295 alongside MergeTrigger). Harvest = add `LootReceived` (hp -1,
  cooldown, product becomes a `lootable` bubble on the crop). Collect = tap the
  lootable (spawns the loot as **ground `Collectable` bubbles** on empty
  cells) then tap those Collectables to pick them up. The Harvest button does
  all three in iterative rounds (the ~1 fps background loop needs settle time).
- **Game pauses while the tab is hidden** (`document.visibilityState`): taps
  queue in the entity `_behaviorQueue` and all fire on refocus — Auto Clear
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
