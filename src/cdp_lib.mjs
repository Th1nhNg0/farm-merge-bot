// Shared CDP client: connects to Chrome's DevTools browser websocket and
// provides attach-to-target + evaluate helpers for the game frame.
//
// The browser WS URL is auto-detected from Chrome's DevToolsActivePort file.
// Candidates, in order:
//   1. FMV_DEVPORT_FILE env override
//   2. normal Chrome profile
//   3. HTTP fallback on the standard debug port (9222)
// The launcher starts Chrome with --remote-debugging-port=9222 and
// IsolateSandboxedIframes (needed for the Discord activity iframe). Override
// with env FMV_WS if needed. Node >= 22 (built-in WebSocket).
//
// Fail-fast contract: every connection path is bounded — the WS handshake has
// a timeout, the HTTP fallback has a timeout, the candidate list is refreshed
// per retry (a stale DevToolsActivePort uuid or a Chrome that is still
// initializing can recover), and a dropped socket rejects all pending
// requests instead of hanging the caller.

import { readFileSync, existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const CONNECT_ATTEMPTS = 3;
const CONNECT_RETRY_MS = 750;
const HANDSHAKE_TIMEOUT_MS = 5000;
const FETCH_TIMEOUT_MS = 3000;
// Backstop for a request the browser accepted but never answers (normally
// replies are prompt; long Runtime.evaluate calls stay far below this).
const SEND_TIMEOUT_MS = 300000;

// Fallback: Chrome may be started with --remote-debugging-pipe AND
// --remote-debugging-port=9222; the pipe mode may skip writing
// DevToolsActivePort, so query the HTTP endpoint directly.
async function portFallback(port = 9222) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (res.ok) {
      const { webSocketDebuggerUrl } = await res.json();
      if (webSocketDebuggerUrl) return webSocketDebuggerUrl;
    }
  } catch (e) {}
  return null;
}

// Synchronous file-based candidates (safe at import time — no network).
function fileCandidates() {
  const urls = [];
  const files = [
    process.env.FMV_DEVPORT_FILE,
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
  return urls;
}

// Full candidate list (files + live HTTP fallback). Called fresh on every
// connect attempt so retries can recover from stale files / late-starting
// Chrome. Never throws — an empty list just means "retry later".
async function wsCandidates() {
  if (process.env.FMV_WS) return [process.env.FMV_WS];
  const urls = fileCandidates();
  const fallback = await portFallback();
  if (fallback) urls.push(fallback);
  return urls;
}

const WS_CANDIDATES = fileCandidates();
export const WS_URL = WS_CANDIDATES[0];

export class CDP {
  constructor(url = WS_URL) {
    this.url = url;
    this.id = 0;
    this.pending = new Map();
    this.ws = null;
    this._openReject = null;
  }
  async connect() {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    let lastErr = null;
    for (let attempt = 0; attempt < CONNECT_ATTEMPTS; attempt++) {
      // DevToolsActivePort can be stale (uuid from a dead Chrome) or missing
      // (Chrome still initializing / pipe mode) while /json/version already
      // answers. Re-resolve every attempt instead of retrying dead URLs.
      const fresh = await wsCandidates().catch(() => []);
      const urls = [this.url, ...WS_CANDIDATES, ...fresh].filter(
        (u, i, a) => u && a.indexOf(u) === i
      );
      for (const url of urls) {
        try {
          await this._open(url);
          return;
        } catch (e) {
          lastErr = e;
        }
      }
      if (attempt < CONNECT_ATTEMPTS - 1) await sleep(CONNECT_RETRY_MS);
    }
    throw new Error("ws connect error — no CDP endpoint reachable: " + lastErr?.message);
  }
  _open(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      this._openReject = reject; // lets close() abort an in-flight handshake
      const timer = setTimeout(() => {
        try { ws.close(); } catch (e) {}
        reject(new Error("ws connect timeout (" + HANDSHAKE_TIMEOUT_MS + "ms): " + url));
      }, HANDSHAKE_TIMEOUT_MS);
      const done = () => clearTimeout(timer);
      ws.onopen = () => {
        done();
        this._openReject = null;
        resolve();
      };
      ws.onerror = () => {
        done();
        try { ws.close(); } catch (e) {}
        reject(new Error("ws connect error: " + url));
      };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve, reject } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) reject(new Error(JSON.stringify(msg.error)));
          else resolve(msg.result);
        }
      };
      // Chrome died / ws dropped mid-operation: fail every pending request
      // instead of leaving install.mjs (or any caller) hanging forever.
      // Only the CURRENT socket may touch pending — a late close from a
      // failed candidate attempt must not reject live requests.
      ws.onclose = () => {
        done();
        if (this.ws !== ws) return;
        const err = new Error('CDP websocket closed');
        for (const [, p] of this.pending) p.reject(err);
        this.pending.clear();
      };
      this.ws = ws;
    });
  }
  send(method, params = {}, sessionId = null) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('CDP websocket not open'));
        return;
      }
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error('CDP request timeout: ' + method));
      }, SEND_TIMEOUT_MS);
      const done = () => clearTimeout(timer);
      // wrap resolve/reject so the backstop timer stops once settled
      this.pending.set(id, {
        resolve: (v) => { done(); resolve(v); },
        reject: (e) => { done(); reject(e); },
      });
      const msg = { id, method, params };
      if (sessionId) msg.sessionId = sessionId;
      try { this.ws.send(JSON.stringify(msg)); }
      catch (e) { done(); this.pending.delete(id); reject(e); }
    });
  }
  close() {
    // A pending handshake must not leave connect() hanging forever.
    if (this._openReject) {
      this._openReject(new Error("CDP close() called during connect"));
      this._openReject = null;
    }
    try { if (this.ws) this.ws.close(); } catch (e) {}
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
// origin, served by the CrazyGames proxy). When several discordsays iframes
// exist, each candidate is probed for the game's webpack hook and the first
// live one wins — attaching to a shell iframe would otherwise fail silently.
export async function findGameTarget(cdp) {
  const { targetInfos } = await cdp.send("Target.getTargets");
  const candidates = targetInfos.filter(
    (t) =>
      (t.type === "iframe" || t.type === "page") &&
      t.url.includes("discordsays.com")
  );
  if (!candidates.length) return null;
  if (candidates.length === 1) return candidates[0];
  for (const t of candidates) {
    try {
      const sid = await attach(cdp, t.targetId);
      const res = await evalIn(
        cdp,
        sid,
        "!!(self.webpackChunkfarm_merge_game || window.FMV)"
      );
      await cdp.send("Target.detachFromTarget", { sessionId: sid }).catch(() => {});
      if (res && res.result && res.result.value) return t;
    } catch (e) {}
  }
  return candidates[0];
}
