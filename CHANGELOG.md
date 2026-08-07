# Changelog

All notable changes to the FMV auto-farm injection project are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Trees/rocks (energy-cost sources) are not harvested yet.
- Only one game session should be open (target matched by URL).
- `window.FMV.I` is a getter function and must be called (`FMV.I()`) — bare
  `FMV.I` yields `undefined` and makes everything look non-mergeable.
- Poller fake chunk ids are `0x7ff00000 + n`; id `0` is permanently consumed
  after one use — never reuse it.
