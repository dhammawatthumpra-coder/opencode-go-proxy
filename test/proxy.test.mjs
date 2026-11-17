// node --test test/
//
// Imports go-session-proxy.mjs without starting it: the module only listens
// when executed directly, and STATS_FILE is blanked so no counter file lands
// in the checkout.

import test from "node:test";
import assert from "node:assert/strict";
import { Readable, Writable } from "node:stream";

process.env.STATS_FILE = "";
const {
  toUpstreamPath,
  analyzeConversation,
  touchSession,
  affinity,
  stats,
  statsView,
  recordUsage,
  resetStats,
  usageMeter,
} = await import("../go-session-proxy.mjs");

const u = (text) => ({ role: "user", content: text });
const a = (text) => ({ role: "assistant", content: text });
const body = (messages, system) =>
  Buffer.from(JSON.stringify(system === undefined ? { messages } : { system, messages }));

test("path folding", () => {
  assert.equal(toUpstreamPath("/v1/chat/completions"), "/chat/completions");
  assert.equal(toUpstreamPath("/openai/v1/chat/completions"), "/chat/completions");
  assert.equal(toUpstreamPath("/anthropic/v1/messages"), "/messages");
  assert.equal(toUpstreamPath("/v1/v1/messages"), "/messages");
  assert.equal(toUpstreamPath("/v1"), "/");
  assert.equal(toUpstreamPath("/v1/models?x=1"), "/models?x=1");
});

test("session id is stable across turns and differs per chat", () => {
  const first = analyzeConversation(body([u("same opener")]));
  const later = analyzeConversation(body([u("same opener"), a("hi"), u("next")]));
  const other = analyzeConversation(body([u("different opener")]));
  assert.equal(first.id, later.id);
  assert.notEqual(first.id, other.id);
});

test("the system prompt is part of the id", () => {
  const x = analyzeConversation(body([u("hi")], "sys A"));
  const y = analyzeConversation(body([u("hi")], "sys B"));
  assert.notEqual(x.id, y.id);
});

test("one chat keeps one id while it grows", () => {
  affinity.clear();
  let id;
  for (const n of [1, 2, 3, 4]) {
    const msgs = Array.from({ length: n }, (_, i) => (i % 2 ? a(`a${i}`) : u(i === 0 ? "opener" : `m${i}`)));
    const conv = analyzeConversation(body(msgs));
    id = touchSession(conv.id, conv);
  }
  assert.match(id, /^ses_[0-9a-f]{32}$/);
});

test("a second chat reusing the opener gets its own id", () => {
  affinity.clear();
  const open = analyzeConversation(body([u("opener")]));
  const id1 = touchSession(open.id, open);

  const mid = analyzeConversation(body([u("opener"), a("x"), u("y")]));
  touchSession(mid.id, mid);

  const again = analyzeConversation(body([u("opener")]));
  const id2 = touchSession(again.id, again);
  assert.notEqual(id1, id2);
  assert.equal(id2, `${open.id}_r1`);
});

test("a sliding window does not rotate", () => {
  affinity.clear();
  let id;
  for (let i = 0; i < 5; i++) {
    const conv = analyzeConversation(body([u("opener"), a("x"), u("y"), a("z")]));
    id = touchSession(conv.id, conv);
  }
  assert.match(id, /^ses_[0-9a-f]{32}$/);
});

test("compaction keeps the route when recent turns survive", () => {
  affinity.clear();
  const t1 = analyzeConversation(body([u("opener")]));
  const id = touchSession(t1.id, t1);

  const t2 = analyzeConversation(body([u("opener"), a("hi"), u("second")]));
  assert.equal(touchSession(t2.id, t2), id);

  // The harness replaced the opener with a summary; "second" is still there.
  const compacted = analyzeConversation(body([u("summary of the earlier work"), a("ok"), u("second")]));
  assert.notEqual(compacted.id, t2.id, "the anchor really did change");
  assert.equal(touchSession(compacted.id, compacted), id, "but the route is reused");
});

test("compaction reuse expires", () => {
  affinity.clear();
  const t1 = analyzeConversation(body([u("opener")]));
  const id = touchSession(t1.id, t1, 1_000);

  const t2 = analyzeConversation(body([u("opener"), a("hi"), u("second")]));
  touchSession(t2.id, t2, 2_000);

  const compacted = analyzeConversation(body([u("summary"), a("ok"), u("second")]));
  const late = touchSession(compacted.id, compacted, 2_000 + 31 * 60_000);
  assert.notEqual(late, id, "past the TTL it starts a fresh route");
});

test("totals and the cost maths", () => {
  resetStats();
  recordUsage("s1", { hit: 900, miss: 100, input: 1000, output: 50 });
  recordUsage("s2", { hit: 0, miss: 1000, input: 1000, output: 0 });

  const v = statsView();
  assert.equal(v.input_tokens, 2000);
  assert.equal(v.cache_read_tokens, 900);
  assert.equal(v.cache_miss_tokens, 1100);
  assert.equal(v.hit_pct, 45);
  assert.equal(v.tokens_saved, 900);
  // actual   = 900*0.003 + 1100*0.15 + 50*0.6   = 197.7  -> $0.000198
  // baseline = 2000*0.15 + 50*0.6               = 330    -> $0.000330
  assert.equal(v.est_cost_usd, 0.000198);
  assert.equal(v.est_cost_without_cache_usd, 0.00033);
  assert.equal(v.est_saved_usd, 0.000132);
  assert.match(v.since, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(v.responses_without_usage, 0);
});

test("a response without usage is counted, not silently ignored", async () => {
  resetStats();
  await new Promise((resolve, reject) => {
    Readable.from([Buffer.from('{"choices":[{"message":{"content":"hi"}}]}')])
      .pipe(usageMeter("s1"))
      .pipe(new Writable({ write(_c, _e, cb) { cb(); }, final(cb) { cb(); } }))
      .on("finish", resolve)
      .on("error", reject);
  });
  assert.equal(stats.noUsage, 1);
  assert.equal(statsView().responses_without_usage, 1);
});
