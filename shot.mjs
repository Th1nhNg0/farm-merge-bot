import { CDP, attach, findGameTarget, WS_URL } from "./src/cdp_lib.mjs";
import { writeFileSync } from "node:fs";
const cdp = new CDP(WS_URL);
await cdp.connect();
const { targetInfos } = await cdp.send("Target.getTargets");
const top = targetInfos.find((t) => t.type === "page" && t.url.includes("discord.com"));
if (!top) { console.log("no top target"); process.exit(1); }
const sid = await attach(cdp, top.targetId);
try { await cdp.send("Page.enable", {}, sid); } catch (e) {}
const { data } = await cdp.send("Page.captureScreenshot", { format: "png" }, sid);
writeFileSync("menu_shot.png", Buffer.from(data, "base64"));
console.log("saved");
cdp.close();
