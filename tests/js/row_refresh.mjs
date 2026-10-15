// checks.js draws a Home row again when its check has ended - one at a time, never while the
// owner works in it (edit panel open, a field focused or unsaved, the pointer on its buttons);
// such a row is marked stale and drawn when that ends or at the next try. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/checks.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, crypto: globalThis.crypto, Math, JSON, Array, Uint8Array, Number, String, Set, Boolean });
const { createRowRefresher, rowBusy } = window.TowRowRefresher;
const flush = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve(); };

const harness = ({ answer = async (topic) => `<row ${topic}>` } = {}) => {
  const rows = new Map([["a", { id: "row-a", dataset: {} }], ["b", { id: "row-b", dataset: {} }]]);
  const busyRows = new Set();
  const fetched = [];
  const placed = [];
  const timers = [];
  const refresher = createRowRefresher({
    fetchRow: (topic) => { fetched.push(topic); return answer(topic); },
    findRow: (topic) => rows.get(topic) || null,
    busy: (row) => busyRows.has(row.id),
    place: (row, html) => placed.push([row.id, html]),
    setTimer: (fn) => { timers.push(fn); return timers.length; },
    clearTimer: () => {},
  });
  return { refresher, rows, busyRows, fetched, placed, timers };
};
const verdict = {};

// A free row is drawn again; one TOW no longer has, or that did not answer, stays as it is.
{
  const test = harness({ answer: async (topic) => (topic === "b" ? null : `<row ${topic}>`) });
  test.refresher.refresh("a");
  test.refresher.refresh("b");
  test.refresher.refresh("gone");
  await flush();
  assert.deepEqual(test.placed, [["row-a", "<row a>"]]);
  assert.deepEqual(test.fetched, ["a", "b"]);
  assert.equal(JSON.stringify(test.refresher.stale()), "[]");
}
verdict.freeRowReplaced = true;

// A busy row is not asked for: it is stale until the work ends (settle), then drawn once.
{
  const test = harness();
  test.busyRows.add("row-a");
  test.refresher.refresh("a");
  await flush();
  assert.deepEqual(test.fetched, []);
  assert.equal(test.rows.get("a").dataset.stale, "1");
  assert.equal(JSON.stringify(test.refresher.stale()), JSON.stringify(["a"]));
  test.refresher.settle("a"); // still busy: nothing
  await flush();
  assert.deepEqual(test.fetched, []);
  test.busyRows.delete("row-a");
  test.refresher.settle("a");
  await flush();
  assert.deepEqual(test.placed, [["row-a", "<row a>"]]);
  assert.equal(JSON.stringify(test.refresher.stale()), "[]");
  test.refresher.settle("a"); // not stale any more: nothing again
  await flush();
  assert.equal(test.fetched.length, 1);
}
verdict.busyRowWaits = true;

// The owner starts working in the row while it is fetched: it is not replaced under the hands.
{
  let release;
  const test = harness({ answer: (topic) => new Promise((resolve) => { release = () => resolve(`<row ${topic}>`); }) });
  test.refresher.refresh("a");
  await flush();
  test.busyRows.add("row-a");
  release();
  await flush();
  assert.deepEqual(test.placed, []);
  assert.equal(JSON.stringify(test.refresher.stale()), JSON.stringify(["a"]));
  // The next try (every 3 s) draws it once the row is free.
  test.busyRows.delete("row-a");
  test.timers.at(-1)();
  await flush();
  release();
  await flush();
  assert.deepEqual(test.placed, [["row-a", "<row a>"]]);
}
verdict.busyDuringFetchWaits = true;

// One at a time, each row once however often it is asked for.
{
  let open = 0;
  let most = 0;
  const test = harness({ answer: async (topic) => { open += 1; most = Math.max(most, open); await flush(); open -= 1; return `<row ${topic}>`; } });
  test.refresher.refresh("a");
  test.refresher.refresh("b");
  test.refresher.refresh("b"); // asked again while waiting: drawn once
  await flush();
  await flush();
  await flush();
  assert.equal(most, 1);
  assert.deepEqual(test.fetched, ["a", "b"]);
}
verdict.oneAtATime = true;

// What makes a row busy: its edit panel open, unsaved or submitting words, a focused field, the
// pointer on its buttons. A focused Check button alone does not (the owner watches the result).
{
  const document = fakeDocument();
  const make = (extra = []) => {
    const row = build(document, ["div", { class: "row-wrap", id: "row-a" },
      ["details", { class: "row-edit" }, ["summary", {}, "Show"], ["form", { action: "/topics/a/edit" }, ["input", { name: "title" }]]],
      ["span", { class: "row-ops" }, ["form", { action: "/topics/a/check" }, ["button", {}, "Check"]]], ...extra]);
    document.body.append(row);
    return row;
  };
  const row = make();
  assert.equal(rowBusy(row, document.body), false);
  assert.equal(rowBusy(row, row.querySelector("button")), false);
  assert.equal(rowBusy(row, row.querySelector("input")), true);
  row.querySelector("details").open = true;
  assert.equal(rowBusy(row, null), true);
  row.querySelector("details").open = false;
  row.querySelector("details form").dataset.dirty = "1";
  assert.equal(rowBusy(row, null), true);
  delete row.querySelector("details form").dataset.dirty;
  assert.equal(rowBusy(row, null), false);
  row.querySelector(".row-ops").hovered = true;
  assert.equal(rowBusy(row, null), true);
}
verdict.busyRules = true;

console.log(JSON.stringify(verdict));
