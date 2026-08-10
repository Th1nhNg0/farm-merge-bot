// Shared CDP client: connects to Chrome's DevTools browser websocket and
// provides attach-to-target + evaluate helpers for the game frame.
//
// The browser WS URL is auto-detected from Chrome's DevToolsActivePort file.
// Candidates, in order:
//   1. FMV_DEVPORT_FILE env override
//   2. chrome-devtools-mcp profile (.cache/chrome-devtools-mcp/chrome-profile)
//   3. normal Chrome profile
// The MCP-launched Chrome exposes --remote-debugging-port=9222 (see
// ~/.config/opencode/opencode.jsonc) so the scripts can attach to the browser
// that hosts the Discord-embedded game. Override with env FMV_WS if needed.
// Node >= 22 (built-in WebSocket).

import { readFileSync, existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";

// Fallback: MCP Chrome runs with --remote-debugging-pipe AND
// --remote-debugging-port=9222; the pipe mode may skip writing
// DevToolsActivePort, so query the HTTP endpoint directly.
async function portFallback(port = 9222) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`);
    if (res.ok) {
      const { webSocketDebuggerUrl } = await res.json();
      if (webSocketDebuggerUrl) return webSocketDebuggerUrl;
    }
  } catch (e) {}
  return null;
}

async function wsCandidates() {
  if (process.env.FMV_WS) return [process.env.FMV_WS];
  const urls = [];
  const files = [
    process.env.FMV_DEVPORT_FILE,
    path.join(
      os.homedir(),
      ".cache",
      "chrome-devtools-mcp",
      "chrome-profile",
      "DevToolsActivePort"
    ),
    path.join(
      os.homedir(),
      "AppData",
      "Local",
      "Google",
      "Chrome",
      "User Data",
      "DevToolsActivePort"
    ),
  ];
  for (const file of files) {
    if (file && existsSync(file)) {
      const [port, wsPath] = readFileSync(file, "utf8").trim().split(/\r?\n/);
      if (port && wsPath) urls.push(`ws://127.0.0.1:${port}${wsPath}`);
    }
  }
  const fallback = await portFallback();
  if (fallback) urls.push(fallback);
  if (!urls.length)
    throw new Error(
      "Could not find DevToolsActivePort. Start Chrome with --remote-debugging-port=9222."
    );
  return urls;
}

const WS_CANDIDATES = await wsCandidates();
export const WS_URL = WS_CANDIDATES[0];

export class CDP {
  constructor(url = WS_URL) {
    this.url = url;
    this.urls = [url, ...WS_CANDIDATES].filter(
      (u, i, a) => a.indexOf(u) === i
    );
    this.id = 0;
    this.pending = new Map();
    this.ws = null;
  }
  async connect() {
    // DevToolsActivePort can be stale (uuid from a dead Chrome) or missing
    // (Chrome still initializing / pipe mode) while /json/version already
    // answers. Try every candidate, then retry the whole set a few times.
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    let lastErr = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      for (const url of this.urls) {
        try {
          await this._open(url);
          return;
        } catch (e) {
          lastErr = e;
        }
      }
      if (attempt < 2) await sleep(750);
    }
    throw new Error("ws connect error — no CDP endpoint reachable: " + lastErr?.message);
  }
  _open(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.onopen = () => resolve();
      ws.onerror = () => {
        try { ws.close(); } catch (e) {}
        reject(new Error("ws connect error: " + url));
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve, reject } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) reject(new Error(JSON.stringify(msg.error)));
          else resolve(msg.result);
        }
      };
      this.ws = ws;
    });
  }
  send(method, params = {}, sessionId = null) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      const msg = { id, method, params };
      if (sessionId) msg.sessionId = sessionId;
      this.ws.send(JSON.stringify(msg));
    });
  }
  close() {
    this.ws.close();
  }
}

export async function attach(cdp, targetId) {
  const { sessionId } = await cdp.send("Target.attachToTarget", {
    targetId,
    flatten: true,
  });
  return sessionId;
}

export async function evalIn(cdp, sessionId, expression, opts = {}) {
  return cdp.send(
    "Runtime.evaluate",
    { expression, awaitPromise: true, returnByValue: true, ...opts },
    sessionId
  );
}

// Finds the live game frame target (only one game session should be open).
// The game runs directly in the Discord Activities iframe (discordsays.com
// origin, served by the CrazyGames proxy).
export async function findGameTarget(cdp) {
  const { targetInfos } = await cdp.send("Target.getTargets");
  return (
    targetInfos.find(
      (t) =>
        (t.type === "iframe" || t.type === "page") &&
        t.url.includes("discordsays.com")
    ) || null
  );
}
