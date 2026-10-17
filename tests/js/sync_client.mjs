// sync.js's poll without the page (createSyncClient): 5 s while visible from a random first moment,
// nothing while hidden, at once when shown; 304 keeps what the page has; failures wait 5, 10, 20,
// 40, 60 s and never less than Retry-After; 10 s at most per request; one at a time and one
// remembered wish; another tab's hint within 250 ms but never through a failure's wait; a
// sign-out forgets every area and ignores a late answer. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { fakeTimers } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/sync.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, Boolean, Date });
const { createSyncClient } = window.TowSync;
const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
const REV = (n) => String(n).repeat(32).slice(0, 32);

// A harness: a fake clock, a request that answers what the test queues (or a fixed answer).
const harness = ({ random = () => 0.5, answer = null } = {}) => {
  const clock = fakeTimers();
  const sent = [];
  const queue = [];
  let hiddenNow = false;
  let wallShift = 0; // the wall clock set back or forward; the steady one never moves back
  const ended = [];
  const client = createSyncClient({
    now: () => clock.now() + wallShift,
    monotonic: clock.now,
    setTimer: clock.setTimeout,
    clearTimer: clock.clearTimeout,
    random,
    hidden: () => hiddenNow,
    request: (req) => {
      sent.push({ ...req, at: clock.now() });
      if (queue.length) return queue.shift()(req);
      return Promise.resolve(answer ? answer(req) : { status: 200, body: { ok: true, now_ts: clock.now() / 1000, rev: {}, areas: {} } });
    },
    signedOut: (status) => ended.push(status),
  });
  const run = async (ms) => {
    const end = clock.now() + ms;
    for (;;) {
      const before = sent.length;
      clock.advance(Math.min(100, end - clock.now()));
      await flush();
      if (clock.now() >= end && sent.length === before) break;
    }
  };
  return {
    clock, sent, queue, client, run, ended,
    hide: (on) => { hiddenNow = on; },
    setClock: (ms) => { wallShift += ms; },
  };
};
const verdict = {};

// The first request at a random moment of the first 5 s, then every 5 s while visible.
{
  const test = harness({ random: () => 0.6 });
  const start = test.clock.now();
  test.client.start();
  await test.run(16000);
  assert.deepEqual(test.sent.map((r) => r.at - start), [3000, 8000, 13000]);
}
verdict.randomPhaseThenEveryFive = true;

// Hidden: nothing asked (the one already sent may finish); shown again: one request at once.
{
  const test = harness();
  test.client.start();
  await test.run(3000);
  const before = test.sent.length;
  test.hide(true);
  test.client.hidden();
  await test.run(60000);
  assert.equal(test.sent.length, before);
  test.hide(false);
  const shown = test.clock.now();
  test.client.nudge("show");
  test.client.nudge("show"); // twice in one moment: one request
  await test.run(10);
  assert.equal(test.sent.length, before + 1);
  assert.equal(test.sent.at(-1).at, shown);
}
verdict.hiddenAsksNothingShownAsksOnce = true;

// 200: the areas reach their readers and ``have`` names what was applied; a reader that applies
// later (manual) is named only after its ack. The ETag goes only with the same request; 304
// keeps every area, reaches no reader, and counts as a success (5 s later, not a backoff).
{
  const test = harness();
  const seen = [];
  test.client.on("header", (payload) => seen.push(["header", payload.rev]));
  test.client.on("topics", (payload) => seen.push(["topics", payload.rev]), { manual: true });
  const polls = [];
  test.client.onPoll((_, ok) => polls.push(ok));
  test.queue.push(async () => ({ status: 200, etag: 'W/"a"', body: { ok: true, now_ts: 100, tz_now: 180, rev: {}, areas: { header: { rev: REV(1), sites: {} }, topics: { rev: REV(2), full: true, order: [], rows: {} } } } }));
  test.queue.push(async () => ({ status: 200, etag: 'W/"b"', body: { ok: true, now_ts: 105, rev: {}, areas: {} } }));
  test.queue.push(async () => ({ status: 304, date: test.clock.now() + 60000 }));
  test.client.start();
  await test.run(2600);
  assert.deepEqual(seen, [["header", REV(1)], ["topics", REV(2)]]);
  assert.equal(test.client.applied("header"), REV(1));
  assert.equal(test.client.applied("topics"), "");
  assert.equal(test.client.tzNow(), 180);
  test.client.ack("topics", REV(2));
  await test.run(5000);
  assert.ok(test.sent[1].url.includes(`have=header%3A${REV(1)}%2Ctopics%3A${REV(2)}`), test.sent[1].url);
  assert.equal(test.sent[1].etag, ""); // another request than the one the ETag named
  await test.run(5000);
  assert.equal(test.sent[2].etag, 'W/"b"');
  assert.equal(test.sent[2].at - test.sent[1].at, 5000);
  assert.equal(test.client.area("header").rev, REV(1)); // kept through the 304
  assert.equal(Math.round(test.client.serverNow() - test.clock.now()), 60000); // the 304's Date sets the clock
  await test.run(5000);
  assert.equal(test.sent[3].at - test.sent[2].at, 5000);
  assert.deepEqual(polls, [true, true, true, true]);
  assert.equal(seen.length, 2);
}
verdict.deltaHaveEtagAndNotModified = true;

// A reader that fails leaves its area unapplied: the next answer brings it again.
{
  const test = harness();
  let calls = 0;
  test.client.on("undo", () => { calls += 1; if (calls === 1) throw new Error("synthetic"); });
  test.queue.push(async () => ({ status: 200, body: { ok: true, now_ts: 1, rev: {}, areas: { undo: { rev: REV(3), undo: null } } } }));
  test.client.start();
  await test.run(2600);
  assert.equal(test.client.applied("undo"), "");
  assert.equal(test.client.observed("undo"), REV(3));
  assert.ok(!test.sent.at(-1).url.includes("undo"));
}
verdict.failedReaderRetried = true;

// An area its reader could not apply is kept and offered to it again after later successful polls -
// also when every later answer is 304 Not Modified (the same request: the server sends it no more) -
// 5, 10, 20, 40, then every 60 s, never more often. A reader that answers with a promise applies
// the area only once that promise says so (anything but false).
{
  const test = harness({ answer: () => ({ status: 304 }) });
  const calls = [];
  let throwsLeft = 3;
  test.client.on("undo", () => {
    calls.push(test.clock.now());
    if (throwsLeft > 0) {
      throwsLeft -= 1;
      throw new Error("synthetic");
    }
  });
  const asked = [];
  let accepted = false;
  test.client.on("messages", () => { asked.push(test.clock.now()); return Promise.resolve(accepted); });
  test.queue.push(async () => ({ status: 200, etag: 'W/"a"', body: { ok: true, now_ts: 1, rev: {}, areas: { undo: { rev: REV(3), undo: null }, messages: { rev: REV(6) } } } }));
  test.client.start();
  await test.run(2600);
  const first = calls[0];
  assert.equal(test.client.applied("undo"), "");
  assert.equal(test.client.applied("messages"), "");
  await test.run(5000 + 10000 + 20000);
  assert.deepEqual(calls.map((at) => at - first), [0, 5000, 15000, 35000]);
  assert.equal(test.client.applied("undo"), REV(3));
  assert.ok(test.sent.slice(1).every((request) => request.etag === 'W/"a"')); // every later answer: 304
  assert.deepEqual(asked.slice(0, 3).map((at) => at - first), [0, 5000, 15000]);
  accepted = true;
  await test.run(40000); // its fourth try (35 s) failed too: the fifth is 40 s later
  assert.equal(test.client.applied("messages"), REV(6));
  const offered = asked.length;
  await test.run(120000);
  assert.equal(calls.length, 4); // applied: never offered again
  assert.equal(asked.length, offered);
  assert.ok(test.sent.at(-1).url.includes(`messages%3A${REV(6)}`), test.sent.at(-1).url);
  // Always failing: offered at most once a minute.
  const always = harness({ answer: () => ({ status: 304 }) });
  const tries = [];
  always.client.on("undo", () => { tries.push(always.clock.now()); throw new Error("synthetic"); });
  always.queue.push(async () => ({ status: 200, body: { ok: true, now_ts: 1, rev: {}, areas: { undo: { rev: REV(7), undo: null } } } }));
  always.client.start();
  await always.run(2600 + 600000);
  const gaps = tries.map((at, i) => (i ? (at - tries[i - 1]) / 1000 : 0)).slice(1);
  assert.deepEqual(gaps.slice(0, 5), [5, 10, 20, 40, 60]);
  assert.ok(gaps.every((gap) => gap <= 60 && gap >= 5), String(gaps));
}
verdict.unappliedOfferedAgainThrough304 = true;

// A reader whose promise never settles (a request that hangs under it) holds its area 30 s at most:
// then it counts as failed and is offered again after later polls; a late answer of the lost
// promise changes nothing.
{
  const test = harness({ answer: () => ({ status: 304 }) });
  const asked = [];
  const answers = [];
  test.client.on("messages", () => {
    asked.push(test.clock.now());
    return new Promise((resolve) => { answers.push(resolve); });
  });
  test.queue.push(async () => ({ status: 200, etag: 'W/"a"', body: { ok: true, now_ts: 1, rev: {}, areas: { messages: { rev: REV(8) } } } }));
  test.client.start();
  await test.run(2600);
  assert.equal(asked.length, 1);
  await test.run(29000);
  assert.equal(asked.length, 1); // still waiting for its reader
  await test.run(1000 + 5000 + 5000);
  assert.equal(asked.length, 2);
  assert.ok(asked[1] - asked[0] >= 35000, String(asked[1] - asked[0]));
  answers[0](true); // the lost one answers late: not its revision's answer any more
  await flush();
  assert.equal(test.client.applied("messages"), "");
  answers[1](true);
  await flush();
  assert.equal(test.client.applied("messages"), REV(8));
}
verdict.hungReaderGivenUp = true;

// Failures wait 5, 10, 20, 40, 60, 60 s (jitter at 0.5 = none); one success goes back to 5 s.
{
  let failing = true;
  const test = harness({ answer: () => (failing ? { status: 500 } : { status: 200, body: { ok: true, now_ts: 1, areas: {} } }) });
  test.client.start();
  await test.run(2600 + 5000 + 10000 + 20000 + 40000 + 60000 + 60000 + 100);
  const gaps = test.sent.map((r, i, all) => (i ? (r.at - all[i - 1].at) / 1000 : 0)).slice(1);
  assert.deepEqual(gaps, [5, 10, 20, 40, 60, 60]);
  failing = false;
  await test.run(70000);
  const after = test.sent.map((r, i, all) => (i ? (r.at - all[i - 1].at) / 1000 : 0)).slice(7);
  assert.deepEqual(after.slice(0, 3), [60, 5, 5]);
}
verdict.backoffFiveToSixty = true;

// The page's readers learn how each poll ended: its HTTP status, 0 when there was no answer at all
// (TOW unreachable: updates.js then says TOW is restarting while a job runs).
{
  const test = harness();
  const told = [];
  test.client.onPoll((_, ok, status) => told.push([ok, status]));
  test.queue.push(async () => { throw new TypeError("Failed to fetch"); });
  test.queue.push(async () => ({ status: 503, retryAfter: 2 }));
  test.queue.push(async () => ({ status: 304 }));
  test.queue.push(async () => ({ status: 200, body: { ok: true, now_ts: 1, areas: {} } }));
  test.client.start();
  await test.run(2600 + 5000 + 10000 + 5000 + 100);
  assert.deepEqual(told, [[false, 0], [false, 503], [true, 304], [true, 200]]);
}
verdict.pollersLearnTheStatus = true;

// Jitter stays within ±20 % of each step.
{
  const test = harness({ random: () => 0.999, answer: () => ({ status: 503 }) });
  test.client.start();
  await test.run(5000 + 6000 + 12000 + 100);
  const gaps = test.sent.map((r, i, all) => (i ? r.at - all[i - 1].at : 0)).slice(1);
  // (+100 ms: the harness answers at the end of its 100 ms step)
  assert.ok(gaps[0] > 5000 && gaps[0] <= 6100, String(gaps));
  assert.ok(gaps[1] > 10000 && gaps[1] <= 12100, String(gaps));
}
verdict.jitterBounded = true;

// 429 with Retry-After 30: nothing before 30 s, not even when the tab is shown again.
{
  const test = harness();
  test.queue.push(async () => ({ status: 429, retryAfter: 30 }));
  test.client.start();
  await test.run(2600);
  const failedAt = test.sent[0].at;
  test.client.nudge("show");
  test.client.nudge("hint");
  await test.run(29000);
  assert.equal(test.sent.length, 1);
  await test.run(1500);
  assert.equal(test.sent.length, 2);
  assert.ok(test.sent[1].at - failedAt >= 30000);
}
verdict.retryAfterHonoured = true;

// 429 with Retry-After 12, then the tab hidden and shown again inside the window: the hide
// cleared the timer, and the show used to return without planning anything - the page never
// asked again (1.33.1 QA-02). It asks at the window's end, then every 5 s; also when the wall
// clock was set back an hour meanwhile (the waits run on the steady clock).
for (const shift of [0, -3600000, 3600000]) {
  const test = harness();
  test.queue.push(async () => ({ status: 429, retryAfter: 12 }));
  test.client.start();
  await test.run(2600);
  const failedAt = test.sent[0].at;
  test.setClock(shift);
  test.hide(true);
  test.client.hidden();
  await test.run(2000);
  test.hide(false);
  test.client.nudge("show");
  await test.run(9000);
  assert.equal(test.sent.length, 1, `shift ${shift}: nothing inside the window`);
  await test.run(1000);
  assert.equal(test.sent.length, 2, `shift ${shift}: asked at the window's end`);
  assert.ok(test.sent[1].at - failedAt >= 12000 && test.sent[1].at - failedAt <= 12100, String(test.sent[1].at - failedAt));
  await test.run(10100);
  assert.equal(test.sent.length, 4, `shift ${shift}: then every 5 s`);
}
// A hint inside the window, with no timer planned (cleared by a hide), plans the window's end.
{
  const test = harness();
  test.queue.push(async () => ({ status: 429, retryAfter: 12 }));
  test.client.start();
  await test.run(2600);
  const failedAt = test.sent[0].at;
  test.client.hidden();
  test.client.nudge("hint");
  await test.run(12000);
  assert.equal(test.sent.length, 2);
  assert.ok(test.sent[1].at - failedAt >= 12000, String(test.sent[1].at - failedAt));
}
// A wall clock set back an hour during a failure's backoff does not stretch the wait.
{
  const test = harness();
  test.queue.push(async () => ({ status: 500 }));
  test.client.start();
  await test.run(2600);
  test.setClock(-3600000);
  await test.run(6200);
  assert.equal(test.sent.length, 2);
}
verdict.retryAfterSurvivesHideShowAndClockJumps = true;

// A request that does not answer in 10 s is aborted and counts as a failure (5 s later again).
{
  const test = harness();
  let aborted = 0;
  test.queue.push((req) => { req.onAbort(() => { aborted += 1; }); return new Promise(() => {}); });
  test.client.start();
  await test.run(2600 + 10000);
  assert.equal(aborted, 1);
  assert.equal(test.client.backingOff(), true);
  await test.run(5000);
  assert.equal(test.sent.length, 2);
  assert.equal(test.sent[1].at - test.sent[0].at, 15000);
}
verdict.tenSecondTimeout = true;

// One request at a time; wishes meanwhile are one more request right after, not several.
{
  const test = harness();
  let release;
  test.queue.push(() => new Promise((resolve) => { release = () => resolve({ status: 200, body: { ok: true, now_ts: 1, areas: {} } }); }));
  test.client.start();
  await test.run(2600);
  test.client.nudge("show");
  test.client.nudge("show");
  test.client.nudge("hint");
  await test.run(1000);
  assert.equal(test.sent.length, 1);
  release();
  await flush();
  await test.run(10);
  assert.equal(test.sent.length, 2);
  await test.run(4900);
  assert.equal(test.sent.length, 2);
  await test.run(200);
  assert.equal(test.sent.length, 3);
}
verdict.oneInFlightOnePending = true;

// Another tab's hints within 250 ms are one request; during a failure's wait none at all.
{
  const test = harness();
  test.client.start();
  await test.run(2600);
  const before = test.sent.length;
  test.client.nudge("hint");
  await test.run(100);
  test.client.nudge("hint");
  test.client.nudge("hint");
  await test.run(149);
  assert.equal(test.sent.length, before);
  await test.run(2);
  assert.equal(test.sent.length, before + 1);
  // a failure: hints wait with it
  test.queue.push(async () => ({ status: 500 }));
  test.client.nudge("show");
  await test.run(10);
  const failed = test.sent.length;
  for (let i = 0; i < 5; i += 1) {
    test.client.nudge("hint");
    await test.run(300);
  }
  assert.equal(test.sent.length, failed);
}
verdict.hintsCoalescedNeverThroughBackoff = true;

// Signed out (401): told once, every area forgotten, nothing asked any more.
{
  const test = harness();
  test.queue.push(async () => ({ status: 200, body: { ok: true, now_ts: 1, areas: { undo: { rev: REV(4), undo: null } } } }));
  test.queue.push(async () => ({ status: 401 }));
  test.client.start();
  await test.run(2600 + 5000);
  assert.deepEqual(test.ended, [401]);
  assert.equal(test.client.area("undo"), null);
  assert.equal(test.client.applied("undo"), "");
  const count = test.sent.length;
  test.client.nudge("show");
  await test.run(60000);
  assert.equal(test.sent.length, count);
}
verdict.unauthorizedEnds = true;

// Signing out while an answer is on its way: the answer reaches nobody.
{
  const test = harness();
  const seen = [];
  test.client.on("undo", (payload) => seen.push(payload.rev));
  let release;
  let aborted = 0;
  test.queue.push((req) => {
    req.onAbort(() => { aborted += 1; });
    return new Promise((resolve) => { release = () => resolve({ status: 200, body: { ok: true, now_ts: 1, areas: { undo: { rev: REV(5), undo: null } } } }); });
  });
  test.client.start();
  await test.run(2600);
  test.client.signOut(0);
  assert.equal(aborted, 1); // the request on its way is ended with the session
  release();
  await flush();
  assert.deepEqual(seen, []);
  assert.equal(test.client.area("undo"), null);
  assert.deepEqual(test.ended, [0]);
}
verdict.lateAnswerRejected = true;

console.log(JSON.stringify(verdict));
