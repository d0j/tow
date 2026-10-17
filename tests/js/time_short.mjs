// window.towTime (app.js): the one time rule of the server's short_time in the page scripts -
// today "20:31", this year "15 Oct, 20:31", another year "15 Oct 2025, 20:31", to come dated - in
// TOW's own zone (base.html: _tz_name when the server knows it, _tz_now). A time the server wrote
// (a <time datetime>, its words {iso, short, full}) is read with the offset it carries (summer or
// winter); other times with the name's rules, else the offset of now. Every short time is written
// again when the tab is shown and at midnight, with or without the name. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const DAY = 86400000;
const NOW = Date.UTC(2026, 9, 16, 18, 45); // with TOW at UTC+02:17: 16 Oct, 21:02
const MONTHS = {
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  ru: ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
};
const texts = (lang, zone) => ({
  ...Object.fromEntries(MONTHS[lang].map((name, index) => [`time.month_short.${index + 1}`, name])),
  "time.this_year": "{day} {month}, {time}",
  "time.other_year": "{day} {month} {year}, {time}",
  _datetime: lang === "ru" ? "%d.%m.%Y %H:%M:%S" : "%Y-%m-%d %H:%M:%S",
  _tz_now: String(zone.now),
  ...(zone.name ? { _tz_name: zone.name } : {}),
});

const load = (lang, zone, extra = [], health = {}) => {
  const document = fakeDocument();
  document.body.append(build(document, ["script", { id: "tow-i18n" }, JSON.stringify(texts(lang, zone))]), ...extra.map((spec) => build(document, spec)));
  const timers = [];
  const context = {
    document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Map, Set, Boolean, RegExp,
    WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent,
    Date: class extends Date { static now() { return NOW; } },
    location: { href: "http://127.0.0.1/", origin: "http://127.0.0.1", pathname: "/", search: "", hash: "" },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    setTimeout: (fn, ms) => { timers.push(ms); return timers.length; }, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  };
  const polls = [];
  // sync.js's poll: each answer hands /health.json's words (tz_now among them) to the page's readers.
  const towSync = { onPoll: (fn) => polls.push(fn), health: () => ({ ok: true, ...health }), now() {} };
  context.window = { ...context, towSync, addEventListener() {}, confirm: () => true };
  vm.runInNewContext(source, context);
  return { document, time: context.window.towTime, timers, answer: () => polls.forEach((fn) => fn(null, true)) };
};

const verdict = {};
// An offset no real zone has: the fixed offset is used whatever this machine's zone is.
const odd = { now: 137 };
const en = load("en", odd).time;
const ru = load("ru", odd).time;
const at = (...fields) => Date.UTC(...fields);

assert.equal(en.short(at(2026, 9, 16, 10, 0)), "12:17"); // today
assert.equal(en.short(at(2026, 9, 15, 22, 0)), "00:17"); // the 15th in UTC is already the 16th for TOW
assert.equal(en.short(NOW + 30000), "21:02"); // within the minute: now
verdict.today = true;

assert.equal(en.short("2026-10-15T20:31:27+00:00"), "15 Oct, 22:48");
assert.equal(ru.short("2026-10-15T20:31:27+00:00"), "15 окт, 22:48");
assert.equal(en.short(at(2026, 4, 3, 7, 0)), "3 May, 09:17");
assert.equal(ru.short(at(2026, 4, 3, 7, 0)), "3 мая, 09:17");
verdict.thisYear = true;

assert.equal(en.short("2025-10-15T00:00:00Z"), "15 Oct 2025, 02:17");
assert.equal(ru.short("2025-10-15T00:00:00Z"), "15 окт 2025, 02:17");
verdict.anotherYear = true;

assert.equal(en.short(NOW + 2 * 3600000), "16 Oct, 23:02"); // later today: scheduled, dated
assert.equal(en.short(at(2027, 0, 2, 6, 0)), "2 Jan 2027, 08:17");
verdict.toComeIsDated = true;

// Every way a time comes: milliseconds, seconds, digits, ISO; nothing and words stay as they are.
const seconds = Math.floor(at(2026, 9, 15, 20, 31) / 1000);
assert.equal(en.short(seconds), "15 Oct, 22:48");
assert.equal(en.short(String(seconds)), "15 Oct, 22:48");
assert.equal(en.short(seconds * 1000), "15 Oct, 22:48");
assert.equal(en.short("soon"), "soon");
assert.equal(en.short(null), "");
assert.equal(en.iso("soon"), "");
verdict.everyValue = true;

// The tooltip and the datetime: seconds and TOW's UTC offset.
assert.equal(en.full("2026-10-15T20:31:27+00:00"), "2026-10-15 22:48:27 UTC+02:17");
assert.equal(ru.full("2026-10-15T20:31:27+00:00"), "15.10.2026 22:48:27 UTC+02:17");
assert.equal(en.iso("2026-10-15T20:31:27+00:00"), "2026-10-15T22:48:27+02:17");
const west = load("en", { now: -300 }).time;
assert.equal(west.full("2026-10-15T20:31:27+00:00"), "2026-10-15 15:31:27 UTC-05:00");
assert.equal(load("en", { now: 0 }).time.full("2026-10-15T20:31:27+00:00"), "2026-10-15 20:31:27 UTC");
verdict.fullTimeAndOffset = true;

// TOW's zone by name: every moment with its own offset, whatever zone the browser is in. With
// TOW in Jerusalem in summer (+03:00) a winter time is +02:00 - the offset of now put it an hour late.
{
  const named = load("en", { now: 180, name: "Asia/Jerusalem" }).time;
  assert.equal(named.short(at(2026, 0, 10, 10, 0)), "10 Jan, 12:00");
  assert.equal(named.full(at(2026, 0, 10, 10, 0)), "2026-01-10 12:00:00 UTC+02:00");
  assert.equal(named.iso(at(2026, 0, 10, 10, 0)), "2026-01-10T12:00:00+02:00");
  assert.equal(named.short(at(2026, 6, 10, 10, 0)), "10 Jul, 13:00");
  assert.equal(named.short(at(2026, 9, 16, 18, 0)), "21:00"); // today, summer
  assert.equal(named.short(at(2025, 11, 31, 22, 30)), "1 Jan, 00:30"); // already the new year there
  // A name the browser does not know: the offset of now, as without one.
  assert.equal(load("en", { now: 137, name: "Nowhere/Atlantis" }).time.short(at(2026, 0, 10, 10, 0)), "10 Jan, 12:17");
}
verdict.namedZoneHasEachMomentsOffset = true;

// Without the name (Windows, most services): a <time> the server wrote is written again when the
// tab is shown and at midnight, read with the offset its datetime carries (that moment's, summer or
// winter), never with today's. The page was written yesterday evening: "20:31" was today then.
{
  const page = load("en", odd, [
    ["time", { id: "evening", datetime: "2026-10-15T20:31:00+02:17", "data-short": "", title: "x" }, "20:31"],
    ["time", { id: "winter", datetime: "2026-01-10T12:00:00+01:17", "data-short": "", title: "x" }, "12:00"],
    ["time", { id: "today", datetime: "2026-10-16T21:00:00+02:17", "data-short": "", title: "x" }, "stale"],
  ]);
  const node = (id) => page.document.root.querySelector(`#${id}`);
  // 21:02 now: 2 h 58 min to midnight, then a second - kept for the server's times alone.
  assert.ok(page.timers.includes((2 * 60 + 58) * 60000 + 1000), page.timers);
  page.document.dispatch(page.document.body, "visibilitychange");
  assert.equal(node("evening").textContent, "15 Oct, 20:31");
  assert.equal(node("winter").textContent, "10 Jan, 12:00"); // its own +01:17, not today's +02:17
  assert.equal(node("today").textContent, "21:00");
  // The server's words for a time are shown as they are, and follow midnight like the others.
  const words = page.time.element({ iso: "2026-01-10T12:00:00+02:00", short: "12:00", full: "2026-01-10 12:00:00 UTC+02:00" });
  assert.equal(words.getAttribute("datetime"), "2026-01-10T12:00:00+02:00");
  assert.equal(words.textContent, "12:00");
  assert.equal(words.title, "2026-01-10 12:00:00 UTC+02:00");
  page.document.body.append(words);
  page.document.dispatch(page.document.body, "visibilitychange");
  assert.equal(words.textContent, "10 Jan, 12:00");
  assert.equal(page.time.element({ iso: "", short: "", full: "" }, "written").textContent, "written");
  // A time written now (a live message): TOW's offset of now, and it follows midnight.
  const made = page.time.element("2026-10-15T20:31:27+00:00");
  assert.equal(made.tagName, "TIME");
  assert.equal(made.getAttribute("datetime"), "2026-10-15T22:48:27+02:17");
  assert.equal(made.title, "2026-10-15 22:48:27 UTC+02:17");
  assert.equal(made.textContent, "15 Oct, 22:48");
  assert.ok(made.hasAttribute("data-short"));
  page.document.body.append(made);
  made.textContent = "old";
  page.document.dispatch(page.document.body, "visibilitychange");
  assert.equal(made.textContent, "15 Oct, 22:48");
  assert.equal(page.time.element("15.10.2026 22:48 CET", "15.10.2026 22:48 CET").textContent, "15.10.2026 22:48 CET");
}
verdict.withoutTheNameEveryTimeIsWrittenAgainWithItsOwnOffset = true;

// "Today" by TOW's offset now, which changes twice a year: each answer of the page's poll (sync.js,
// which asks at once when the tab is shown again) brings it. 18:45 UTC is 21:02 at +02:17, but already the 17th at
// +06:00: a time of the 16th written "21:00" becomes dated.
{
  const page = load("en", odd, [["time", { datetime: "2026-10-16T21:00:00+02:17", "data-short": "", title: "x" }, "21:00"]], { tz_now: 360 });
  const node = page.document.root.querySelector("time[data-short]");
  page.answer();
  assert.equal(node.textContent, "16 Oct, 21:00");
  assert.equal(page.time.short(NOW - 1800000), "00:15"); // its own times now by the new offset
}
verdict.todayFollowsTowsOffsetFromTheServer = true;

// With the name: the server's <time>s are written again when the tab is shown and at TOW's
// midnight, and a script computes the server's words itself.
{
  const page = load("en", { now: 180, name: "Asia/Jerusalem" }, [["time", { datetime: "2026-10-16T21:00:00+03:00", "data-short": "", title: "x" }, "stale"]]);
  const node = page.document.root.querySelector("time[data-short]");
  page.document.dispatch(page.document.body, "visibilitychange");
  assert.equal(node.textContent, "21:00");
  // 21:45 now in Jerusalem: 2 h 15 min to midnight, then a second.
  assert.ok(page.timers.includes((2 * 60 + 15) * 60000 + 1000), page.timers);
  const words = page.time.element({ iso: "2026-01-10T12:00:00+02:00", short: "stale words", full: "x" });
  assert.equal(words.textContent, "10 Jan, 12:00");
  assert.equal(words.title, "2026-01-10 12:00:00 UTC+02:00");
}
verdict.withTheNameEveryTimeIsWrittenAgain = true;

console.log(JSON.stringify(verdict));