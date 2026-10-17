// updates.js on Settings: "Check for updates automatically" follows the config area of
// /sync.json - the box and its form's base (the value a save is merged against) take another
// device's change - but never while the owner uses it (the box being saved, an unsaved change of
// it, the form under the owner's hands, a conflict waiting for a choice): then once that ends, after
// a later poll. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/updates.js", import.meta.url), "utf8");
const document = fakeDocument();
const timers = fakeTimers();
const baseOf = (value, pending = []) => JSON.stringify({ v: 1, k: "updates", id: "updates", f: { check_updates: value }, p: pending });
const form = build(document, ["form", { method: "post", action: "/settings/updates", "data-auto-submit": "" },
  ["input", { type: "hidden", name: "base", value: baseOf(true) }],
  ["input", { type: "hidden", name: "enabled", value: "0" }],
  ["div", { class: "check-field" }, ["input", { id: "check-updates", name: "enabled", value: "1", type: "checkbox", "data-auto-submit-control": "" }]]]);
document.body.append(form, build(document, ["span", { "data-release-badge": "", hidden: "" }]));
const box = document.getElementById("check-updates");
const base = form.querySelector('input[name="base"]');
box.checked = true;
box.defaultChecked = true;
box.dataset.initialChecked = "1";
const readers = {};
const polls = [];
const areas = {};
let guarded = false;
const window = {
  setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, setInterval: timers.setInterval, clearInterval: timers.clearInterval,
  addEventListener() {},
  towSync: { on: (area, fn) => { readers[area] = fn; }, onPoll: (fn) => polls.push(fn), area: (name) => areas[name] || null },
  towPages: { protectedElement: (element) => guarded && element === form },
};
vm.runInNewContext(source, {
  window, document, JSON, Math, Number, String, Array, Object, Map, Set, Boolean, Promise, Date, URLSearchParams,
  t: (key) => key, fetch: async () => ({ ok: false, json: async () => ({}) }),
});
const deliver = (value) => {
  areas.config = { rev: String(value), theme: "auto", language: "en", language_resolved: "en", interval_sec: 3600, check_updates: value };
  readers.config(areas.config, {});
};
const polled = () => polls.forEach((fn) => fn(null, true));
const saved = () => JSON.parse(base.value).f.check_updates;
const verdict = {};

// Another device turned them off: the box and its base follow, as drawn (no unsaved change).
deliver(false);
assert.equal(box.checked, false);
assert.equal(box.defaultChecked, false);
assert.equal(box.dataset.initialChecked, "0");
assert.equal(saved(), false);
assert.deepEqual(JSON.parse(base.value), JSON.parse(baseOf(false)));
verdict.followsAnotherDevice = true;

// Under the owner's hands: nothing changes; once free, the next poll brings it.
guarded = true;
deliver(true);
assert.equal(box.checked, false);
assert.equal(saved(), false);
polled();
assert.equal(box.checked, false);
guarded = false;
polled();
assert.equal(box.checked, true);
assert.equal(saved(), true);
// Being saved (the box sent, disabled), or changed and not saved: not touched either.
form.dataset.submitting = "1";
box.disabled = true;
deliver(false);
assert.equal(box.checked, true);
delete form.dataset.submitting;
box.disabled = false;
box.checked = false; // the owner's own change, not saved yet
box.defaultChecked = true;
deliver(true);
assert.equal(box.checked, false);
assert.equal(saved(), true);
box.checked = true; // put back
polled();
assert.equal(box.checked, true);
verdict.protectedWhileInUse = true;

// A conflict waiting for the owner's choice keeps its base.
base.value = baseOf(true, ["updates"]);
deliver(false);
assert.equal(JSON.parse(base.value).p.length, 1);
assert.equal(saved(), true);
assert.equal(box.checked, true);
verdict.conflictKeepsItsBase = true;

console.log(JSON.stringify(verdict));
