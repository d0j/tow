// The header countdown and the personal timers share one poll: sync.js's, whose every answer hands
// both what /health.json said; the page asks nothing of its own (no /health.json), also not while a
// check is overdue. Runs the checked-in app.js with a tiny DOM and fake timers; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let now = 1_000_000_000_000;
let nextId = 1;
const timers = new Map();
const listeners = {};
const fetches = [];

const clock = {
  dataset: { last: String((now - 13 * 3600 * 1000) / 1000), interval: "43200", checkOk: "1", error: "" },
  textContent: "", title: "", classList: { add() {}, toggle() {}, remove() {} },
};
const value = { textContent: "" };
const words = { textContent: "" };
const timerNode = {
  dataset: { topicTimer: "t1", timerNow: String(now / 1000), timerAt: String(now / 1000 + 600), timerState: "stopped", timerMinutes: "10" },
  isConnected: true, title: "", attributes: {},
  querySelector: (selector) => (selector === "[data-timer-words]" ? words : value),
  setAttribute(key, text) { this.attributes[key] = text; }, remove() { this.isConnected = false; },
};
const document = {
  hidden: false,
  getElementById: (id) => (id === "next-check" ? clock : null),
  querySelector: () => null,
  querySelectorAll: (selector) => (selector === "[data-topic-timer]" ? [timerNode] : []),
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
  health: () => ({
    ok: true, now_ts: now / 1000, next_from_ts: (now - 13 * 3600 * 1000) / 1000, interval_sec: 43200, check_ok: true,
    topic_timers: { t1: { state: "scheduled", next_at: now / 1000 + 3600, minutes: 10 } },
  }),
};
const answer = (ok) => polls.forEach((fn) => fn(null, ok));
Object.assign(globalThis, {
  window, document, location: window.location, URL, URLSearchParams, FormData: class {},
  setInterval: window.setInterval, setTimeout: window.setTimeout, clearInterval: window.clearInterval,
  Date: { now: () => now },
  fetch: async (url) => { fetches.push({ url: String(url), at: now }); return { ok: false, json: async () => ({}) }; },
});
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
await advance(10 * 60 * 1000); // overdue all along: the page asks nothing itself
const ownRequests = fetches.length;
answer(true); // one answer of the page's poll
const timerUpdated = timerNode.dataset.timerState === "scheduled" && value.textContent === "01:00:00";
const clockShown = clock.textContent;
// The timer's meaning is its tooltip and hidden words, never an aria-label rewritten each second.
const timerWords = words.textContent === `${timerNode.title} ` && !("aria-label" in timerNode.attributes);
answer(false); // a failed answer: no new deadline, the timer is not running as far as the page knows
await advance(1000);
const failedStops = timerNode.dataset.timerState === "stopped";
console.log(JSON.stringify({ readers: polls.length, ownRequests, timerUpdated, timerWords, clock: clockShown, failedStops }));
