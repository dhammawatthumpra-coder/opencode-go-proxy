// node --test test/integration.test.mjs
//
// Runs the real proxy against a mock upstream and checks the things the unit
// tests cannot see: the auth header a path gets, the path that reaches the
// gateway, the session header, streaming, and the stats endpoints.

import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import zlib from "node:zlib";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(new URL(".", import.meta.url)));
const KEY = "oc_sk_test_key";

const seen = [];
let upstream;
let child;
let port;

const usage = { prompt_tokens: 1000, prompt_cache_hit_tokens: 900, completion_tokens: 50 };

function body(messages, extra = {}) {
  return JSON.stringify({ model: "m", messages, ...extra });
}

async function listen(handler) {
  const srv = http.createServer(handler);
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return srv;
}

// A second proxy for tests that need a different upstream.
async function startProxy(upstreamUrl) {
  const child = spawn(process.execPath, [path.join(ROOT, "go-session-proxy.mjs")], {
    env: {
      ...process.env,
      PORT: "0",
      PORT_SCAN: "0",
      UPSTREAM: upstreamUrl,
      STATS_FILE: "",
      USAGE: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const port = await new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => reject(new Error("proxy did not report a port")), 8000);
    child.stdout.on("data", (d) => {
      buf += d.toString();
      const m = /RUNTIME_PORT=(\d+)/.exec(buf);
      if (m) {
        clearTimeout(timer);
        resolve(Number(m[1]));
      }
    });
  });
  return { port, child };
}

before(async () => {
  upstream = await listen((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      let json = {};
      try {
        json = JSON.parse(raw || "{}");
      } catch {}
      seen.push({ url: req.url, method: req.method, headers: req.headers, json });

      if (json.stream) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.write('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n');
        res.write(`data: {"usage":${JSON.stringify(usage)}}\n\n`);
        res.end("data: [DONE]\n\n");
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "ok" } }], usage }));
    });
  });

  const started = await startProxy(`http://127.0.0.1:${upstream.address().port}`);
  child = started.child;
  port = started.port;
});

after(() => {
  if (child) child.kill();
  if (upstream) upstream.close();
});

const url = (p) => `http://127.0.0.1:${port}${p}`;

test("an OpenAI path gets a Bearer token, whatever the client sent", async () => {
  seen.length = 0;
  const r = await fetch(url("/v1/chat/completions"), {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": KEY },
    body: body([{ role: "user", content: "hello" }]),
  });
  assert.equal(r.status, 200);
  await r.json(); // drain so the usage meter has flushed
  await new Promise((r) => setTimeout(r, 100));

  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "/chat/completions", "the /v1 prefix is folded away");
  assert.equal(seen[0].headers.authorization, `Bearer ${KEY}`);
  assert.equal(seen[0].headers["x-api-key"], undefined, "the Claude header is dropped");
  assert.match(seen[0].headers["x-opencode-session"], /^ses_[0-9a-f]{32}$/);
});

test("an Anthropic path gets x-api-key instead", async () => {
  seen.length = 0;
  const r = await fetch(url("/anthropic/v1/messages"), {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
    body: body([{ role: "user", content: "hello" }]),
  });
  assert.equal(r.status, 200);
  await r.text();

  assert.equal(seen[0].url, "/messages");
  assert.equal(seen[0].headers["x-api-key"], KEY);
  assert.equal(seen[0].headers.authorization, undefined);
  assert.equal(seen[0].headers["anthropic-version"], "2023-06-01");
});

test("the Responses API is proxied like the other OpenAI shapes", async () => {
  seen.length = 0;
  const r = await fetch(url("/v1/responses"), {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": KEY },
    body: JSON.stringify({ model: "m", input: [{ role: "user", content: "hi" }] }),
  });
  assert.equal(r.status, 200);
  await r.text();

  assert.equal(seen[0].url, "/responses");
  assert.equal(seen[0].headers.authorization, `Bearer ${KEY}`);
  assert.equal(seen[0].headers["x-api-key"], undefined);
});

test("a doubled /v1 is collapsed", async () => {
  seen.length = 0;
  const r = await fetch(url("/v1/v1/chat/completions"), {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
    body: body([{ role: "user", content: "hello" }]),
  });
  await r.text();
  assert.equal(seen[0].url, "/chat/completions");
});

test("streaming passes through untouched and still gets metered", async () => {
  const before = (await (await fetch(url("/__stats"))).json()).requests;
  const r = await fetch(url("/v1/chat/completions"), {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
    body: body([{ role: "user", content: "stream please" }], { stream: true }),
  });
  assert.match(r.headers.get("content-type"), /event-stream/);
  const text = await r.text();
  assert.match(text, /data: /);
  assert.match(text, /\[DONE\]/);

  await new Promise((r) => setTimeout(r, 150));
  const afterStats = await (await fetch(url("/__stats"))).json();
  assert.ok(afterStats.requests > before, "the streamed turn was counted");
  assert.ok(afterStats.cache_read_tokens >= 900, "usage from the last chunk was parsed");
});

test("a browser page is served at /ui", async () => {
  const r = await fetch(url("/ui"));
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type"), /text\/html/);
  const html = await r.text();
  assert.match(html, /OpenCode Go Proxy/);
  assert.match(html, /fetch\("\/__stats"\)/, "the page reads the live endpoints");
  assert.match(html, /__stats\/history/, "and the history endpoint");
});

test("/__stats and /__stats/history describe the run", async () => {
  const s = await (await fetch(url("/__stats"))).json();
  assert.equal(typeof s.requests, "number");
  assert.ok(s.prices_per_1m.cache_read > 0);
  assert.match(s.since, /^\d{4}-\d{2}-\d{2}T/);

  const h = await (await fetch(url("/__stats/history"))).json();
  assert.ok(Array.isArray(h.days));
  assert.ok(h.days.length >= 1, "today has a bucket");
  assert.ok(h.windows.today.requests >= 1);
});

test("reset clears the counters but not the ledger", async () => {
  const before = await (await fetch(url("/__stats/history"))).json();
  const r = await fetch(url("/__stats/reset"), { method: "POST" });
  const cleared = await r.json();
  assert.equal(cleared.requests, 0);
  assert.equal(cleared.cache_read_tokens, 0);

  const after = await (await fetch(url("/__stats/history"))).json();
  assert.deepEqual(after.days, before.days, "history survives the reset");
});

test("a response with no usage is counted, not ignored", async () => {
  const bare = await listen((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end('{"choices":[{"message":{"content":"no usage here"}}]}');
    });
  });
  const { port: p2, child: child2 } = await startProxy(`http://127.0.0.1:${bare.address().port}`);

  await fetch(`http://127.0.0.1:${p2}/v1/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
    body: body([{ role: "user", content: "no usage" }]),
  }).then((r) => r.text());
  await new Promise((r) => setTimeout(r, 150));

  const s = await (await fetch(`http://127.0.0.1:${p2}/__stats`)).json();
  assert.equal(s.responses_without_usage, 1);

  child2.kill();
  bare.close();
});

test("a gzip response is decoded before the caller sees it", async () => {
  // Chromium harnesses ask for gzip on every request. undici decodes the body
  // for us, so forwarding the upstream's content-encoding header made the
  // caller decompress plain text and report "Invalid response from provider".
  const payload = JSON.stringify({
    choices: [{ message: { role: "assistant", content: "gz" } }],
    usage,
  });
  const gz = zlib.gzipSync(payload);
  const gzUp = await listen((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json", "content-encoding": "gzip" });
      res.end(gz);
    });
  });
  const { port: p3, child: child3 } = await startProxy(`http://127.0.0.1:${gzUp.address().port}`);

  const r = await fetch(`http://127.0.0.1:${p3}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "accept-encoding": "gzip, deflate, br",
      authorization: `Bearer ${KEY}`,
    },
    body: body([{ role: "user", content: "gz" }]),
  });

  assert.equal(r.headers.get("content-encoding"), null, "the header is gone");
  const json = await r.json(); // throws if the body were still gzipped
  assert.equal(json.choices[0].message.content, "gz");

  child3.kill();
  gzUp.close();
});
