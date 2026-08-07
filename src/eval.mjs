// One-off Runtime.evaluate of a JS expression in the game frame.
//
// Usage:  node src\eval.mjs "window.FMV.board().filter(i => i.mergeable)"

import { CDP, attach, evalIn, findGameTarget, WS_URL } from "./cdp_lib.mjs";

const EXPR = process.argv[2];
if (!EXPR) {
  console.error("usage: node eval.mjs <js expression>");
  process.exit(1);
}

const cdp = new CDP(WS_URL);
await cdp.connect();

const target = await findGameTarget(cdp);
if (!target) throw new Error("game iframe target not found — open the Discord activity first");

const sid = await attach(cdp, target.targetId);
const res = await evalIn(cdp, sid, EXPR);
if (res.exceptionDetails) {
  console.error("EXCEPTION:", JSON.stringify(res.exceptionDetails, null, 2));
} else {
  console.log(JSON.stringify(res.result.value, null, 2));
}
cdp.close();
