// A short time's full time (app.js): a tap shows it in one shared tip that describes the time;
// Escape (without closing what is around it), a tap elsewhere or a scroll hides it. A time in a
// control (a row's summary) is left alone. 1.32.1 (C5): a time is not a tab stop of its own (300
// lines of History were 300 stops); a list has one "Full timestamps" toggle that shows every
// time of that list in full - also lines the list gets later - and this tab keeps the choice.
// Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const short = (title, text) => ["time", { datetime: "2026-10-15T20:31:27+02:00", title, "data-short": "" }, text];
const page = (storage) => {
  const document = fakeDocument();
  document.body.append(
    build(document, ["script", { id: "tow-i18n" }, JSON.stringify({ _tz_now: "120" })]),
    build(document, ["button", { type: "button", "data-full-times": "history-log", "aria-pressed": "false" }, "Full timestamps"]),
    build(document, ["div", { class: "log-box", id: "history-log" },
      ["p", { class: "log-line" }, short("2026-10-15 20:31:27 UTC+02:00", "15 Oct, 20:31")],
      ["p", { class: "log-line" }, short("2026-10-14 08:00:00 UTC+02:00", "14 Oct, 08:00")]]),
    build(document, ["details", {}, ["summary", {}, short("2026-10-14 08:00:00 UTC+02:00", "14 Oct, 08:00")]]),
    build(document, ["p", {}, ["time", { datetime: "2026-10-15T20:31:27+02:00" }, "no rule"]]),
  );
  const context = {
    document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
    WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
    location: { href: "http://127.0.0.1/history", origin: "http://127.0.0.1", pathname: "/history", search: "", hash: "" },
    history: { replaceState() {} },
    sessionStorage: storage,
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: () => Promise.reject(new Error("no request expected")),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    innerWidth: 390, innerHeight: 800,
  };
  const windowListeners = {};
  context.window = { ...context, addEventListener: (type, fn) => { windowListeners[type] = fn; }, confirm: () => true };
  vm.runInNewContext(source, context);
  return { document, context, windowListeners };
};
const verdict = {};
const storage = memoryStorage();
const { document, context, windowListeners } = page(storage);
const [plain, second, inSummary] = document.root.querySelectorAll("time[data-short]");

// No time is a tab stop of its own: not the page's, not one a script writes.
assert.equal(plain.getAttribute("tabindex"), null);
assert.equal(inSummary.getAttribute("tabindex"), null);
const made = context.window.towTime.element("2026-10-15T18:31:27Z");
assert.equal(made.getAttribute("tabindex"), null);
assert.ok(made.title);
verdict.noTabStops = true;

// A tap shows the full time in the tip that describes the time; Escape hides only the tip.
document.dispatch(plain, "click");
const tip = document.getElementById("time-tip");
assert.ok(tip);
assert.equal(tip.getAttribute("role"), "tooltip");
assert.equal(tip.textContent, "2026-10-15 20:31:27 UTC+02:00");
assert.equal(tip.hidden, false);
assert.equal(plain.getAttribute("aria-describedby"), "time-tip");
const escape = document.dispatch(plain, "keydown", { key: "Escape" });
assert.equal(escape.defaultPrevented, true);
assert.equal(tip.hidden, true);
assert.equal(plain.getAttribute("aria-describedby"), null);
// A tap elsewhere or a scroll hides it; a time in a row's summary does not show it.
document.dispatch(plain, "click");
document.dispatch(document.body, "click");
assert.equal(tip.hidden, true);
document.dispatch(plain, "click");
windowListeners.scroll();
assert.equal(tip.hidden, true);
document.dispatch(inSummary, "click");
assert.equal(tip.hidden, true);
verdict.tapShowsAndHides = true;

// The list's one toggle: every time of the list in full, pressed; short again, not pressed.
const toggle = document.root.querySelector("[data-full-times]");
const list = document.getElementById("history-log");
document.dispatch(toggle, "click");
assert.equal(toggle.getAttribute("aria-pressed"), "true");
assert.equal(plain.textContent, "2026-10-15 20:31:27 UTC+02:00");
assert.equal(second.textContent, "2026-10-14 08:00:00 UTC+02:00");
assert.equal(inSummary.textContent, "14 Oct, 08:00"); // not in that list
assert.equal(storage.getItem("tow.times.full"), "1");
verdict.toggleShowsFull = true;

// Lines the list gets later follow it.
const later = build(document, ["p", { class: "log-line" }, short("2026-10-13 10:00:00 UTC+02:00", "13 Oct, 10:00")]);
list.append(later);
context.window.towTime.prepare(list);
assert.equal(later.querySelector("time").textContent, "2026-10-13 10:00:00 UTC+02:00");
verdict.laterLinesFollow = true;

document.dispatch(toggle, "click");
assert.equal(toggle.getAttribute("aria-pressed"), "false");
assert.equal(plain.textContent, "15 Oct, 20:31");
assert.equal(later.querySelector("time").textContent, "13 Oct, 10:00");
assert.equal(storage.getItem("tow.times.full"), "0");
verdict.toggleBack = true;

// The next page of this tab opens with the choice.
storage.setItem("tow.times.full", "1");
const next = page(storage);
const nextToggle = next.document.root.querySelector("[data-full-times]");
assert.equal(nextToggle.getAttribute("aria-pressed"), "true");
assert.equal(next.document.root.querySelector("#history-log time").textContent, "2026-10-15 20:31:27 UTC+02:00");
verdict.choiceKept = true;

console.log(JSON.stringify(verdict));
