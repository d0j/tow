// F6: the home countdown asks nothing of its own while a check is overdue (it once polled /health.json
// every 5 s); sync.js's poll, whose cadence and hidden-tab rule are its own (sync_client.mjs), hands it
// each answer: a healthy one brings the countdown back, a failed one keeps what it shows.
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
const polls = [];
window.towSync = {
  onPoll: (fn) => polls.push(fn), now() {},
  health: () => ({ ok: true, next_from_ts: healthy ? now / 1000 : (now - 13 * 3600 * 1000) / 1000, interval_sec: 43200, check_ok: true }),
};
const answer = (ok) => polls.forEach((fn) => fn(null, ok));
const context = {
  window, document, location: window.location, console, URL, URLSearchParams, FormData: class {},
  setInterval: window.setInterval, setTimeout: window.setTimeout, clearInterval: window.clearInterval,
  Date: { now: () => now },
  fetch: async (url) => { fetches.push({ url: String(url), at: now }); return { ok: false, json: async () => ({}) }; },
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
await advance(1000);
await advance(10 * 60 * 1000);
const overdue = clock.textContent;
const ownRequests = fetches.length;
answer(false); // a failed answer keeps what it shows
const afterFailed = clock.textContent;
healthy = true; // the scheduled check ran
answer(true);
await advance(1000);
console.log(JSON.stringify({ overdue, ownRequests, afterFailed, recovered: clock.textContent }));
