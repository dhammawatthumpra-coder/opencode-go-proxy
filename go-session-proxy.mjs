// OpenCode Go harness shim.
//
// Fixes what harnesses usually get wrong against Go:
//   1. missing  x-opencode-session   -> 400 MissingSessionID
//   2. auth header that does not match the API shape
//        OpenAI path     needs  Authorization: Bearer
//        Anthropic path  needs  x-api-key
//      A Claude-style harness sending x-api-key to /chat/completions gets a 401,
//      which surfaces as "Provider authentication failed".
//   3. an unstable session id, which breaks prompt-cache reuse and makes every
//      request pay full input price instead of the cached-read price.
//
// Usage:
//   node go-session-proxy.mjs
//   point the harness base URL at: http://127.0.0.1:8787/v1
//
//   node go-session-proxy.mjs --check oc_sk_...
//   sends one tiny request per API shape and prints the real error.
//
//   curl http://127.0.0.1:8787/__stats
//   running totals across this run: requests, hit%, tokens and money saved.
//
// Env:
//   PORT              preferred listen port   (default 8787, 0 = OS picks)
//   PORT_SCAN         how many ports to try upward when PORT is busy (default 20,
//                     0 = fail instead of scanning)
//   UPSTREAM          Go base URL            (default https://opencode.ai/inference/go/v1)
//   SESSION_MODE      conversation | fixed | passthrough   (default conversation)
//                       conversation - derive a stable id from system + first messages
//                                      so every turn of one chat reuses the same route
//                                      and prompt cache. A second chat that reuses the
//                                      same opener gets its own _rN id instead of
//                                      sharing the first one's cache
//                       fixed        - one id for every request
//                       passthrough  - never touch a session header the client sends
//   OPENCODE_SESSION  id used by fixed mode (default ses_proxy_local_0001)
//   LOG=1             print one line per request: path, status, session id
//   USAGE=1           print cache_read / cache_miss / in / out and the cache
//                     hit percentage parsed from the response
//   DUMP=1            with LOG, also print message count, system prompt length
//                     and the first user message, so you can see what changed
//                     between two requests that produced different session ids
//   PRICE_CACHE=      per-1M prices used only by the savings display
//   PRICE_MISS=         default 0.003 / 0.15 / 0.60, matching the README table
//   PRICE_OUT=          set these when your model is priced differently
//
// In conversation mode the derived id wins even if the client sends its own,
// because a client-generated id that changes per request is exactly what
// destroys cache affinity.

import http from "node:http";
import crypto from "node:crypto";
import { Readable, Transform } from "node:stream";
import { StringDecoder } from "node:string_decoder";

const PREFERRED_PORT = Number(process.env.PORT ?? 8787);
const PORT_SCAN = Number(process.env.PORT_SCAN ?? 20);
const UPSTREAM = (process.env.UPSTREAM ?? "https://opencode.ai/inference/go/v1").replace(/\/+$/, "");
const SESSION = process.env.OPENCODE_SESSION ?? "ses_proxy_local_0001";
const SESSION_MODE = process.env.SESSION_MODE ?? "conversation";
const LOG = process.env.LOG === "1";
const DUMP = process.env.DUMP === "1";
const USAGE = process.env.USAGE === "1";

// List prices per 1M tokens, used only by the savings display (README table).
// Override with PRICE_* when the model is priced differently.
const PRICE_CACHE = Number(process.env.PRICE_CACHE ?? 0.003);
const PRICE_MISS = Number(process.env.PRICE_MISS ?? 0.15);
const PRICE_OUT = Number(process.env.PRICE_OUT ?? 0.6);

// Running totals, served on GET /__stats and in a summary line every 10 turns.
const stats = { requests: 0, window: 0, sessions: new Set(), hit: 0, miss: 0, input: 0, output: 0 };

const round6 = (n) => Number(n.toFixed(6));

// Header names the Go gateway accepts as a session id.
const SESSION_HEADERS = [
  "x-opencode-session",
  "session_id",
  "x-session-id",
  "x-session-affinity",
  "x-conversation-id",
];

// Hop-by-hop headers must not be forwarded.
const SKIP_REQ = new Set(["host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"]);
const SKIP_RES = new Set(["connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"]);

const isAnthropicPath = (p) => /\/messages$/.test(p);
const isOpenAIPath = (p) => /(\/chat\/completions|\/responses)$/.test(p);

// UPSTREAM ends in /v1, so fold away the path prefixes clients may add.
//   /v1/chat/completions          -> /chat/completions
//   /v1/v1/messages               -> /messages
//   /openai/v1/chat/completions   -> /chat/completions
//   /anthropic/v1/messages        -> /messages
// Gemini (/google/v1beta/...) sits outside /go/v1, so it is not proxied.
function toUpstreamPath(url) {
  const q = url.indexOf("?");
  let path = q === -1 ? url : url.slice(0, q);
  const query = q === -1 ? "" : url.slice(q);

  path = path.replace(/^\/(openai|anthropic)(?=\/)/, "");
  while (path.startsWith("/v1/v1/")) path = path.slice(3);
  if (path.startsWith("/v1/")) path = path.slice(3);
  else if (path === "/v1") path = "/";

  return path + query;
}

function hasSession(headers) {
  return SESSION_HEADERS.some((h) => {
    const v = headers[h];
    return v != null && String(v).trim() !== "";
  });
}

// A stable id per conversation. The system prompt and the first user message
// never change as a chat grows, so hashing them gives the same id on every turn
// while still separating different chats. Anything that varies per request
// (timestamps, git status, cwd) inside the system prompt breaks this, so the
// debug fields make it possible to see which part moved.
function preview(v) {
  let s;
  try {
    s = typeof v === "string" ? v : JSON.stringify(v);
  } catch {
    s = String(v);
  }
  return (s ?? "").replace(/\s+/g, " ").slice(0, 60);
}

function analyzeConversation(body) {
  if (!body?.length) return {};
  let json;
  try {
    json = JSON.parse(body.toString("utf8"));
  } catch {
    return {};
  }
  const msgs = Array.isArray(json.messages) ? json.messages : Array.isArray(json.input) ? json.input : null;
  if (!msgs || msgs.length === 0) return {};

  const anchor = msgs.find((m) => m && m.role === "user") ?? msgs[0];
  if (anchor == null) return {};

  const sys = json.system ?? null;
  const head = [sys, anchor.content ?? anchor];
  const id = `ses_${crypto.createHash("sha256").update(JSON.stringify(head)).digest("hex").slice(0, 32)}`;

  return {
    id,
    debug: {
      msgs: msgs.length,
      sysLen: sys == null ? 0 : JSON.stringify(sys).length,
      first: preview(anchor.content ?? anchor),
    },
  };
}

// A second chat that happens to share the first message (harnesses often open
// with the same boilerplate reminder) would derive the same id and fight the
// first chat over one cache entry. A conversation that was already several
// messages in and suddenly presents a single message is a different chat, so
// give it its own id. Staying at one message is left alone, because that is
// also what a retry of the opening turn looks like and a retry should reuse
// the cache. Sliding-window harnesses never drop to one message, so they are
// unaffected.
const affinity = new Map();

function touchSession(base, msgs) {
  if (!base) return null;
  let e = affinity.get(base);
  if (!e) {
    e = { id: base, msgs: msgs ?? 0, rot: 0 };
    affinity.set(base, e);
  } else if (msgs === 1 && e.msgs > 1) {
    e.rot += 1;
    e.id = `${base}_r${e.rot}`;
    e.msgs = msgs;
  } else if (msgs != null) {
    e.msgs = msgs;
  }
  return e.id;
}

// Pull the key out of whichever header the client used.
function pickApiKey(headers) {
  const auth = headers["authorization"];
  if (auth) {
    const m = /^Bearer\s+(.+)$/i.exec(String(auth));
    if (m) return m[1].trim();
    return String(auth).trim();
  }
  for (const h of ["x-api-key", "api-key", "x-goog-api-key"]) {
    if (headers[h]) return String(headers[h]).trim();
  }
  return null;
}

// Put the key in the header the gateway expects for this API shape.
function shapeAuth(headers, path) {
  const key = pickApiKey(headers);
  if (!key) return;
  if (isAnthropicPath(path)) {
    headers["x-api-key"] = key;
    delete headers["api-key"];
    headers["anthropic-version"] ??= "2023-06-01";
  } else if (isOpenAIPath(path)) {
    headers["authorization"] = `Bearer ${key}`;
    delete headers["x-api-key"];
    delete headers["api-key"];
  }
}

// Tee the response through here to report cache reuse per request. Works for
// both a plain JSON body and an SSE stream: usage is usually in the last chunk,
// so keeping a tail of the stream is enough.
const USAGE_KEYS = new Set([
  "prompt_cache_hit_tokens",
  "prompt_cache_miss_tokens",
  "cache_read_input_tokens",
  "cache_creation_input_tokens",
  "cached_tokens",
  "prompt_tokens",
  "completion_tokens",
  "input_tokens",
  "output_tokens",
]);

function usageMeter(session) {
  const decoder = new StringDecoder("utf8");
  let tail = "";
  return new Transform({
    transform(chunk, enc, cb) {
      tail += decoder.write(chunk);
      if (tail.length > 32768) tail = tail.slice(-32768);
      cb(null, chunk);
    },
    flush(cb) {
      tail += decoder.end();
      const found = {};
      for (const m of tail.matchAll(/"([a-z_]+)"\s*:\s*(\d+)/g)) {
        if (USAGE_KEYS.has(m[1])) found[m[1]] = Number(m[2]);
      }
      const hit = found.prompt_cache_hit_tokens ?? found.cache_read_input_tokens ?? found.cached_tokens;
      const miss = found.prompt_cache_miss_tokens;
      const input = found.prompt_tokens ?? found.input_tokens;
      const output = found.completion_tokens ?? found.output_tokens;
      if (hit == null && input == null && output == null) return cb();

      recordUsage(session, { hit, miss, input, output });

      const parts = [];
      if (hit != null) parts.push(`cache_read=${hit}`);
      if (miss != null) parts.push(`cache_miss=${miss}`);
      if (input != null) parts.push(`in=${input}`);
      if (output != null) parts.push(`out=${output}`);
      if (hit != null && input) parts.push(`hit=${Math.round((hit / input) * 100)}%`);
      console.log(`    usage ${session ?? "-"}: ${parts.join(" ")}`);
      cb();
    },
  });
}

// Tokens that came from cache were billed at the cheap rate instead of the
// full input rate, so `hit` is exactly the number of tokens saved.
function recordUsage(session, u) {
  if (u.hit == null && u.input == null && u.output == null) return;
  const input = u.input ?? (u.hit ?? 0) + (u.miss ?? 0);
  const hit = u.hit ?? 0;
  stats.hit += hit;
  stats.miss += u.miss ?? Math.max(0, input - hit);
  stats.input += input;
  stats.output += u.output ?? 0;
  if (session) stats.sessions.add(session);

  stats.window += 1;
  if (stats.window % 10 === 0) {
    const v = statsView();
    console.log(`  stats ${v.requests} req / ${v.sessions} ses: hit=${v.hit_pct}% saved=~$${v.est_saved_usd}`);
  }
}

function statsView() {
  // "Without cache" bills every input token at the miss rate; that difference
  // is what this proxy is worth. Output is identical either way.
  const actual = stats.hit * PRICE_CACHE + stats.miss * PRICE_MISS + stats.output * PRICE_OUT;
  const baseline = stats.input * PRICE_MISS + stats.output * PRICE_OUT;
  return {
    requests: stats.requests,
    sessions: stats.sessions.size,
    input_tokens: stats.input,
    cache_read_tokens: stats.hit,
    cache_miss_tokens: stats.miss,
    output_tokens: stats.output,
    hit_pct: stats.input ? Math.round((stats.hit / stats.input) * 100) : 0,
    tokens_saved: stats.hit,
    est_cost_usd: round6(actual / 1e6),
    est_cost_without_cache_usd: round6(baseline / 1e6),
    est_saved_usd: round6((baseline - actual) / 1e6),
    prices_per_1m: { cache_read: PRICE_CACHE, cache_miss: PRICE_MISS, output: PRICE_OUT },
  };
}

const MAX_BODY = 64 * 1024 * 1024;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  // Local-only reporting endpoint; never forwarded upstream.
  if (req.method === "GET" && String(req.url).replace(/\?.*$/, "").replace(/\/+$/, "") === "/__stats") {
    res.writeHead(200, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
    res.end(JSON.stringify(statsView(), null, 2));
    return;
  }

  const path = toUpstreamPath(req.url);
  const url = UPSTREAM + path;

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  let body;
  if (hasBody) {
    try {
      body = await readBody(req);
    } catch (err) {
      res.writeHead(413, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { type: "proxy_error", message: String(err.message ?? err) } }));
      return;
    }
  }

  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (SKIP_REQ.has(k)) continue;
    headers[k] = v;
  }

  const conv = analyzeConversation(body);
  const derived = conv.id ? touchSession(conv.id, conv.debug?.msgs) : null;
  const session =
    SESSION_MODE === "passthrough"
      ? null
      : SESSION_MODE === "fixed"
        ? SESSION
        : (derived ?? (hasSession(headers) ? null : SESSION));
  if (session) headers["x-opencode-session"] = session;
  stats.requests += 1;

  if (headers["user-agent"] == null) headers["user-agent"] = "go-session-proxy/1.0";
  shapeAuth(headers, path);

  let upstream;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? body : undefined,
      redirect: "manual",
    });
  } catch (err) {
    res.writeHead(502, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        error: { type: "proxy_error", message: String(err), cause: String(err?.cause ?? "") },
      }),
    );
    return;
  }

  if (LOG) {
    const d = conv.debug;
    const extra = DUMP && d ? ` | msgs=${d.msgs} sysLen=${d.sysLen} first="${d.first}"` : "";
    console.log(`${upstream.status} ${req.method} ${path} ${session ?? headers["x-opencode-session"] ?? "-"}${extra}`);
  }

  const out = {};
  upstream.headers.forEach((v, k) => {
    if (!SKIP_RES.has(k)) out[k] = v;
  });
  res.writeHead(upstream.status, out);

  if (!upstream.body) {
    res.end();
    return;
  }
  // Stream straight through so SSE token streaming is not buffered.
  const downstream = Readable.fromWeb(upstream.body);
  // A mid-stream failure must not take the whole process down.
  downstream.on("error", () => res.destroy());
  res.on("error", () => downstream.destroy());

  if (USAGE) {
    downstream.pipe(usageMeter(session ?? headers["x-opencode-session"])).pipe(res);
  } else {
    downstream.pipe(res);
  }
});

// --check <key>: send one minimal request per API shape and report the real error.
async function check(key) {
  const body = { model: process.env.MODEL ?? "deepseek-v4.1-flash", max_tokens: 1, messages: [{ role: "user", content: "hi" }] };
  const cases = [
    ["OpenAI  /chat/completions + Bearer", UPSTREAM + "/chat/completions", { authorization: `Bearer ${key}` }],
    ["Anthropic /messages      + x-api-key", UPSTREAM + "/messages", { "x-api-key": key, "anthropic-version": "2023-06-01" }],
  ];
  let ok = 0;
  let saw401 = false;
  let saw403 = false;
  for (const [label, url, auth] of cases) {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json", "user-agent": "go-session-proxy/1.0", "x-opencode-session": SESSION, ...auth },
        body: JSON.stringify(body),
      });
      const txt = (await r.text()).replace(/\s+/g, " ").slice(0, 200);
      if (r.status === 200) ok++;
      else if (r.status === 403) saw403 = true;
      else if (/Invalid credential/.test(txt)) saw401 = true;
      console.log(`${r.status === 200 ? "OK  " : "FAIL"} ${label} -> ${r.status} ${r.status === 200 ? "" : txt}`);
    } catch (err) {
      console.log(`FAIL ${label} -> ${err}`);
    }
  }
  if (!ok) {
    console.log("");
    if (saw403) {
      console.log("403 \"This API key can't call models\" = the key authenticated but its PERMISSION SCOPE forbids model calls.");
      console.log("  Fix in Console: Account > Personal API keys > key > Edit... > Permissions");
      console.log("       Restricted  ->  Models: Request      (Models: Read only lists models, it cannot call them)");
      console.log("  A key needs Models: Request to use Inference / Go.");
    } else if (saw401) {
      console.log('"Invalid credential"  = key arrived but was rejected: typo, revoked, expired, or created for a different workspace.');
    } else {
      console.log('"requires an API key" = key never arrived (wrong header for that path).');
    }
    process.exitCode = 1;
    return;
  }
  console.log(`\nkey works. start the proxy, then set the harness base URL to http://127.0.0.1:${PREFERRED_PORT}/v1`);
}

if (process.argv[2] === "--check") {
  const key = process.argv[3] ?? process.env.OPENCODE_API_KEY;
  if (!key) {
    console.error("usage: node go-session-proxy.mjs --check oc_sk_...");
    process.exit(2);
  }
  await check(key);
  // No process.exit() here: a hard exit with undici keep-alive sockets open
  // trips a libuv assertion on Windows. Let the event loop drain instead.
  // The server below is skipped by the `else` branch.
} else {
  // Bind the preferred port, or the next free one when it is taken.
  // PORT=0 hands the choice to the OS (a random free port).
  // PORT_SCAN=0 disables the search and fails loudly instead.
  const tryListen = (port, remaining) => {
    const onError = (err) => {
      server.removeListener("listening", onListening);
      if (err.code === "EADDRINUSE" && remaining > 0 && port !== 0) {
        const next = port + 1;
        console.error(`Port ${port} is busy, trying ${next}...`);
        tryListen(next, remaining - 1);
        return;
      }
      if (err.code === "EADDRINUSE") {
        console.error(`Port ${port} is already in use and no free port was found.`);
        console.error(`  Set PORT to something else, e.g.  $env:PORT=8790; node go-session-proxy.mjs`);
      } else if (err.code === "EACCES") {
        console.error(`Port ${port} is not permitted on this system. Pick another one with PORT.`);
      } else {
        console.error(err);
      }
      process.exit(1);
    };

    const onListening = () => {
      server.removeListener("error", onError);
      const actual = server.address().port;
      console.log(`Go proxy listening on http://127.0.0.1:${actual}/v1`);
      // Machine-readable line: the tray app watches for it to show the real URL.
      console.log(`RUNTIME_PORT=${actual}`);
      console.log(`  upstream : ${UPSTREAM}`);
      console.log(`  session  : ${SESSION_MODE}${SESSION_MODE === "fixed" ? ` (${SESSION})` : ""}`);
      console.log(`  set your harness base URL to: http://127.0.0.1:${actual}/v1`);
      if (actual !== PREFERRED_PORT && PREFERRED_PORT !== 0) {
        console.log(`  note: preferred port ${PREFERRED_PORT} was busy; update your harness to the URL above`);
      }
    };

    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, "127.0.0.1");
  };

  tryListen(PREFERRED_PORT, Number.isFinite(PORT_SCAN) ? PORT_SCAN : 20);
}
