// The scheduler of checks.js with a fake clock and a fake /check/status: polling bounds, rapid
// clicks, a lost acknowledgement found by its action id, a reload, shared jobs, expiry, blocked
// storage, a hidden tab, signing out - and exactly one result per check. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/checks.js", import.meta.url), "utf8");
const window = {};
// messages.js first, as on a page: the scheduler scrubs what it stores with its scrubbing.
const messages = readFileSync(new URL("../../src/tow/static/messages.js", import.meta.url), "utf8");
const context = { window, crypto: globalThis.crypto, Math, JSON, Array, Uint8Array, Number, String, URL, document: { getElementById: () => null } };
vm.runInNewContext(messages, context);
vm.runInNewContext(source, context);
const createScheduler = window.TowCheckScheduler;

const flush = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve(); };
const harness = ({ answer, storage = null, hiddenNow = () => false } = {}) => {
  let now = 1_000_000;
  let next = 1;
  const timers = new Map();
  const requests = [];
  const progress = [];
  const terminal = [];
  const repeats = [];
  const signedOut = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const scheduler = createScheduler({
    now: () => now,
    setTimer: (fn, ms) => { timers.set(next, { at: now + ms, fn }); return next++; },
    clearTimer: (id) => timers.delete(id),
    storage,
    key: "tow.checks.test",
    hidden: hiddenNow,
    request: async (entry) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      requests.push({ at: now, action: entry.action, job: entry.job });
      await Promise.resolve();
      inFlight -= 1;
      return answer(entry, requests.length);
    },
    progress: (entry, body) => progress.push([entry.action || entry.job, body.status]),
    signedOut: (entries) => signedOut.push(entries.length),
    // Said results only: a repeat (a job already answered) is recorded apart.
    terminal: (entry, body, same, repeat) => (repeat ? repeats : terminal).push({ key: entry.job || entry.action, body, same: same.length }),
  });
  return {
    scheduler, requests, progress, terminal, repeats, signedOut, maxInFlight: () => maxInFlight, now: () => now,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
        await flush();
      }
      now = end;
    },
  };
};
const ok = (body) => ({ status: 200, body });
const memory = () => {
  const data = new Map();
  return { data, get: (key) => data.get(key) ?? null, set: (key, value) => data.set(key, value), remove: (key) => data.delete(key), keys: () => [...data.keys()] };
};
const verdict = {};

// Rapid clicks: every click is followed, each check asked no faster than every 5 s, at most two
// requests a second, one at a time, in turn - and each ends in exactly one result.
{
  const polls = new Map();
  const test = harness({
    answer: (entry) => {
      const count = (polls.get(entry.action) || 0) + 1;
      polls.set(entry.action, count);
      return ok({ status: count < 3 ? "running" : "done", job: `job-${entry.action}`, message: { id: `check-job-${entry.action}` } });
    },
  });
  const actions = Array.from({ length: 5 }, () => test.scheduler.begin({ subject: "Show" }));
  assert.ok(actions.every(Boolean));
  assert.equal(new Set(actions).size, 5);
  await test.advance(4999);
  assert.equal(test.requests.length, 0); // nothing sooner than 5 s
  await test.advance(120000);
  assert.equal(test.terminal.length, 5);
  assert.deepEqual(test.terminal.map((item) => item.same), [1, 1, 1, 1, 1]);
  assert.equal(test.requests.length, 15);
  assert.equal(test.maxInFlight(), 1);
  const at = test.requests.map((request) => request.at);
  assert.ok(at.every((value, index) => index === 0 || value - at[index - 1] >= 500)); // <= 2 a second
  for (const action of actions) {
    const own = test.requests.filter((request) => request.action === action).map((request) => request.at);
    assert.ok(own.every((value, index) => index === 0 || value - own[index - 1] >= 5000));
  }
  assert.deepEqual(test.requests.slice(0, 5).map((request) => request.action), actions); // in turn
  assert.equal(test.scheduler.count(), 0);
  await test.advance(60000);
  assert.equal(test.requests.length, 15); // nothing more once each has its result
}
verdict.rapidClicksBounded = true;

// The acknowledgement was lost (the page navigated, the connection dropped): the check is found
// by the action id kept before sending; until TOW knows it, "unknown" is not final for a while.
{
  let known = false;
  const test = harness({ answer: (entry) => ok(known ? { status: "done", job: "J1" } : { status: "unknown" }) });
  test.scheduler.begin({});
  await test.advance(5000);
  assert.equal(test.terminal.length, 0); // the request may still be on its way
  known = true;
  await test.advance(5000);
  assert.equal(test.terminal.length, 1);
  assert.equal(test.terminal[0].key, "J1");
  assert.ok(test.requests[0].action);
}
verdict.lostAcknowledgementFound = true;

// A check TOW no longer knows: once, as unknown (its message says the result is unconfirmed).
{
  const test = harness({ answer: () => ok({ status: "unknown" }) });
  const action = test.scheduler.begin({});
  test.scheduler.acknowledge(action, { job: "J9" });
  await test.advance(5000);
  assert.equal(test.terminal.length, 1);
  assert.equal(test.terminal[0].body.status, "unknown");
  const never = harness({ answer: () => ok({ status: "unknown" }) });
  never.scheduler.begin({}); // never registered
  await never.advance(14000);
  assert.equal(never.terminal.length, 0);
  await never.advance(10000);
  assert.equal(never.terminal.length, 1);
  await never.advance(60000);
  assert.equal(never.terminal.length, 1);
}
verdict.expiredOnce = true;

// Two clicks that share a waiting check: one result for both.
{
  const test = harness({ answer: () => ok({ status: "done", job: "SHARED" }) });
  const one = test.scheduler.begin({});
  const two = test.scheduler.begin({});
  test.scheduler.acknowledge(one, { job: "SHARED" });
  test.scheduler.acknowledge(two, { job: "SHARED" });
  await test.advance(30000);
  assert.equal(test.terminal.length, 1);
  assert.equal(test.terminal[0].same, 2);
  assert.equal(test.requests.length, 1);
}
verdict.sharedJobOneResult = true;

// A reload (another page of the tab): the checks are followed on from the tab's storage.
{
  const storage = memory();
  const first = harness({ storage, answer: () => ok({ status: "running", job: "R1" }) });
  first.scheduler.acknowledge(first.scheduler.begin({ subject: "Show" }), { job: "R1" });
  const second = harness({ storage, answer: () => ok({ status: "done", job: "R1" }) });
  assert.equal(second.scheduler.count(), 1);
  second.scheduler.wake();
  await second.advance(10000);
  assert.equal(second.terminal.length, 1);
  assert.equal(JSON.parse(storage.data.get("tow.checks.test")).entries.length, 0);
}
verdict.reloadResumes = true;

// Storage that throws: followed in memory all the same.
{
  const blocked = { get() { throw new Error("SecurityError"); }, set() { throw new Error("SecurityError"); } };
  const test = harness({ storage: blocked, answer: () => ok({ status: "done", job: "B1" }) });
  assert.ok(test.scheduler.begin({}));
  assert.equal(test.scheduler.stored(), false);
  await test.advance(6000);
  assert.equal(test.terminal.length, 1);
}
verdict.storageBlocked = true;

// TOW does not answer: slower and slower, up to a minute; the check stays followed.
{
  let failing = true;
  const test = harness({ answer: () => (failing ? null : ok({ status: "done", job: "T1" })) });
  test.scheduler.begin({});
  await test.advance(5000 + 10000 + 20000 + 40000 + 60000 + 60000);
  const gaps = test.requests.map((request, index) => (index ? request.at - test.requests[index - 1].at : 0)).slice(1);
  assert.deepEqual(gaps, [10000, 20000, 40000, 60000, 60000]);
  assert.equal(test.terminal.length, 0);
  failing = false;
  await test.advance(60000);
  assert.equal(test.terminal.length, 1);
}
verdict.backoff = true;

// A hidden tab asks nothing; shown again, it goes on.
{
  let hidden = true;
  const test = harness({ hiddenNow: () => hidden, answer: () => ok({ status: "done", job: "H1" }) });
  test.scheduler.begin({});
  await test.advance(60000);
  assert.equal(test.requests.length, 0);
  hidden = false;
  test.scheduler.wake();
  await test.advance(1);
  assert.equal(test.requests.length, 1);
  assert.equal(test.terminal.length, 1);
}
verdict.hiddenPauses = true;

// Signed out: nothing more is asked, the tab's followed checks go with the session (no message
// naming their topics); the page is told once.
{
  const storage = memory();
  const test = harness({ storage, answer: () => ({ status: 401, body: { error: "sign in again" } }) });
  test.scheduler.begin({ subject: "Show" });
  test.scheduler.begin({ subject: "Other" });
  await test.advance(6000);
  assert.equal(test.terminal.length, 0);
  assert.equal(JSON.stringify(test.signedOut), "[2]");
  assert.equal(storage.data.size, 0);
  assert.equal(test.scheduler.count(), 0);
  assert.equal(test.scheduler.begin({}), null);
  await test.advance(60000);
  assert.equal(test.requests.length, 1);
}
verdict.signedOutStops = true;

// Another install's or sign-in's followed checks in this tab go; a stored title keeps only plain
// words (no link with its address, no path).
{
  const storage = memory();
  storage.set("tow.checks.old-session", "{}");
  storage.set("tow.messages.old-session", "{}");
  const test = harness({ storage, answer: () => ok({ status: "running" }) });
  test.scheduler.begin({ subject: "Show https://user:secret@tracker.example/t?id=1 D:\\TV\\Show" });
  assert.equal(storage.get("tow.checks.old-session"), null);
  assert.equal(storage.get("tow.messages.old-session"), "{}"); // the messages' own store keeps its keys
  const stored = storage.get("tow.checks.test");
  assert.ok(!stored.includes("secret"));
  assert.ok(!stored.includes("TV"));
  test.scheduler.stop(); // signing out by hand: the same end
  assert.equal(storage.get("tow.checks.test"), null);
  assert.equal(test.scheduler.count(), 0);
}
verdict.storeScopedAndScrubbed = true;

// No more than TOW keeps (64): the next one is not taken (the page then opens as before), and
// none followed is dropped.
{
  const test = harness({ answer: () => ok({ status: "running" }) });
  for (let n = 0; n < 64; n += 1) assert.ok(test.scheduler.begin({}));
  assert.equal(test.scheduler.begin({}), null);
  assert.equal(test.scheduler.count(), 64);
}
verdict.bounded = true;

// Progress while it waits or runs, then one result; a forgotten check (answered with a page) is
// never asked again.
{
  let calls = 0;
  const test = harness({ answer: () => { calls += 1; return ok({ status: calls < 3 ? "queued" : "done", job: "P1" }); } });
  test.scheduler.begin({});
  const gone = test.scheduler.begin({});
  test.scheduler.forget(gone);
  await test.advance(30000);
  assert.equal(JSON.stringify(test.progress), JSON.stringify([["P1", "queued"], ["P1", "queued"]].map(([, s]) => [test.requests[0].action, s])));
  assert.equal(test.terminal.length, 1);
  assert.ok(test.requests.every((request) => request.action !== gone));
}
verdict.progressThenOneResult = true;

// Two clicks TOW took as one waiting job: followed as one (one deadline, never two requests for
// the job 500 ms apart), one result for both.
{
  const test = harness({ answer: (entry) => ok({ status: test.requests.length < 3 ? "queued" : "done", job: "SAME" }) });
  const one = test.scheduler.begin({});
  const two = test.scheduler.begin({});
  test.scheduler.acknowledge(one, { job: "SAME" });
  test.scheduler.acknowledge(two, { job: "SAME" });
  assert.equal(test.scheduler.count(), 1);
  await test.advance(60000);
  const at = test.requests.map((request) => request.at);
  assert.ok(at.every((value, index) => index === 0 || value - at[index - 1] >= 5000));
  assert.equal(test.terminal.length, 1);
  assert.equal(test.terminal[0].same, 2);
}
verdict.coalescedFollowedAsOne = true;

// A click whose answer was lost turns out to be a job another click already followed to its
// result: nothing is said again (one terminal entry, one announcement per job).
{
  let known = false;
  const test = harness({ answer: (entry) => ok(entry.job === "JOB" || known ? { status: "done", job: "JOB" } : { status: "unknown" }) });
  const lost = test.scheduler.begin({}); // its answer never came
  const seen = test.scheduler.begin({});
  test.scheduler.acknowledge(seen, { job: "JOB" });
  await test.advance(5000);
  await test.advance(1000);
  assert.equal(test.terminal.length, 1);
  known = true; // the lost click's id now resolves to the same, finished job
  await test.advance(30000);
  assert.equal(test.terminal.length, 1);
  assert.equal(test.repeats.length, 1);
  assert.equal(test.scheduler.count(), 0);
  assert.ok(lost);
}
verdict.repeatResultSuppressed = true;

// Whether a click is still followed (app.js says "answer lost" only then): by its own id, or as
// the alias of a click TOW took as the same job; no longer once its result is shown.
{
  const test = harness({ answer: () => ok({ status: "done", job: "PJ" }) });
  const one = test.scheduler.begin({});
  const two = test.scheduler.begin({});
  test.scheduler.acknowledge(one, { job: "PJ" });
  test.scheduler.acknowledge(two, { job: "PJ" });
  assert.equal(test.scheduler.pending(one), true);
  assert.equal(test.scheduler.pending(two), true);
  await test.advance(6000);
  assert.equal(test.terminal.length, 1);
  assert.equal(test.scheduler.pending(one), false);
  assert.equal(test.scheduler.pending(two), false);
}
verdict.pendingUntilDelivered = true;

console.log(JSON.stringify(verdict));