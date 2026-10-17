// sync.js's Home rows without the page (createRowSync): what the topics area says reaches the list
// - new rows in their place, removed rows gone, changed rows drawn again - in batches of at most 200
// ids and two requests at a time. A fragment of another revision is never used, a failed one is
// asked again, and the area counts as applied only when every row is there. The whole manifest
// (after a restart) is reconciled by its hashes, removals too. A row under the owner's hands stays,
// with a note, and follows once the work ends; protection is checked again right before a row is
// put in. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/sync.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, Boolean, Date });
const { createRowSync } = window.TowSync;
const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
const REV = (n) => String(n).repeat(32).slice(0, 32);

// The page: rows as plain objects {id, hash, html, note}; the server: rows by id (hash, html).
const harness = ({ page = [], server = {}, answer = null, now = undefined } = {}) => {
  let rows = page.map((id) => ({ id: `row-${id}`, dataset: { rowHash: `h-${id}` }, html: `old ${id}`, note: "" }));
  const guarded = new Set();
  const requests = [];
  const acks = [];
  const emptied = [];
  let retries = 0;
  let open = 0;
  let most = 0;
  let serverRev = REV(1);
  const sync = createRowSync({
    rows: () => rows,
    fetchRows: async (ids, rev) => {
      requests.push({ ids: [...ids], rev });
      open += 1;
      most = Math.max(most, open);
      await flush();
      open -= 1;
      if (answer) return answer(ids, rev);
      return {
        status: 200,
        body: {
          ok: true, rev: serverRev, order: Object.keys(server),
          rows: ids.filter((id) => server[id]).map((id) => ({ id, html: server[id].html, hash: server[id].hash })),
          missing: ids.filter((id) => !server[id]), deferred: [], stale: rev !== serverRev,
        },
      };
    },
    isProtected: (row) => guarded.has(row.id),
    place: (row, fragment, beforeId) => {
      const fresh = { id: `row-${fragment.id}`, dataset: { rowHash: fragment.hash }, html: fragment.html, note: "" };
      if (row) rows = rows.map((each) => (each === row ? fresh : each));
      else {
        const index = beforeId ? rows.findIndex((each) => each.id === `row-${beforeId}`) : -1;
        if (index < 0) rows.push(fresh); else rows.splice(index, 0, fresh);
      }
      return fresh;
    },
    removeRow: (row) => { rows = rows.filter((each) => each !== row); },
    note: (row, kind) => { row.note = kind; },
    ack: (rev) => acks.push(rev),
    retry: () => { retries += 1; },
    emptied: (has) => emptied.push(has),
    ...(now ? { now } : {}),
  });
  return {
    sync, requests, acks, emptied, guarded,
    ids: () => rows.map((row) => row.id.slice(4)),
    row: (id) => rows.find((row) => row.id === `row-${id}`),
    retries: () => retries,
    most: () => most,
    setRev: (rev) => { serverRev = rev; },
  };
};
const verdict = {};

// A delta: a new row in its place (before the row that follows it), a removed row gone, a
// changed row drawn again; the revision is applied once all of it is there.
{
  const server = { a: { hash: "h-a", html: "a" }, n: { hash: "h-n", html: "new n" }, b: { hash: "h-b2", html: "b changed" } };
  const test = harness({ page: ["a", "b", "c"], server });
  const done = await test.sync.topics({ rev: REV(1), full: false, order: ["a", "n", "b"], changed: ["b"], removed: ["c"] });
  assert.equal(done, true);
  assert.deepEqual(test.ids(), ["a", "n", "b"]);
  assert.equal(test.row("b").html, "b changed");
  assert.equal(test.row("a").html, "old a"); // unchanged: never fetched
  assert.deepEqual(test.requests.map((r) => r.ids), [["b", "n"]]);
  assert.equal(test.requests[0].rev, REV(1));
  assert.deepEqual(test.acks, [REV(1)]);
}
verdict.insertRemoveRedraw = true;

// After a restart the server sends the whole manifest: only rows whose hash differs are fetched,
// rows it no longer lists go - a partial diff is never assumed.
{
  const server = { a: { hash: "h-a", html: "a" }, b: { hash: "h-b9", html: "b new" }, d: { hash: "h-d", html: "d" } };
  const test = harness({ page: ["a", "b", "c"], server });
  await test.sync.topics({ rev: REV(1), full: true, order: ["a", "b", "d"], rows: { a: "h-a", b: "h-b9", d: "h-d" } });
  assert.deepEqual(test.ids(), ["a", "b", "d"]);
  assert.deepEqual(test.requests.map((r) => r.ids), [["b", "d"]]);
  assert.deepEqual(test.acks, [REV(1)]);
}
verdict.ringMissManifest = true;

// A row the owner works in: changed elsewhere, it stays with a note; removed elsewhere, it stays
// with a note. Once the work ends they follow: drawn again, removed.
{
  const server = { a: { hash: "h-a2", html: "a changed" } };
  const test = harness({ page: ["a", "c"], server });
  test.guarded.add("row-a");
  test.guarded.add("row-c");
  await test.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: ["c"] });
  assert.equal(test.row("a").html, "old a");
  assert.equal(test.row("a").note, "changed");
  assert.equal(test.row("c").note, "removed");
  assert.deepEqual(test.acks, [REV(1)]); // what could be applied was; the rest follows the owner
  test.guarded.clear();
  await test.sync.settle();
  assert.equal(test.row("a").html, "a changed");
  assert.equal(test.row("a").note, "");
  assert.equal(test.row("c"), undefined);
}
verdict.protectedRowsWaitWithNote = true;

// The owner starts working in a row while its fragment is on its way: checked again right before
// it is put in, it stays.
{
  const server = { a: { hash: "h-a2", html: "a changed" } };
  let guardNow = null;
  const test = harness({
    page: ["a"], server,
    answer: (ids, rev) => { guardNow(); return { status: 200, body: { ok: true, rev, order: ["a"], rows: [{ id: "a", html: "a changed", hash: "h-a2" }], missing: [], deferred: [] } }; },
  });
  guardNow = () => test.guarded.add("row-a");
  await test.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] });
  assert.equal(test.row("a").html, "old a");
  assert.equal(test.row("a").note, "changed");
}
verdict.protectionCheckedBeforeApply = true;

// A fragment drawn from another revision is not used; the area is not applied and the server is
// asked again soon. A failed request is not applied either and is asked again with the next poll.
{
  const server = { a: { hash: "h-a2", html: "a changed" } };
  const test = harness({ page: ["a"], server });
  test.setRev(REV(2));
  const done = await test.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] });
  assert.equal(done, false);
  assert.equal(test.row("a").html, "old a");
  assert.deepEqual(test.acks, []);
  assert.equal(test.retries(), 1);
  let fail = true;
  const failing = harness({
    page: ["a"], server,
    answer: (ids, rev) => (fail ? { status: 500, body: null } : { status: 200, body: { ok: true, rev, order: ["a"], rows: [{ id: "a", html: "a changed", hash: "h-a2" }], missing: [], deferred: [] } }),
  });
  assert.equal(await failing.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] }), false);
  assert.deepEqual(failing.acks, []);
  fail = false;
  assert.equal(await failing.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] }), true);
  assert.equal(failing.row("a").html, "a changed");
  assert.deepEqual(failing.acks, [REV(1)]);
}
verdict.staleDiscardedFailedRetried = true;

// The rows whose first request failed are asked again by ``settle`` (after every successful poll,
// also a 304 that brings the area no more) for the same revision - 5, 10, 20 s… after the failure,
// never sooner - and the revision is applied once they are all there.
{
  let clock = 0;
  let fail = true;
  const server = { a: { hash: "h-a2", html: "a changed" }, b: { hash: "h-b", html: "b" } };
  const test = harness({
    page: ["a", "b"], server, now: () => clock,
    answer: (ids, rev) => (fail ? { status: 500, body: null } : { status: 200, body: { ok: true, rev, order: ["a", "b"], rows: ids.map((id) => ({ id, html: server[id].html, hash: server[id].hash })), missing: [], deferred: [] } }),
  });
  assert.equal(await test.sync.topics({ rev: REV(1), full: false, order: ["a", "b"], changed: ["a"], removed: [] }), false);
  assert.deepEqual(test.acks, []);
  await test.sync.settle(); // too soon
  assert.equal(test.requests.length, 1);
  clock = 5000;
  await test.sync.settle(); // fails again: next in 10 s
  assert.equal(test.requests.length, 2);
  assert.deepEqual(test.requests[1], { ids: ["a"], rev: REV(1) });
  fail = false;
  clock = 14000;
  await test.sync.settle();
  assert.equal(test.requests.length, 2);
  clock = 15000;
  await test.sync.settle();
  assert.equal(test.row("a").html, "a changed");
  assert.deepEqual(test.acks, [REV(1)]);
  clock = 100000;
  await test.sync.settle(); // nothing left to ask
  assert.equal(test.requests.length, 3);
}
verdict.failedRowsAskedAgainBySettle = true;

// A newer topics area while an older one is still being fetched: the older answer is not used.
{
  const server = { a: { hash: "h-a3", html: "a third" } };
  let release = [];
  const test = harness({
    page: ["a"], server,
    answer: (ids, rev) => new Promise((resolve) => release.push(() => resolve({ status: 200, body: { ok: true, rev, order: ["a"], rows: [{ id: "a", html: rev === REV(1) ? "a second" : "a third", hash: rev === REV(1) ? "h-a2" : "h-a3" }], missing: [], deferred: [] } }))),
  });
  const first = test.sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] });
  await flush();
  const second = test.sync.topics({ rev: REV(2), full: false, order: ["a"], changed: ["a"], removed: [] });
  await flush();
  release[1]();
  await second;
  release[0]();
  await first;
  assert.equal(test.row("a").html, "a third");
  assert.deepEqual(test.acks, [REV(2)]);
}
verdict.olderAnswerDiscarded = true;

// 500 new rows: at most 200 ids a request, at most two requests at a time; a deferred row
// (the answer's size limit) is asked again.
{
  const ids = Array.from({ length: 500 }, (_, i) => `t${i}`);
  let deferOnce = true;
  const test = harness({
    page: ["t0"], // a Home that has its list
    answer: (asked, rev) => {
      const deferred = deferOnce ? asked.slice(-5) : [];
      deferOnce = false;
      const drawn = asked.filter((id) => !deferred.includes(id));
      return { status: 200, body: { ok: true, rev, order: ids, rows: drawn.map((id) => ({ id, html: id, hash: `h-${id}` })), missing: [], deferred } };
    },
  });
  assert.equal(await test.sync.topics({ rev: REV(1), full: true, order: ids, rows: Object.fromEntries(ids.map((id) => [id, `h-${id}`])) }), true);
  assert.ok(test.requests.every((r) => r.ids.length <= 200));
  assert.equal(test.most(), 2);
  assert.equal(test.ids().length, 500);
  assert.equal(new Set(test.ids()).size, 500);
  assert.ok(test.requests.some((r) => r.ids.length === 5)); // the deferred ones, again
}
verdict.batchesBoundedAndDeferredAgain = true;

// The first topic on an empty Home and the last one gone: the page is told (it reloads when idle);
// an empty Home that stays empty is told nothing.
{
  const empty = harness({ page: [], server: { a: { hash: "h-a", html: "a" } } });
  assert.equal(await empty.sync.topics({ rev: REV(1), full: true, order: ["a"], rows: { a: "h-a" } }), false);
  assert.deepEqual(empty.emptied, [true]);
  const still = harness({ page: [], server: {} });
  await still.sync.topics({ rev: REV(1), full: true, order: [], rows: {} });
  assert.deepEqual(still.emptied, [null]);
  const last = harness({ page: ["a"], server: {} });
  await last.sync.topics({ rev: REV(1), full: false, order: [], changed: [], removed: ["a"] });
  assert.deepEqual(last.emptied, [false]);
  assert.deepEqual(last.ids(), []);
}
verdict.emptyHomeTransitions = true;

// A check that started or ended elsewhere: its row is drawn again (its words "Checking…").
{
  const server = { a: { hash: "h-a", html: "a checking" } };
  const test = harness({ page: ["a"], server });
  await test.sync.topics({ rev: REV(1), full: true, order: ["a"], rows: { a: "h-a" } });
  assert.equal(test.requests.length, 0);
  await test.sync.redraw(["a"]);
  assert.equal(test.row("a").html, "a checking");
}
verdict.jobsRedrawRows = true;

// Signed out while rows are asked: the page is told, nothing is applied.
{
  let ended = 0;
  const rows = [{ id: "row-a", dataset: { rowHash: "x" } }];
  const sync = createRowSync({
    rows: () => rows,
    fetchRows: async () => ({ status: 401 }),
    place: () => { throw new Error("must not place"); },
    removeRow: () => {},
    signedOut: () => { ended += 1; },
    ack: () => { throw new Error("must not ack"); },
  });
  assert.equal(await sync.topics({ rev: REV(1), full: false, order: ["a"], changed: ["a"], removed: [] }), false);
  assert.equal(ended, 1);
}
verdict.signedOutStops = true;

console.log(JSON.stringify(verdict));
