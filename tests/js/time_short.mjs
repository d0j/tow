// window.towTime (app.js): the one time rule of the server's short_time in the page scripts -
// today "20:31", this year "15 Oct, 20:31", another year "15 Oct 2025, 20:31", to come dated - in
// TOW's own zone (base.html: _tz_now, _tz_half), the browser's calendar only when its zone is the
// same; "today" written again when the tab is shown and at midnight. Prints a JSON verdict.
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
  _tz_half: String(zone.half),
});

const load = (lang, zone, extra = []) => {
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
  context.window = { ...context, addEventListener() {}, confirm: () => true };
  vm.runInNewContext(source, context);
  return { document, time: context.window.towTime, timers };
};

const verdict = {};
// An offset no real zone has: the fixed offset is used whatever this machine's zone is.
const odd = { now: 137, half: 137 };
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
const west = load("en", { now: -300, half: -300 }).time;
assert.equal(west.full("2026-10-15T20:31:27+00:00"), "2026-10-15 15:31:27 UTC-05:00");
const utc = load("en", { now: 0, half: 0 });
if (new Date(NOW).getTimezoneOffset() !== 0 || new Date(NOW - 182 * DAY).getTimezoneOffset() !== 0) {
  assert.equal(utc.time.full("2026-10-15T20:31:27+00:00"), "2026-10-15 20:31:27 UTC");
}
verdict.fullTimeAndOffset = true;

// A browser in TOW's zone (now and half a year ago): its own calendar, summer time included.
{
  const local = { now: -new Date(NOW).getTimezoneOffset(), half: -new Date(NOW - 182 * DAY).getTimezoneOffset() };
  const time = load("en", local).time;
  const two = (n) => String(n).padStart(2, "0");
  for (const value of [NOW - 3 * 3600000, NOW - 40 * DAY, NOW - 200 * DAY, NOW - 400 * DAY]) {
    const d = new Date(value);
    const today = new Date(NOW);
    const clock = `${two(d.getHours())}:${two(d.getMinutes())}`;
    const same = d.toDateString() === today.toDateString();
    const expected = same ? clock : d.getFullYear() === today.getFullYear()
      ? `${d.getDate()} ${MONTHS.en[d.getMonth()]}, ${clock}` : `${d.getDate()} ${MONTHS.en[d.getMonth()]} ${d.getFullYear()}, ${clock}`;
    assert.equal(time.short(value), expected);
    const offset = -d.getTimezoneOffset();
    assert.ok(time.full(value).endsWith(offset ? `UTC${offset > 0 ? "+" : "-"}${two(Math.floor(Math.abs(offset) / 60))}:${two(Math.abs(offset) % 60)}` : "UTC"));
  }
}
verdict.browserInTowsZone = true;

// A <time> the server wrote is written again when the tab is shown, and at TOW's midnight.
{
  const page = load("en", odd, [["time", { datetime: "2026-10-16T21:00:00+02:17", "data-short": "", title: "x" }, "stale"]]);
  const node = page.document.root.querySelector("time[data-short]");
  page.document.dispatch(page.document.body, "visibilitychange");
  assert.equal(node.textContent, "21:00");
  // 21:02 now: 2 h 58 min to midnight, then a second.
  assert.ok(page.timers.includes((2 * 60 + 58) * 60000 + 1000), page.timers);
  const made = page.time.element("2026-10-15T20:31:27+00:00");
  assert.equal(made.tagName, "TIME");
  assert.equal(made.getAttribute("datetime"), "2026-10-15T22:48:27+02:17");
  assert.equal(made.title, "2026-10-15 22:48:27 UTC+02:17");
  assert.equal(made.textContent, "15 Oct, 22:48");
  assert.ok(made.hasAttribute("data-short"));
  assert.equal(page.time.element("15.10.2026 22:48 CET", "15.10.2026 22:48 CET").textContent, "15.10.2026 22:48 CET");
}
verdict.writtenAgain = true;

console.log(JSON.stringify(verdict));
