// The header clock's tooltip names why the last attempt failed in the page's words, never the
// error class ("Last attempt: error"): from the page first, then from each answer of the page's poll
// (sync.js: /sync.json's health).
// Runs the checked-in app.js with a tiny DOM and fake timers; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let now = 1_000_000_000_000;
let nextId = 1;
const timers = new Map(); // id -> {at, fn, every}
const texts = { "js.clock.last_attempt": "Последняя попытка: {error}" };

const clock = {
  dataset: {
    last: String((now - 60 * 1000) / 1000), interval: "43200", checkOk: "0", error: "qbit", errorText: "торрент-клиент",
  },
  textContent: "",
  title: "",
  classList: { add() {}, toggle() {}, remove() {} },
};
const i18n = { textContent: JSON.stringify(texts) };
const document = {
  hidden: false,
  getElementById: (id) => (id === "next-check" ? clock : id === "tow-i18n" ? i18n : null),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
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
window.towSync = { onPoll: (fn) => polls.push(fn), health: () => answer, now() {} };
const answer = { ok: true, next_from_ts: (now - 60 * 1000) / 1000, interval_sec: 43200, check_ok: false,
  check_error: "tracker", check_error_text: "сайт недоступен" };
Object.assign(globalThis, {
  window, document, location: window.location, console, URL, URLSearchParams, FormData: class {},
  setInterval: window.setInterval, setTimeout: window.setTimeout, clearInterval: window.clearInterval,
  Date: { now: () => now },
});
require("../../src/tow/static/app.js");
const fromPage = clock.title;

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
await advance(20 * 1000);
polls.forEach((fn) => fn(null, true)); // the poll answers
console.log(JSON.stringify({ fromPage, afterPoll: clock.title }));
