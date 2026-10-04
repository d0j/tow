// F6: the home countdown polls /health.json with backoff and never in a hidden tab.
// Runs the checked-in app.js with a tiny DOM and fake timers; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let now = 1_000_000_000_000;
let nextId = 1;
const timers = new Map(); // id -> {at, fn, every}
const listeners = {};
const fetches = [];
let healthy = false;

const clock = {
  dataset: { last: String((now - 13 * 3600 * 1000) / 1000), interval: "43200", checkOk: "1", error: "" },
  textContent: "",
  title: "",
  classList: { add() {}, toggle() {}, remove() {} },
};
const document = {
  hidden: false,
  getElementById: (id) => (id === "next-check" ? clock : null),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  createElement: () => ({ append() {}, classList: { add() {} } }),
};
const schedule = (fn, ms, every) => { const id = nextId++; timers.set(id, { at: now + ms, fn, every: every ? ms : 0 }); return id; };
const window = {
  setTimeout: (fn, ms) => schedule(fn, ms, false),
  setInterval: (fn, ms) => schedule(fn, ms, true),
  clearTimeout: (id) => timers.delete(id),
  clearInterval: (id) => timers.delete(id),
  location: { assign() {}, origin: "http://127.0.0.1", href: "http://127.0.0.1/" },
  addEventListener() {},
};
const context = {
  window, document, location: window.location, console, URL, URLSearchParams, FormData: class {},
  setInterval: window.setInterval, setTimeout: window.setTimeout, clearInterval: window.clearInterval,
  Date: { now: () => now },
  fetch: async (url) => {
    fetches.push({ url: String(url), at: now });
    const next = healthy ? now / 1000 : (now - 13 * 3600 * 1000) / 1000;
    return { ok: true, json: async () => ({ ok: true, next_from_ts: next, interval_sec: 43200, check_ok: true }) };
  },
};
Object.assign(globalThis, context);
require("../../src/tow/static/app.js");

const advance = async (ms) => {
  const end = now + ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    const [id, timer] = due;
    now = timer.at;
    if (timer.every) timer.at += timer.every; else timers.delete(id);
    await timer.fn();
    await new Promise((r) => setImmediate(r));
  }
  now = end;
};
const polls = () => fetches.filter((f) => f.url.includes("/health.json"));

await advance(1000); // first tick sees the overdue check and starts polling
await advance(10 * 60 * 1000);
const overdueGaps = polls().map((f, i, all) => (i ? (f.at - all[i - 1].at) / 1000 : 0)).slice(1);
const beforeHide = polls().length;
document.hidden = true;
(listeners.visibilitychange || []).forEach((fn) => fn());
await advance(30 * 60 * 1000);
const whileHidden = polls().length - beforeHide;
document.hidden = false;
(listeners.visibilitychange || []).forEach((fn) => fn());
await new Promise((r) => setImmediate(r));
const onShow = polls().length - beforeHide - whileHidden;
healthy = true; // the scheduled check ran
await advance(5 * 60 * 1000);
const afterRecovery = polls().length;
await advance(30 * 60 * 1000);
console.log(JSON.stringify({
  overdueGaps,
  whileHidden,
  onShow,
  pollsAfterRecovery: polls().length - afterRecovery,
}));
