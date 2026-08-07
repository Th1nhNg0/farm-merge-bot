// One-shot installer for the in-game bot menu (FMV Bot).
//   node src\install.mjs            full: poller -> hunter -> FMV -> menu overlay
//   node src\install.mjs poller     poller only (debug)
//   node src\install.mjs fmv        hunter + FMV only (debug)
// Requires the farm to be fully loaded (activity open). Re-run after any
// Chrome restart / Discord activity restart / game reload (the activity
// restarts on frame reload, which wipes the injection).

export const VERSION = "1.5.1";

import { CDP, attach, evalIn, findGameTarget, WS_URL } from "./cdp_lib.mjs";
import { POLLER_SOURCE } from "./poller.js";
import { HUNTER_SOURCE } from "./hunter.js";
import { FMV_HELPER_SOURCE } from "./fmv_helper.js";
import { MENU_SOURCE } from "./menu.js";
import { PAUSE_PROTECT_SOURCE } from "./pause_protect.js";

const stage = process.argv[2] || "all";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const cdp = new CDP(WS_URL);
await cdp.connect();

const target = await findGameTarget(cdp);
if (!target) throw new Error("game frame target not found — open the Discord activity first");
console.log("target:", target.targetId, target.url.slice(0, 100));

const sid = await attach(cdp, target.targetId);

const probe = async () =>
  (await evalIn(cdp, sid, "({polled: (window.__FMV_rt || []).length, hasFMV: !!window.FMV, menu: !!(window.FMV && window.FMV.menu)})")).result.value;

// Pause protection first: keeps the game loop alive in background tabs and
// repairs the current session's visibility state (idempotent).
await evalIn(cdp, sid, PAUSE_PROTECT_SOURCE);

let p = await probe();
console.log("probe:", JSON.stringify(p));

if (stage === "all" || stage === "poller") {
  if (!p.polled) {
    console.log("installing poller (live, no reload)...");
    await evalIn(cdp, sid, POLLER_SOURCE);
    for (let i = 0; i < 20 && !(p = await probe()).polled; i++) await sleep(500);
    console.log("poller captures:", p.polled);
    if (!p.polled) throw new Error("no webpack runtime captured — re-run once the activity is fully loaded");
  }
}

if (stage === "all" || stage === "fmv") {
  if (!p.hasFMV) {
    console.log("running hunter...");
    const hunt = (await evalIn(cdp, sid, HUNTER_SOURCE)).result.value;
    console.log("hunter:", JSON.stringify(hunt));
    if (hunt.ok !== true) throw new Error("hunter failed — is the farm fully loaded?");
    const res = await evalIn(cdp, sid, FMV_HELPER_SOURCE);
    console.log("FMV install:", JSON.stringify(res.result));
  }
}

if (stage === "all") {
  const menu = await evalIn(cdp, sid, MENU_SOURCE);
  console.log("menu install:", JSON.stringify(menu.result.value));

  const check = await evalIn(
    cdp,
    sid,
    `(function(){ try {
        const b = window.FMV.board();
        return { menu: !!window.FMV.menu, board: Array.isArray(b) ? b.length : b.error,
                 crates: window.FMV.rootServices().inventory.getAmount('crates') };
      } catch (e) { return { error: e.message }; } })()`
  );
  console.log("check:", JSON.stringify(check.result.value));
  console.log("DONE v" + VERSION + " — the FMV Bot menu is at the TOP-RIGHT of the game window.");
} else {
  console.log("stage '" + stage + "' done");
}

cdp.close();
