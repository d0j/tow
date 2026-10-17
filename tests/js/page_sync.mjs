// pages.js without the page: the components' lifecycle (mounted once per element, cleaned up once
// when taken out, again when it comes back), the keyed list Sites, Settings and History are drawn
// again by (new in the server's order, gone, changed - and kept with a note while the owner is in
// it, a Settings section still taking its free cards), History's merge (the newest window of its
// filters: a line once, at most its limit), and the page area applied from fragments of exactly the
// revision named (a failed request is asked again, a sign-out stops it, what waited is applied once
// free). Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/pages.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, WeakMap, Boolean, Date, RegExp });
const { createLifecycle, reconcile, mergeHistory, createPageSync } = window.TowPages;
const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
const REV = (n) => String(n).repeat(32).slice(0, 32);
const verdict = {};
// Arrays made inside the script's own realm compare by value.
const plain = (value) => JSON.parse(JSON.stringify(value));

// A tiny tree: {cls, children, parent}; matches a class selector (".a, .b").
const node = (cls, ...children) => {
  const element = { cls, children, parent: null, isConnected: true };
  children.forEach((child) => { child.parent = element; });
  element.matches = (selector) => selector.split(",").some((part) => part.trim() === `.${element.cls}`);
  element.querySelectorAll = (selector) => element.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  element.contains = (other) => other === element || element.children.some((child) => child.contains(other));
  return element;
};

// Mounted once per element, cleaned up once, mounted again when it comes back; an element taken out
// by another script is cleaned up at the next mount.
{
  const form = node("form");
  const other = node("form");
  const section = node("section", form);
  const page = node("page", section, other);
  const log = [];
  const life = createLifecycle({ root: () => page });
  life.register("dirty", ".form", (element) => {
    log.push(`mount ${element === form ? "form" : "other"}`);
    return () => log.push(`cleanup ${element === form ? "form" : "other"}`);
  });
  assert.deepEqual(plain(log), ["mount form", "mount other"]);
  assert.equal(life.mount(page), 0); // nothing new: nothing mounted twice
  assert.equal(life.mount(section), 0);
  assert.equal(life.unmount(section), 1);
  assert.equal(life.unmount(section), 0); // a cleanup runs once
  assert.equal(life.mounted(form, "dirty"), false);
  assert.equal(life.mount(section), 1); // back in: mounted again
  other.isConnected = false; // taken out by another script
  page.children.splice(page.children.indexOf(other), 1);
  life.mount(page);
  assert.deepEqual(plain(log), ["mount form", "mount other", "cleanup form", "mount form", "cleanup other"]);
  // A mount that throws leaves the element as drawn and the others mounted.
  const broken = createLifecycle({ root: () => page });
  broken.register("broken", ".form", () => { throw new Error("no"); });
  assert.equal(broken.size(), 1); // the form in the page, still known as mounted
  verdict.lifecycleIdempotent = true;
}

// The keyed list: a duplicate goes, a fragment the server no longer has goes, a new one comes after
// the one before it in the server's order, a changed one is drawn again, an unchanged one stays.
const harness = (keys, hashes = {}) => {
  const items = keys.map((key) => ({ key, hash: hashes[key] ?? `h-${key}`, note: "" }));
  const ops = [];
  const adapter = {
    replace: (item, fragment) => { ops.push(`replace ${fragment.id}`); item.hash = fragment.hash; },
    insert: (fragment, previous) => {
      ops.push(`insert ${fragment.id} after ${previous}`);
      const at = previous === null ? 0 : items.findIndex((item) => item.key === previous) + 1;
      items.splice(at, 0, { key: fragment.id, hash: fragment.hash, note: "" });
    },
    remove: (item) => { ops.push(`remove ${item.key}`); items.splice(items.indexOf(item), 1); },
    note: (item, kind) => { item.note = kind; },
    adopt: (item, fragment) => { ops.push(`adopt ${fragment.id}`); item.hash = fragment.hash; },
  };
  return { items, ops, adapter, keys: () => items.map((item) => item.key) };
};
const frag = (id, hash = `h-${id}`) => ({ id, hash, html: `<div id="${id}"></div>` });
{
  const list = harness(["head", "a", "b", "b", "gone", "empty"]);
  const result = reconcile({
    current: [...list.items],
    wanted: [frag("head"), frag("a"), frag("new"), frag("b", "h-b2"), frag("empty")],
    ...list.adapter,
  });
  assert.deepEqual(plain(list.keys()), ["head", "a", "new", "b", "empty"]);
  assert.deepEqual(plain(list.ops), ["remove b", "remove gone", "insert new after a", "replace b"]);
  assert.deepEqual(plain(result.placed), ["new", "b"]);
  assert.deepEqual(plain(result.removed), ["b", "gone"]);
  verdict.keyedListInsertRemoveReplace = true;
}

// Protected: a section the owner works in keeps what it shows, says it changed and takes only its
// free cards (``partial``); a row it no longer has stays, said removed. Free again, the next pass
// draws it.
{
  const list = harness(["acc-clients", "acc-bots", "site-row-x"]);
  let busy = new Set(["acc-bots", "site-row-x"]);
  const partial = [];
  const pass = () => reconcile({
    current: [...list.items],
    wanted: [frag("acc-clients", "h2"), frag("acc-bots", "h3")],
    isProtected: (item) => busy.has(item.key),
    partial: (item, fragment) => partial.push(`${item.key}:${fragment.hash}`),
    ...list.adapter,
  });
  const first = pass();
  assert.deepEqual(plain(first.waiting), ["acc-bots"]);
  assert.deepEqual(plain(first.kept), ["site-row-x"]);
  assert.deepEqual(plain(partial), ["acc-bots:h3"]);
  assert.equal(list.items.find((item) => item.key === "acc-bots").note, "changed");
  assert.equal(list.items.find((item) => item.key === "site-row-x").note, "removed");
  assert.equal(list.items.find((item) => item.key === "acc-bots").hash, "h-acc-bots"); // not drawn again
  busy = new Set();
  const second = pass();
  assert.deepEqual(plain(second.waiting), []);
  assert.deepEqual(plain(list.keys()), ["acc-clients", "acc-bots"]);
  assert.equal(list.items.find((item) => item.key === "acc-bots").hash, "h3");
  verdict.protectedSectionsWaitWithNote = true;
}

// A line the page drew before its digest was known takes the server's digest without being drawn
// again (its open details stay as they are).
{
  const list = harness(["e1"], { e1: "" });
  reconcile({ current: [...list.items], wanted: [frag("e1", "x")], ...list.adapter });
  assert.deepEqual(plain(list.ops), ["adopt e1"]);
  verdict.ownLinesAdopted = true;
}

// History: the newest window of its filters merged in - the new lines first, in the server's
// order, a line once, never more than the limit; a line under the owner's hands that fell out of
// the window stays.
{
  const merged = mergeHistory(["e3", "e2", "e2", "e1"], [frag("e5"), frag("e4"), frag("e3"), frag("e2")], 4);
  assert.deepEqual(plain(merged), ["e5", "e4", "e3", "e2"]);
  const bounded = mergeHistory(["e2", "e1"], [frag("e4"), frag("e3"), frag("e2"), frag("e1")], 3);
  assert.deepEqual(plain(bounded), ["e4", "e3", "e2"]);
  const held = mergeHistory(["e2", "e1"], [frag("e3"), frag("e2")], 2, (key) => key === "e1");
  assert.deepEqual(plain(held), ["e3", "e2", "e1"]);
  // Another filter's window (the server's answer for this page's filters) replaces what is not in it.
  assert.deepEqual(plain(mergeHistory(["e3", "e2"], [frag("e9")], 300)), ["e9"]);
  verdict.historyMergedOncePerEventWithinBounds = true;
}

// The page area: fragments of the named revision, applied whole and acknowledged; a failed request
// is not (the next poll brings the area again); a newer revision is applied whole and asked about
// again; a sign-out stops; an older answer arriving after a newer one is dropped; what waited for
// the owner is applied again once free.
{
  const acks = [];
  const applied = [];
  let signed = 0;
  let retried = 0;
  let answers = [];
  let waiting = ["acc-bots"];
  const sync = createPageSync({
    fetchFragments: async (rev) => {
      const next = answers.shift();
      if (typeof next === "function") return next(rev);
      if (next instanceof Error) throw next;
      return next;
    },
    apply: (body) => { applied.push(body.rev); return { waiting: [...waiting], kept: [] }; },
    ack: (rev) => acks.push(rev),
    signedOut: () => { signed += 1; },
    retry: () => { retried += 1; },
  });
  answers = [new Error("offline")];
  assert.equal(await sync.page({ rev: REV(1) }), false);
  answers = [{ status: 503, body: null }];
  assert.equal(await sync.page({ rev: REV(1) }), false);
  assert.deepEqual(plain(acks), []);
  answers = [{ status: 200, body: { ok: true, rev: REV(1), fragments: [] } }];
  assert.equal(await sync.page({ rev: REV(1) }), true);
  answers = [{ status: 200, body: { ok: true, rev: REV(2), stale: true, fragments: [] } }];
  await sync.page({ rev: REV(1) });
  assert.deepEqual(plain(acks), [REV(1), REV(2)]);
  assert.equal(retried, 1);
  // Two answers in flight: only the later question's is used.
  let release = null;
  answers = [() => new Promise((resolve) => { release = () => resolve({ status: 200, body: { ok: true, rev: REV(3) } }); }), { status: 200, body: { ok: true, rev: REV(4) } }];
  const older = sync.page({ rev: REV(3) });
  await flush();
  await sync.page({ rev: REV(4) });
  release();
  assert.equal(await older, false);
  assert.deepEqual(plain(acks.slice(-1)), [REV(4)]);
  // What waited: applied again by settle, until nothing waits.
  const before = applied.length;
  assert.equal(sync.settle(), true);
  waiting = [];
  assert.equal(sync.settle(), true);
  assert.equal(sync.settle(), false);
  assert.equal(applied.length, before + 2);
  answers = [{ status: 401, body: null }];
  assert.equal(await sync.page({ rev: REV(5) }), false);
  assert.equal(signed, 1);
  assert.equal(await sync.page({ rev: "not-a-revision" }), false);
  verdict.pageAreaAppliedFromItsRevision = true;
}

// The page area's first request fails (offline, an error, too slow): ``settle`` - after every
// successful poll, also a 304 that brings the area no more - asks for the same revision again, 5,
// 10, 20 s… after the failure, never sooner, and applies it once it comes.
{
  let clock = 0;
  const acks = [];
  const asked = [];
  let answers = [];
  const sync = createPageSync({
    fetchFragments: async (rev) => {
      asked.push([clock, rev]);
      const next = answers.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    apply: () => ({ waiting: [], kept: [] }),
    ack: (rev) => acks.push(rev),
    now: () => clock,
  });
  answers = [new Error("offline")];
  assert.equal(await sync.page({ rev: REV(1) }), false);
  sync.settle(); // too soon
  await flush();
  assert.equal(asked.length, 1);
  clock = 5000;
  answers = [{ status: 500, body: null }];
  sync.settle();
  await flush();
  assert.equal(asked.length, 2);
  clock = 14000;
  sync.settle(); // the second failure waits 10 s
  await flush();
  assert.equal(asked.length, 2);
  clock = 15000;
  answers = [{ status: 200, body: { ok: true, rev: REV(1), fragments: [] } }];
  sync.settle();
  await flush();
  assert.deepEqual(plain(asked.map(([, rev]) => rev)), [REV(1), REV(1), REV(1)]);
  assert.deepEqual(plain(acks), [REV(1)]);
  clock = 100000;
  sync.settle(); // applied: nothing asked again
  await flush();
  assert.equal(asked.length, 3);
  verdict.failedPageAskedAgainBySettle = true;
}

console.log(JSON.stringify(verdict));
