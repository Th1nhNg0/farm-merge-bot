import { CDP, attach, evalIn, findGameTarget, WS_URL } from "./src/cdp_lib.mjs";
import { MENU_SOURCE } from "./src/menu.js";
const cdp = new CDP(WS_URL);
await cdp.connect();
const target = await findGameTarget(cdp);
const sid = await attach(cdp, target.targetId);
const res = await evalIn(cdp, sid, MENU_SOURCE);
if (res.exceptionDetails) {
  const ex = res.exceptionDetails.exception;
  const desc = ex && ex.description ? ex.description : JSON.stringify(res.exceptionDetails);
  console.error("EXCEPTION:", desc.slice(0, 1500));
} else {
  console.log("ok:", JSON.stringify(res.result && res.result.value));
}
cdp.close();
