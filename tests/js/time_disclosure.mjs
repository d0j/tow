// A short time's full time (app.js timeTip) for a keyboard and a touch screen, not only a mouse's
// tooltip: a time takes the focus; its focus or a tap shows the full time in one shared tip that
// describes it; Escape (without closing what is around it), a tap elsewhere or leaving it hides
// it. A time in a control (a row's summary) is left alone; times a script writes or puts in later
// get it too. Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
const short = (title, text) => ["time", { datetime: "2026-10-15T20:31:27+02:00", title, "data-short": "" }, text];
document.body.append(
  build(document, ["script", { id: "tow-i18n" }, JSON.stringify({ _tz_now: "120" })]),
  build(document, ["p", { class: "log-line" }, short("2026-10-15 20:31:27 UTC+02:00", "15 Oct, 20:31")]),
  build(document, ["details", {}, ["summary", {}, short("2026-10-14 08:00:00 UTC+02:00", "14 Oct, 08:00")]]),
  build(document, ["p", {}, ["time", { datetime: "2026-10-15T20:31:27+02:00" }, "no rule"]]),
);
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
  location: { href: "http://127.0.0.1/history", origin: "http://127.0.0.1", pathname: "/history", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.reject(new Error("no request expected")),
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  innerWidth: 390, innerHeight: 800,
};
const windowListeners = {};
context.window = { ...context, addEventListener: (type, fn) => { windowListeners[type] = fn; }, confirm: () => true };
vm.runInNewContext(source, context);
const verdict = {};
const [plain, inSummary] = document.root.querySelectorAll("time[data-short]");

// Focusable where it is not inside a control; a <time> without the rule stays as it is.
assert.equal(plain.getAttribute("tabindex"), "0");
assert.equal(inSummary.getAttribute("tabindex"), null);
const bare = document.root.querySelectorAll("time").find((node) => !node.hasAttribute("data-short"));
assert.equal(bare.getAttribute("tabindex"), null);
verdict.focusable = true;

// The keyboard's focus shows the full time in the tip that describes the time.
plain.focus();
document.dispatch(plain, "focusin");
const tip = document.getElementById("time-tip");
assert.ok(tip);
assert.equal(tip.getAttribute("role"), "tooltip");
assert.equal(tip.textContent, "2026-10-15 20:31:27 UTC+02:00");
assert.equal(tip.hidden, false);
assert.equal(plain.getAttribute("aria-describedby"), "time-tip");
verdict.focusShows = true;

// Escape hides only the tip: whatever is around the time does not see the key.
const escape = document.dispatch(plain, "keydown", { key: "Escape" });
assert.equal(escape.defaultPrevented, true);
assert.equal(tip.hidden, true);
assert.equal(plain.getAttribute("aria-describedby"), null);
verdict.escapeHides = true;

// A tap shows it; a tap elsewhere, leaving the time or a scroll hides it.
document.dispatch(plain, "click");
assert.equal(tip.hidden, false);
document.dispatch(document.body, "click");
assert.equal(tip.hidden, true);
document.dispatch(plain, "click");
document.dispatch(plain, "focusout");
assert.equal(tip.hidden, true);
document.dispatch(plain, "click");
windowListeners.scroll();
assert.equal(tip.hidden, true);
// A time in a row's summary does not show it (the summary is the control).
document.dispatch(inSummary, "click");
assert.equal(tip.hidden, true);
verdict.tapShowsAndHides = true;

// A time a script writes, and one in a panel put in later.
const made = context.window.towTime.element("2026-10-15T18:31:27Z");
assert.equal(made.getAttribute("tabindex"), "0");
document.body.append(made);
document.dispatch(made, "click");
assert.equal(tip.textContent, made.title);
const panel = build(document, ["div", { class: "edit-panel" }, ["p", {}, short("2026-10-13 10:00:00 UTC+02:00", "13 Oct, 10:00")]]);
document.body.append(panel);
context.window.towTime.prepare(panel);
assert.equal(panel.querySelector("time").getAttribute("tabindex"), "0");
verdict.laterTimes = true;

console.log(JSON.stringify(verdict));
