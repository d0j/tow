// The recent-messages centre of messages.js with a fake clock: reading times per kind, pausing,
// collapsing, reading, announcing once, the tab's storage and its fallback, Undo. Prints a JSON
// verdict (every value true).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/messages.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, document: { getElementById: () => null }, URL });
const createCenter = window.TowMessageCenter;
assert.equal(typeof createCenter, "function");

const clock = () => {
  let now = 1_800_000_000_000;
  let next = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimer: (fn, ms) => { timers.set(next, { at: now + ms, fn }); return next++; },
    clearTimer: (id) => timers.delete(id),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = end;
    },
  };
};
const memoryStore = (data = new Map()) => ({
  data,
  get: (key) => (data.has(key) ? data.get(key) : null),
  set: (key, value) => data.set(key, value),
  remove: (key) => data.delete(key),
  keys: () => [...data.keys()],
});
const make = (options = {}) => {
  const time = clock();
  const said = [];
  const center = createCenter({ ...time, announce: (item) => said.push(item.id), ...options });
  return { center, time, said };
};
const verdict = {};

// Reading time per kind, in the foreground: ok and info 10 s, warn 25 s, err 30 s.
for (const [kind, ms] of [["ok", 10000], ["info", 10000], ["warn", 25000], ["err", 30000]]) {
  const { center, time } = make();
  center.show({ id: kind, kind, text: "x" });
  time.advance(ms - 1);
  assert.equal(center.active(), kind);
  time.advance(1);
  assert.equal(center.active(), "");
  assert.equal(center.unread(), 1); // collapsed by time: still unread, the icon shows it
}
verdict.readingTimes = true;

// Hovered, focused or in a hidden tab: the time stops and the rest of it runs afterwards.
{
  const { center, time } = make();
  center.show({ id: "a", kind: "err", text: "x" });
  time.advance(4000);
  center.hold("pointer");
  time.advance(120000);
  assert.equal(center.active(), "a");
  center.hold("focus");
  center.release("pointer");
  time.advance(60000);
  assert.equal(center.active(), "a");
  center.release("focus");
  center.hold("hidden");
  time.advance(60000);
  assert.equal(center.active(), "a");
  center.release("hidden");
  time.advance(25999);
  assert.equal(center.active(), "a");
  time.advance(1);
  assert.equal(center.active(), "");
}
verdict.pauseResume = true;

// × collapses into the history and marks it read; nothing is erased.
{
  const { center } = make();
  center.show({ id: "a", kind: "warn", text: "x" });
  center.dismiss("a");
  assert.equal(center.active(), "");
  assert.equal(center.unread(), 0);
  assert.equal(center.items().length, 1);
}
verdict.closeCollapsesAndReads = true;

// The icon's colour: the worst unread one (err > warn > ok > info).
{
  const { center } = make();
  center.show({ id: "i", kind: "info", text: "x" });
  assert.equal(center.worst(), "info");
  center.show({ id: "o", kind: "ok", text: "x" });
  assert.equal(center.worst(), "ok");
  center.show({ id: "e", kind: "err", text: "x" });
  center.show({ id: "w", kind: "warn", text: "x" });
  assert.equal(center.worst(), "err");
  center.markRead(["e"]);
  assert.equal(center.worst(), "warn");
  assert.equal(center.unread(), 3);
  center.markAllRead();
  assert.equal(center.unread(), 0);
}
verdict.worstUnread = true;

// Announced once: the same message again (a reload, Back) is history, not news.
{
  const store = memoryStore();
  const first = make({ storage: store, key: "tow.messages.n1" });
  first.center.show({ id: "a", kind: "ok", text: "x" });
  assert.deepEqual(first.said, ["a"]);
  first.center.show({ id: "a", kind: "ok", text: "x" });
  assert.deepEqual(first.said, ["a"]);
  const reloaded = make({ storage: store, key: "tow.messages.n1" });
  reloaded.center.show({ id: "a", kind: "ok", text: "x" });
  assert.deepEqual(reloaded.said, []);
  assert.equal(reloaded.center.active(), "");
  assert.equal(reloaded.center.items().length, 1);
}
verdict.announcedOnce = true;

// A new message never replaces the one under the pointer or the focus: it waits, then shows.
{
  const { center, said } = make();
  center.show({ id: "a", kind: "ok", text: "x" });
  center.hold("focus");
  center.show({ id: "b", kind: "err", text: "y" });
  assert.equal(center.active(), "a");
  assert.deepEqual(said, ["a", "b"]); // b is said when it arrives
  center.release("focus");
  assert.equal(center.active(), "b");
  // Opening the history shows them all: nothing waits for the strip any more.
  center.hold("pointer");
  center.show({ id: "c", kind: "ok", text: "z" });
  center.collapse("panel");
  center.release("pointer");
  assert.equal(center.active(), "");
}
verdict.replacementProtected = true;

// The tab's storage: kept for the same install and sign-in, dropped for another; blocked storage
// falls back to memory without breaking anything.
{
  const store = memoryStore();
  make({ storage: store, key: "tow.messages.n1" }).center.show({ id: "a", kind: "ok", text: "x" });
  assert.equal(make({ storage: store, key: "tow.messages.n1" }).center.items().length, 1);
  store.set("unrelated", "1");
  const other = make({ storage: store, key: "tow.messages.n2" });
  assert.equal(other.center.items().length, 0);
  assert.equal(store.get("tow.messages.n1"), null);
  assert.equal(store.get("unrelated"), "1");
  const blocked = {
    get() { throw new Error("SecurityError"); }, set() { throw new Error("SecurityError"); },
    remove() { throw new Error("SecurityError"); }, keys() { throw new Error("SecurityError"); },
  };
  const fallback = make({ storage: blocked, key: "tow.messages.n1" });
  fallback.center.show({ id: "b", kind: "err", text: "y" });
  assert.equal(fallback.center.items().length, 1);
  assert.equal(fallback.center.active(), "b");
  assert.equal(fallback.center.stored(), false);
  const full = memoryStore();
  full.set = () => { throw new Error("QuotaExceededError"); };
  const quota = make({ storage: full, key: "tow.messages.n1" });
  quota.center.show({ id: "c", kind: "ok", text: "z" });
  assert.equal(quota.center.items().length, 1);
}
verdict.storageAndFallback = true;

// Only plain words are stored: a link keeps its host, a path goes; a link of a message stays on
// this TOW.
{
  const store = memoryStore();
  const { center } = make({ storage: store, key: "tow.messages.n1" });
  center.show({
    id: "a", kind: "err", subject: "Show", href: "https://evil.example/x", link: "Open",
    text: "failed: https://user:secret@tracker.example/t?id=1 at D:\\TV\\Show and /srv/media/show/x.mkv",
  });
  const stored = store.get("tow.messages.n1");
  assert.ok(!stored.includes("secret"));
  assert.ok(!stored.includes("TV"));
  assert.ok(!stored.includes("/srv/"));
  assert.ok(stored.includes("tracker.example"));
  assert.equal(center.items()[0].href, "");
  assert.ok(center.words("a").text.includes("D:\\TV\\Show")); // the strip shows it in full now
  center.show({ id: "b", kind: "ok", text: "x", href: "/?check_result=1" });
  assert.equal(center.items()[0].href, "/?check_result=1");
}
verdict.scrubbed = true;

// At most 50 messages of the last 24 hours; the message with a live Undo stays.
{
  const { center, time } = make();
  center.show({ id: "undo", kind: "ok", text: "deleted", undo: "s1", undoUntil: time.now() + 600000 });
  for (let n = 0; n < 60; n += 1) center.show({ id: `m${n}`, kind: "ok", text: "x" });
  const ids = center.items().map((item) => item.id);
  assert.equal(ids.length, 50);
  assert.ok(ids.includes("undo"));
  assert.ok(ids.includes("m59"));
  assert.ok(!ids.includes("m0"));
  time.advance(25 * 3600 * 1000);
  center.show({ id: "late", kind: "ok", text: "x" });
  assert.equal(JSON.stringify(center.items().map((item) => item.id)), JSON.stringify(["late"]));
}
verdict.boundedHistory = true;

// Undo: the newest change's message has it; the server's stamp decides which one still works; a
// page without its message gets one; the time runs out.
{
  const { center, time } = make();
  center.show({ id: "a", kind: "ok", text: "deleted A", undo: "sA", undoUntil: time.now() + 60000 });
  center.show({ id: "b", kind: "ok", text: "deleted B", undo: "sB", undoUntil: time.now() + 60000 });
  assert.equal(center.find("a").undo, "");
  assert.equal(center.liveUndo().id, "b");
  center.setUndo({ stamp: "sB", until: time.now() + 30000, text: "You can undo B" });
  assert.equal(center.find("b").undoUntil, time.now() + 30000);
  center.setUndo({ stamp: "", until: 0, text: "" }); // used or replaced by a change without one
  assert.equal(center.liveUndo(), null);
  center.setUndo({ stamp: "sC", until: time.now() + 30000, text: "You can undo C" });
  const fallback = center.liveUndo();
  assert.equal(fallback.id, "undo-sC");
  assert.equal(fallback.read, true); // not news: no badge
  time.advance(30001);
  assert.equal(center.liveUndo(), null);
  // Clearing the history keeps a live Undo.
  center.setUndo({ stamp: "sD", until: time.now() + 30000, text: "You can undo D" });
  center.show({ id: "c", kind: "err", text: "x" });
  center.clear();
  assert.equal(JSON.stringify(center.items().map((item) => item.id)), JSON.stringify(["undo-sD"]));
}
verdict.undo = true;

console.log(JSON.stringify(verdict));
