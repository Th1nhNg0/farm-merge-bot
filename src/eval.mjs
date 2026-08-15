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

try {
  await cdp.connect();

  const target = await findGameTarget(cdp);
  if (!target) throw new Error("game iframe target not found — open the Discord activity first");

  const sid = await attach(cdp, target.targetId);
  const res = await evalIn(cdp, sid, EXPR);
  if (res.exceptionDetails) {
    const ex =
      res.exceptionDetails.exception?.description ||
      res.exceptionDetails.text ||
      JSON.stringify(res.exceptionDetails);
    console.error("EXCEPTION:", ex);
    process.exitCode = 1;
  } else if (res.result.value === undefined) {
    console.log("undefined");
  } else {
    console.log(JSON.stringify(res.result.value, null, 2));
  }
} catch (e) {
  console.error("EVAL FAILED:", e.message);
  process.exitCode = 1;
} finally {
  cdp.close();
}
