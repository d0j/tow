// messages.js on a page: it takes over the server's message strip, says it once, collapses it
// into the header panel's "This tab", shows the install's shared list from the server (badge,
// colour, read on opening, clear through the displayed cursor), keeps one header panel open,
// merges the header's Undo into the panel - and on a reload brings nothing back as news; the
// per-tab history of earlier versions goes with one notice. Prints a JSON verdict (every value
// true).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/messages.js", import.meta.url), "utf8");
const TEXTS = {
  "js.messages.title": "Recent messages", "js.messages.label_unread": "Recent messages: {count} unread",
  "js.messages.collapse": "Collapse", "js.messages.kind_ok": "Done", "js.messages.kind_info": "Note",
  "js.messages.kind_warn": "Warning", "js.messages.kind_err": "Error", "js.messages.undo": "Undo",
  "js.messages.legacy_retired": "Recent messages are now the same on every device.", "js.messages.this_tab": "This tab",
};

// The server's side: GET /messages.json (ETag, 304) and POST /messages/read|clear (the cursor).
export const fakeServer = (items = []) => {
  const server = { items: items.map((item) => ({ read: false, ...item })), requests: [], fail: null, revision: 1 };
  const feed = () => {
    const unread = server.items.filter((item) => !item.read);
    const rank = { info: 0, ok: 1, warn: 2, err: 3 };
    return {
      items: server.items.map((item) => ({ ...item })), unread: unread.length,
      worst: unread.reduce((worst, item) => (rank[item.kind] > (rank[worst] ?? -1) ? item.kind : worst), ""),
      cursor: server.items[0]?.cursor || "", revision: `r${server.revision}`,
    };
  };
  const answer = (status, data, etag = "", retryAfter = null) => Promise.resolve({
    status, ok: status >= 200 && status < 300,
    headers: { get: (name) => ({ etag, "retry-after": retryAfter })[name.toLowerCase()] ?? null },
    json: () => Promise.resolve(data),
  });
  server.change = (mutate) => { mutate(server.items); server.revision += 1; };
  server.fetch = (url, options = {}) => {
    const body = options.body ? Object.fromEntries(new URLSearchParams(String(options.body))) : null;
    server.requests.push({ url, method: options.method || "GET", body, headers: options.headers || {} });
    if (server.offline) return Promise.reject(new TypeError("offline"));
    if (server.fail) return answer(server.fail.status, server.fail.data || {}, "", server.fail.retryAfter ?? null);
    if (url === "/messages.json") {
      const etag = `"r${server.revision}"`;
      if (options.headers?.["If-None-Match"] === etag) return answer(304, null, etag);
      return answer(200, feed(), etag);
    }
    const index = server.items.findIndex((item) => item.cursor === body?.cursor);
    if (index < 0) return answer(409, { ok: false, error: "changed" });
    server.change((list) => {
      list.slice(index).forEach((item) => { item.read = true; });
      if (url === "/messages/clear") list.splice(index);
    });
    return answer(200, { ok: true, ...feed() });
  };
  return server;
};
const settle = async () => {
  for (let n = 0; n < 6; n += 1) await new Promise((resolve) => setImmediate(resolve));
};

const page = ({ storage, served = null, undo = null, address = "http://127.0.0.1/?flash=T1&s=name#row-a", server = fakeServer(), extra = {} }) => {
  const document = fakeDocument();
  const timers = fakeTimers();
  const strip = served && ["div", {
    class: "flash error", id: "flash", role: "alert", "data-message-id": served.id, "data-kind": served.kind,
    "data-subject": served.subject, "data-text": served.text, "data-at": "1800000000", ...(served.undo ? { "data-undo": served.undo } : {}),
    ...(served.named ? { "data-named": "1" } : {}),
  }, ["span", { class: "msg-text" }, `${served.subject} — ${served.text}`]];
  const header = build(document, ["header", { class: "app" },
    ["details", { class: "attention-menu", id: "current-problems", "data-attention": "" }, ["summary", {}, "!"]],
    ["details", { class: "msg-menu muted", id: "recent-messages", "data-messages": "", hidden: "" },
      ["summary", { class: "ico", "data-messages-toggle": "" }, ["span", { class: "msg-count", "data-messages-count": "", hidden: "" }]],
      ["div", { class: "msg-panel", "data-messages-panel": "" },
        ["button", { "data-messages-read": "" }, "Mark all read"], ["button", { "data-messages-clear": "" }, "Clear"],
        ["p", { "data-messages-empty": "" }, "No messages"], ["ol", { "data-messages-list": "" }]]],
    ["button", { id: "log-open" }, "log"],
    ...(undo ? [["form", { id: "undo-form", method: "post", action: "/undo" }, ["button", {}, "Undo"]]] : []),
  ]);
  const main = build(document, ["main", { id: "main" },
    ["div", { class: "msg-host", id: "msg-host", "data-namespace": "abc", "data-subject-format": "{subject} — {result}",
      "data-undo-stamp": undo?.stamp || "", "data-undo-left": String(undo?.left || 0),
      ...(undo ? { "data-undo-text": "You can undo deleting X" } : {}) }, ...(strip ? [strip] : [])],
    ["div", { id: "announce", role: "status" }], ["div", { id: "announce-alert", role: "alert" }]]);
  const texts = build(document, ["script", { id: "tow-i18n" }, JSON.stringify(TEXTS)]);
  document.body.append(header, main, texts);
  document.root.querySelector("[data-messages-panel]").rect = { top: 100, bottom: 300, height: 200 };
  const replaced = [];
  const window = {
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval, clearInterval: timers.clearInterval,
    sessionStorage: storage,
    ...extra,
  };
  const context = {
    window, document, URL, JSON, Math, Number, String, Array, Object, Map, Set, Boolean,
    Date: class extends Date { static now() { return timers.now(); } },
    location: { href: address },
    history: { state: null, replaceState: (_state, _title, url) => replaced.push(url) },
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    fetch: (...args) => server.fetch(...args), URLSearchParams, Promise, AbortController,
  };
  vm.runInNewContext(source, context);
  const $ = (selector) => document.root.querySelector(selector);
  return { document, timers, window, replaced, $, menu: $("[data-messages]"), server };
};
const verdict = {};
const storage = memoryStorage();
const served = { id: "m1", kind: "err", subject: "Show", text: "the check failed" };

// The server's strip becomes the script's, said once in the alert region a moment later.
const first = page({ storage, served });
const strip = first.$("#msg-host #flash");
assert.equal(strip.dataset.messageId, "m1");
assert.ok(strip.classList.contains("error"));
assert.equal(strip.querySelector(".msg-text").textContent, "Show — the check failed");
assert.equal(strip.getAttribute("role"), null); // the regions speak, not the strip
assert.equal(first.$("#announce-alert").textContent, "");
first.timers.advance(400);
assert.equal(first.$("#announce-alert").textContent, "Error: Show — the check failed");
assert.equal(first.$("#announce").textContent, "");
assert.equal(first.replaced.at(-1), "/?s=name#row-a"); // the address no longer names the message
verdict.takenOverAndSaidOnce = true;

// The header icon: shown for this tab's message, but it is feedback, not "unread": no count, muted.
assert.equal(first.menu.hidden, false);
assert.ok(first.$("[data-messages-count]").hidden);
assert.ok(first.menu.classList.contains("muted"));
assert.equal(first.$("[data-messages-toggle]").getAttribute("aria-label"), "Recent messages");
verdict.tabFeedbackIsNotUnread = true;

// Its time runs out: it collapses into the panel.
first.timers.advance(30000);
assert.equal(first.$("#msg-host #flash"), null);
assert.equal(first.menu.hidden, false);
verdict.collapsesOnTime = true;

// A reload of the same page: the message is not news - no strip, nothing said.
const again = page({ storage, served });
again.timers.advance(5000);
assert.equal(again.$("#msg-host #flash"), null);
assert.equal(again.$("#announce-alert").textContent, "");
verdict.reloadIsNotNews = true;

// × collapses it into the panel.
const third = page({ storage, served: { id: "m2", kind: "ok", subject: "Show", text: "no changes" } });
third.document.dispatch(third.$("#flash .flash-x"), "click");
assert.equal(third.$("#msg-host #flash"), null);
assert.ok(third.$("[data-messages-count]").hidden);
verdict.closeCollapses = true;

// One header panel at a time; this tab's entries under their heading; Escape closes the panel
// and gives the focus back.
const attention = third.$("[data-attention]");
attention.open = true;
// The entry's time is app.js's (towTime: the one time rule, in TOW's zone).
third.window.towTime = { element: (at) => Object.assign(third.document.createElement("time"), { textContent: `rule:${at}` }) };
third.menu.open = true;
assert.equal(attention.open, false);
const rows = third.document.root.querySelectorAll("[data-messages-list] li");
assert.equal(rows[0].dataset.section, "tab");
assert.equal(rows.length, 2); // a reload's message lived in memory only: the old page's are gone
assert.equal(rows[1].querySelector(".msg-entry-text").textContent, "Show — no changes");
assert.equal(rows[1].querySelector("time").textContent, "rule:1800000000000");
assert.equal(rows[1].querySelector(".msg-entry-kind").textContent, "Done");
assert.ok(!rows[1].classList.contains("unread"));
third.document.dispatch(third.menu, "keydown", { key: "Escape" });
assert.equal(third.menu.open, false);
assert.equal(third.document.activeElement, third.$("[data-messages-toggle]"));
verdict.panelShowsThisTab = true;

// The install's list: what the server says, the same on every device. The first load pops
// nothing up (said before this page); the badge counts the shared unread entries, coloured by the
// worst; this tab's feedback is listed after them.
{
  const server = fakeServer([
    { id: "e3", cursor: "c3", kind: "warn", subject: "rutor", text: "Check failed · 2 topics", at: 1800000300, href: "/history?group=errors" },
    { id: "e2", cursor: "c2", kind: "err", subject: "Show B", text: "Check failed", at: 1800000200, href: "/#row-b" },
    { id: "e1", cursor: "c1", kind: "ok", subject: "Show A", text: "New file", at: 1800000100, read: true, href: "/#row-a" },
  ]);
  const live = page({ storage: memoryStorage(), server });
  await settle();
  assert.equal(server.requests[0].url, "/messages.json");
  assert.equal(live.$("#msg-host #flash"), null); // no replayed pop-up
  live.timers.advance(1000);
  assert.equal(live.$("#announce").textContent, "");
  assert.equal(live.$("#announce-alert").textContent, "");
  assert.equal(live.$("[data-messages-count]").textContent, "2");
  assert.ok(live.menu.classList.contains("err"));
  assert.equal(live.$("[data-messages-toggle]").getAttribute("aria-label"), "Recent messages: 2 unread");
  live.window.towMessages.showMessage({ kind: "err", subject: "Show", text: "the save failed" });
  assert.equal(live.$("[data-messages-count]").textContent, "2"); // the strip adds no unread
  verdict.sharedBadge = true;

  // A refresh (sync.js's poll, the tab shown again) asks conditionally and reads nothing.
  await live.window.towMessages.refresh();
  await settle();
  const poll = server.requests.at(-1);
  assert.equal(poll.headers["If-None-Match"], '"r1"');
  assert.equal(server.requests.filter((request) => request.method === "POST").length, 0);
  assert.equal(live.$("[data-messages-count]").textContent, "2");
  live.document.hidden = false;
  live.document.dispatch(live.document, "visibilitychange");
  await settle();
  assert.equal(server.requests.at(-1).url, "/messages.json");
  verdict.pollNeverReads = true;

  // Opening the panel reads what it shows: up to the newest displayed entry, for every device.
  live.menu.open = true;
  await settle();
  const read = server.requests.find((request) => request.url === "/messages/read");
  assert.equal(read.method, "POST");
  assert.equal(read.body.cursor, "c3");
  assert.ok(live.$("[data-messages-count]").hidden);
  const shared = live.document.root.querySelectorAll("[data-messages-list] li[data-message-id]");
  assert.deepEqual(shared.slice(0, 3).map((row) => row.dataset.messageId), ["e3", "e2", "e1"]);
  assert.equal(shared[0].querySelector(".msg-entry-text").textContent, "rutor — Check failed · 2 topics");
  assert.equal(shared[1].querySelector("a").getAttribute("href"), "/#row-b");
  assert.ok(!shared[0].classList.contains("unread"));
  verdict.openingReads = true;

  // Another device's event arrives after the list was shown; Clear goes through the displayed
  // cursor, so the new one stays (and is unread).
  server.change((list) => list.unshift({ id: "e4", cursor: "c4", kind: "ok", subject: "Show C", text: "Added", at: 1800000400, read: false }));
  live.document.dispatch(live.$("[data-messages-clear]"), "click");
  await settle();
  const clear = server.requests.find((request) => request.url === "/messages/clear");
  assert.equal(clear.body.cursor, "c3");
  assert.deepEqual(live.window.towMessages.shared().items.map((item) => item.id), ["e4"]);
  assert.equal(live.$("[data-messages-count]").textContent, "1");
  verdict.clearKeepsANewerEvent = true;

  // A cursor another device made stale: the list is fetched again. Markers that cannot be
  // saved: said, with the server's words.
  server.change((list) => list.splice(0));
  live.document.dispatch(live.$("[data-messages-read]"), "click");
  await settle();
  assert.equal(server.requests.at(-1).url, "/messages.json");
  server.change((list) => list.push({ id: "e5", cursor: "c5", kind: "warn", text: "x", at: 1800000500 }));
  await live.window.towMessages.refresh();
  await settle();
  server.fail = { status: 503, data: { ok: false, error: "Could not save that the messages are read." } };
  live.document.dispatch(live.$("[data-messages-read]"), "click");
  await settle();
  assert.equal(live.$("#flash .msg-text").textContent, "Could not save that the messages are read.");
  assert.ok(live.$("#flash").classList.contains("error"));
  server.fail = null;
  verdict.staleAndFailedMarks = true;

  // Signed out elsewhere (401): nothing of the list stays on the page.
  server.fail = { status: 401, data: { error: "sign in" } };
  await live.window.towMessages.refresh();
  await settle();
  assert.equal(live.window.towMessages.shared().items.length, 0);
  assert.ok(live.$("[data-messages-count]").hidden);
  server.fail = null;
}
verdict.signedOutDropsTheList = true;

// refresh() says whether the page has the install's list now (sync.js applies the messages area only
// then): true for a list taken, or confirmed unchanged by a 304; false for a failed or lost request;
// a refresh asked while one is on its way answers for a request sent after it.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "ok", subject: "Show A", text: "New file", at: 1800000100 }]);
  const live = page({ storage: memoryStorage(), server });
  await settle();
  const refresh = () => live.window.towMessages.refresh();
  assert.equal(await refresh(), true); // 304: what it has is the list
  server.fail = { status: 503, data: {} };
  assert.equal(await refresh(), false);
  server.fail = null;
  server.offline = true;
  assert.equal(await refresh(), false);
  server.offline = false;
  const count = server.requests.length;
  const first = refresh();
  for (let i = 0; i < 5; i += 1) await Promise.resolve(); // on its way
  server.change((list) => list.unshift({ id: "e2", cursor: "c2", kind: "warn", subject: "Show B", text: "Check failed", at: 1800000200, read: false }));
  const second = refresh();
  assert.equal(await second, true);
  assert.equal(server.requests.length, count + 2); // the second answer is of a request sent after the change
  assert.deepEqual(live.window.towMessages.shared().items.map((item) => item.id), ["e2", "e1"]);
  assert.equal(await first, true);
  // Signed out meanwhile: not the list of this session.
  const late = refresh();
  live.window.towMessages.signOut();
  assert.equal(await late, false);
}
verdict.refreshSaysWhetherTheListIsCurrent = true;

// A request for the list that never answers, or whose body never ends: given up (aborted) after
// 10 s, refresh() answers false (sync.js asks again later), and the next refresh is a request of
// its own - nothing waits behind the lost one. A body that ends after that is not used.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "ok", subject: "Show A", text: "New file", at: 1800000100 }]);
  const plain = server.fetch;
  let hang = "";
  let late = null;
  const signals = [];
  server.fetch = (url, options = {}) => {
    if (url !== "/messages.json" || !hang) return plain(url, options);
    signals.push(options.signal);
    const asked = plain(url, options);
    if (hang === "fetch") return new Promise(() => {});
    return asked.then((answer) => ({
      ...answer,
      json: () => new Promise((resolve) => { late = () => resolve({ items: [{ id: "stale", kind: "err", text: "old" }], unread: 1, worst: "err", cursor: "x" }); }),
    }));
  };
  const live = page({ storage: memoryStorage(), server });
  await settle();
  for (const kind of ["fetch", "body"]) {
    hang = kind;
    server.change(() => {}); // a new list: a 200 with a body
    let answered = null;
    live.window.towMessages.refresh().then((value) => { answered = value; });
    await settle();
    live.timers.advance(9900);
    await settle();
    assert.equal(answered, null, kind);
    live.timers.advance(200);
    await settle();
    assert.equal(answered, false, kind);
    assert.equal(signals.at(-1)?.aborted, true, kind);
    hang = "";
    const count = server.requests.length;
    assert.equal(await live.window.towMessages.refresh(), true, kind);
    assert.equal(server.requests.length, count + 1, kind);
  }
  late();
  await settle();
  assert.deepEqual(live.window.towMessages.shared().items.map((item) => item.id), ["e1"]);
}
verdict.hungListGivenUp = true;

// One request for the list when the tab is shown or a poll names it: refreshes asked in the same
// moment (the undo and the messages areas of one answer) are one request, and under sync.js (its
// poll asks when shown, and names the messages area when the list changed) the tab's own showing
// asks nothing more.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "ok", subject: "Show A", text: "New file", at: 1800000100 }]);
  const live = page({ storage: memoryStorage(), server, extra: { towSync: { client: { stopped: () => false } } } });
  await settle();
  const count = () => server.requests.filter((request) => request.url === "/messages.json").length;
  const before = count();
  const both = [live.window.towMessages.refresh(), live.window.towMessages.refresh(), live.window.towMessages.refresh()];
  assert.deepEqual(await Promise.all(both), [true, true, true]);
  assert.equal(count(), before + 1);
  live.document.hidden = true;
  live.document.dispatch(live.document, "visibilitychange");
  live.document.hidden = false;
  live.document.dispatch(live.document, "visibilitychange");
  await settle();
  assert.equal(count(), before + 1);
}
verdict.oneRequestForTheListAtOnce = true;

// Too many requests (429, Retry-After): the list is not the server's now (false: sync.js keeps the
// messages area unapplied), nothing is asked before Retry-After, then the page asks once by itself.
// A read or clear answered 429 is sent once more after Retry-After; only a second refusal is said.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  const live = page({ storage: memoryStorage(), server });
  await settle();
  const gets = () => server.requests.filter((request) => request.url === "/messages.json").length;
  server.change((list) => list.unshift({ id: "e2", cursor: "c2", kind: "ok", subject: "Show B", text: "New file", at: 1800000200, read: false }));
  server.fail = { status: 429, data: { ok: false, error: "rate_limited" }, retryAfter: "3" };
  const before = gets();
  assert.equal(await live.window.towMessages.refresh(), false);
  assert.equal(gets(), before + 1);
  assert.equal(await live.window.towMessages.refresh(), false); // within Retry-After: not asked
  assert.equal(gets(), before + 1);
  server.fail = null;
  live.timers.advance(2900);
  await settle();
  assert.equal(gets(), before + 1);
  live.timers.advance(200);
  await settle();
  assert.equal(gets(), before + 2); // asked once by itself
  assert.deepEqual(live.window.towMessages.shared().items.map((item) => item.id), ["e2", "e1"]);
  assert.equal(await live.window.towMessages.refresh(), true);
  // Read: refused once (429), sent again after Retry-After, said nothing.
  server.fail = { status: 429, data: { ok: false, error: "Too many requests." }, retryAfter: "2" };
  live.menu.open = true;
  await settle();
  server.fail = null;
  assert.equal(server.requests.filter((request) => request.url === "/messages/read").length, 1);
  live.timers.advance(2100);
  await settle();
  assert.equal(server.requests.filter((request) => request.url === "/messages/read").length, 2);
  assert.equal(live.$("#msg-host #flash"), null);
  assert.equal(live.window.towMessages.shared().unread, 0);
}
verdict.rateLimitedWaitsForRetryAfter = true;

// A read or clear refused (429) is sent again with the cursor of the moment it was asked: an entry
// that arrived during Retry-After (a refresh showed it meanwhile) is neither cleared nor read by it.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  const live = page({ storage: memoryStorage(), server });
  await settle();
  const posts = (url) => server.requests.filter((request) => request.url === url);
  const arrives = (id) => server.change((list) => list.unshift({ id, cursor: `c-${id}`, kind: "ok", subject: "Show", text: "New file", at: 1800000200, read: false }));
  for (const [button, url] of [["[data-messages-clear]", "/messages/clear"], ["[data-messages-read]", "/messages/read"]]) {
    const asked = live.window.towMessages.shared().cursor;
    server.fail = { status: 429, data: { ok: false, error: "Too many requests." }, retryAfter: "3" };
    live.document.dispatch(live.$(button), "click");
    await settle();
    server.fail = null;
    assert.equal(posts(url).at(-1).body.cursor, asked);
    arrives(`${url.slice(10)}-new`);
    assert.equal(await live.window.towMessages.refresh(), true);
    assert.notEqual(live.window.towMessages.shared().cursor, asked);
    live.timers.advance(3100);
    await settle();
    assert.equal(posts(url).length, 2, url);
    assert.equal(posts(url).at(-1).body.cursor, asked, url);
  }
  // What arrived after the clear stays; what arrived after the read (not shown at its click) is unread.
  assert.deepEqual(live.window.towMessages.shared().items.map((item) => [item.id, item.read]), [["read-new", false], ["clear-new", true]]);
}
verdict.retriedMarkKeepsItsCursor = true;

// A read or clear whose POST never answers, or whose body never ends, is given up (aborted) after
// 10 s like the list's GET: the owner is told once that it was not saved, an answer arriving after
// that is not taken (the next refresh shows what the server kept), and a later read or clear is a
// request of its own. Signing out meanwhile ends it silently.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  const plain = server.fetch;
  let hang = "";
  let late = null;
  const signals = [];
  server.fetch = (url, options = {}) => {
    if (url === "/messages.json" || !hang) return plain(url, options);
    signals.push(options.signal);
    if (hang === "fetch") {
      server.requests.push({ url, method: options.method || "GET", body: null, headers: options.headers || {} });
      return new Promise(() => {});
    }
    return plain(url, options).then((answer) => ({
      ...answer,
      json: () => new Promise((resolve) => { late = () => resolve({ ok: true, items: [{ id: "stale", kind: "err", text: "old" }], unread: 1, worst: "err", cursor: "x" }); }),
    }));
  };
  const live = page({ storage: memoryStorage(), server });
  await settle();
  const center = live.window.towMessages.center;
  const shown = [];
  const show = center.show.bind(center);
  center.show = (record, ...rest) => { shown.push(record); return show(record, ...rest); };
  const posts = (url) => server.requests.filter((request) => request.url === url).length;
  for (const [kind, button, url] of [["fetch", "[data-messages-read]", "/messages/read"], ["body", "[data-messages-clear]", "/messages/clear"]]) {
    hang = kind;
    shown.length = 0;
    live.document.dispatch(live.$(button), "click");
    await settle();
    live.timers.advance(9900);
    await settle();
    assert.equal(shown.length, 0, kind);
    live.timers.advance(200);
    await settle();
    assert.equal(signals.at(-1)?.aborted, true, kind);
    assert.deepEqual(shown.map((record) => [record.kind, record.text]), [["err", "js.messages.not_saved"]], kind);
    live.timers.advance(60000);
    await settle();
    assert.equal(shown.length, 1, kind); // said once, never again by itself
    hang = "";
    const before = posts(url);
    live.document.dispatch(live.$(button), "click");
    await settle();
    assert.equal(posts(url), before + 1, kind);
  }
  late();
  await settle();
  assert.ok(!live.window.towMessages.shared().items.some((item) => item.id === "stale"));
  assert.equal(shown.length, 1);
  // Signed out while a read is on its way: nothing is said, then or after the 10 s.
  server.change((list) => list.unshift({ id: "e3", cursor: "c3", kind: "ok", subject: "Show B", text: "New file", at: 1800000300, read: false }));
  assert.equal(await live.window.towMessages.refresh(), true);
  hang = "fetch";
  shown.length = 0;
  const sent = signals.length;
  live.document.dispatch(live.$("[data-messages-read]"), "click");
  await settle();
  assert.equal(signals.length, sent + 1);
  live.window.towMessages.signOut();
  await settle();
  live.timers.advance(20000);
  await settle();
  assert.equal(signals.at(-1)?.aborted, true);
  assert.deepEqual(shown, []);
}
verdict.hungMarkGivenUp = true;

// A hidden tab sends no automatic retry after 429 (as sync.js's poll asks nothing while hidden): the
// list's retry and a refused read wait until the tab is shown again, and the list's retry goes with
// that moment's own refresh - one request - under sync.js and without it.
for (const withSync of [true, false]) {
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  const extra = withSync ? { towSync: { client: { stopped: () => false } } } : {};
  const live = page({ storage: memoryStorage(), server, extra });
  await settle();
  const count = (url) => server.requests.filter((request) => request.url === url).length;
  server.change((list) => list.unshift({ id: "e2", cursor: "c2", kind: "ok", subject: "Show B", text: "New file", at: 1800000200, read: false }));
  server.fail = { status: 429, data: { ok: false, error: "Too many requests." }, retryAfter: "3" };
  assert.equal(await live.window.towMessages.refresh(), false);
  live.document.dispatch(live.$("[data-messages-read]"), "click");
  await settle();
  server.fail = null;
  live.document.hidden = true;
  live.document.dispatch(live.document, "visibilitychange");
  const gets = count("/messages.json");
  const reads = count("/messages/read");
  live.timers.advance(60000);
  await settle();
  assert.equal(count("/messages.json"), gets, `hidden, sync ${withSync}`);
  assert.equal(count("/messages/read"), reads, `hidden, sync ${withSync}`);
  live.document.hidden = false;
  live.document.dispatch(live.document, "visibilitychange");
  await settle();
  assert.equal(count("/messages.json"), gets + 1, `shown, sync ${withSync}`);
  assert.equal(count("/messages/read"), reads + 1, `shown, sync ${withSync}`);
  assert.equal(server.requests.filter((request) => request.url === "/messages/read").at(-1).body.cursor, "c1");
  live.timers.advance(60000);
  await settle();
  assert.equal(count("/messages.json"), gets + 1); // nothing owed any more
  assert.equal(count("/messages/read"), reads + 1);
}
verdict.hiddenTabRetriesWhenShown = true;

// The list and its markers name this tab to the server (X-TOW-Tab) with the name sync.js made for
// its poll, so a tab's requests count in that tab's rate bucket; without sync.js none is named.
{
  const TAB = "0123456789abcdef01234567";
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  const live = page({ storage: memoryStorage(), server, extra: { towSync: { tab: TAB, client: { stopped: () => false } } } });
  await settle();
  live.document.dispatch(live.$("[data-messages-read]"), "click");
  await settle();
  live.document.dispatch(live.$("[data-messages-clear]"), "click");
  await settle();
  assert.deepEqual(server.requests.map((request) => request.url), ["/messages.json", "/messages/read", "/messages/clear"]);
  assert.ok(server.requests.every((request) => request.headers["X-TOW-Tab"] === TAB), JSON.stringify(server.requests));
  const alone = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show A", text: "Check failed", at: 1800000100 }]);
  page({ storage: memoryStorage(), server: alone });
  await settle();
  assert.equal(alone.requests[0].headers["X-TOW-Tab"], undefined);
}
verdict.requestsNameTheTab = true;

// The per-tab history of 1.32 and older: removed with one notice, never sent anywhere; a reload
// says nothing again.
{
  const store = memoryStorage();
  store.setItem("tow.messages.abc", JSON.stringify({ v: 1, items: [{ id: "old", kind: "err", text: "an old failure at D:\\TV" }] }));
  const server = fakeServer();
  const legacy = page({ storage: store, server });
  await settle();
  assert.equal(legacy.$("#flash .msg-text").textContent, "Recent messages are now the same on every device.");
  assert.equal(store.getItem("tow.messages.abc"), null);
  assert.ok(server.requests.every((request) => !JSON.stringify(request).includes("old failure")));
  const reloaded = page({ storage: store, server });
  await settle();
  assert.equal(reloaded.$("#msg-host #flash"), null);
}
verdict.legacyRetiredOnce = true;

// The header's Undo goes into the history (no-script fallback hidden); its entry counts down.
const undone = page({ storage: memoryStorage(), undo: { stamp: "s1", left: 60 } });
assert.equal(undone.$("header #undo-form").hidden, true);
assert.equal(undone.menu.hidden, false);
assert.ok(undone.$("[data-messages-count]").hidden); // not news
undone.menu.open = true;
const form = undone.$('[data-messages-list] form[action="/undo"]');
assert.equal(form.querySelector('input[name="stamp"]').value, "s1");
assert.equal(form.querySelector("[data-undo-left]").textContent, "1:00");
undone.timers.advance(1000);
assert.equal(undone.$("[data-messages-list] [data-undo-left]").textContent, "0:59");
undone.timers.advance(60000);
assert.equal(undone.$('[data-messages-list] form[action="/undo"]'), null);
verdict.undoInHistory = true;

// Storage that throws: the page still shows its message (memory only).
const blocked = {
  get length() { throw new Error("SecurityError"); }, key() { throw new Error("SecurityError"); },
  getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("SecurityError"); },
  removeItem() { throw new Error("SecurityError"); },
};
const offline = page({ storage: blocked, served: { id: "m9", kind: "warn", subject: "", text: "partly saved" } });
assert.equal(offline.$("#flash .msg-text").textContent, "Partly saved");
offline.timers.advance(400);
assert.equal(offline.$("#announce").textContent, "Warning: Partly saved");
verdict.storageBlocked = true;

// Words that start with a name (the server's record says so: data-named, "named") keep it as
// written, in the strip, in what is said and in the history: it showed "QBittorrent …" and
// "Nnmclub: …". Catalog words said on their own still start as a sentence.
{
  const named = page({ storage: memoryStorage(), served: { id: "n1", kind: "warn", subject: "", text: "nnmclub: all mirrors are paused", named: true } });
  assert.equal(named.$("#flash .msg-text").textContent, "nnmclub: all mirrors are paused");
  named.timers.advance(400);
  assert.equal(named.$("#announce").textContent, "Warning: nnmclub: all mirrors are paused");
  named.window.towMessages.showMessage({ kind: "err", text: "qBittorrent does not answer", named: "1" });
  assert.equal(named.$("#flash .msg-text").textContent, "qBittorrent does not answer");
  named.window.towMessages.showMessage({ kind: "info", text: "check sent" });
  assert.equal(named.$("#flash .msg-text").textContent, "Check sent");
  named.menu.open = true;
  const texts = named.document.root.querySelectorAll("[data-messages-list] .msg-entry-text").map((node) => node.textContent);
  assert.deepEqual(texts, ["Check sent", "qBittorrent does not answer", "nnmclub: all mirrors are paused"]);
}
verdict.namesKeepTheirSpelling = true;

// Another script shows a message the same way, and gets the same strip.
offline.window.towMessages.showMessage({ kind: "err", subject: "Show", text: "could not confirm the result" });
assert.equal(offline.$("#flash .msg-text").textContent, "Show — could not confirm the result");
verdict.oneEntryPoint = true;

// A check's progress and then its result patch the strip in place: the same element, its focus
// kept; a control that goes while it has the focus hands it to the strip's x, so the strip's
// time is still held by a focus that is really there.
{
  const live = page({ storage: memoryStorage() });
  const show = (record, options) => live.window.towMessages.showMessage(record, options);
  show({ id: "check-J", kind: "info", subject: "Show", text: "queued" });
  const box = live.$("#flash");
  const close = box.querySelector(".flash-x");
  close.focus();
  live.document.dispatch(close, "focusin");
  show({ id: "check-J", kind: "info", subject: "Show", text: "running since 12:00" }, { replace: true, quiet: true });
  assert.equal(live.$("#flash"), box);
  assert.equal(box.querySelector(".msg-text").textContent, "Show — running since 12:00");
  assert.equal(live.document.activeElement, close);
  show({ id: "check-J", kind: "err", subject: "Show", text: "failed", href: "/?open=a", link: "Open" }, { replace: true });
  assert.equal(live.$("#flash"), box);
  assert.ok(box.classList.contains("error"));
  assert.equal(box.querySelector(".msg-ico").getAttribute("aria-label"), "Error");
  const anchor = box.querySelector("a.msg-link");
  assert.equal(anchor.getAttribute("href"), "/?open=a");
  assert.equal(live.document.activeElement, close);
  anchor.focus();
  show({ id: "check-J", kind: "err", subject: "Show", text: "failed again" }, { replace: true });
  assert.equal(box.querySelector("a.msg-link"), null);
  assert.equal(live.document.activeElement, close); // not lost with the link
  // The focus leaves: the hold ends and the strip collapses after its time.
  live.document.activeElement = live.document.body;
  live.document.dispatch(close, "focusout", { relatedTarget: null });
  live.timers.advance(30000);
  assert.equal(live.$("#flash"), null);
}
verdict.progressPatchesInPlace = true;

// The open panel follows a message updated in place: its words, colour and link change where the
// entry is; a focused control of the entry keeps the focus when it stays. This tab's entries are
// never "unread" (only the install's list is).
{
  const live = page({ storage: memoryStorage() });
  const show = (record, options) => live.window.towMessages.showMessage(record, options);
  show({ id: "check-J", kind: "info", subject: "Show", text: "queued", href: "/history", link: "History" });
  live.menu.open = true;
  live.timers.advance(300);
  const row = live.$('[data-messages-list] li[data-message-id="check-J"]');
  const anchor = row.querySelector("a");
  anchor.focus();
  show({ id: "check-J", kind: "info", subject: "Show", text: "running since 12:00", href: "/history", link: "History" }, { replace: true, quiet: true });
  assert.equal(live.$('[data-messages-list] li[data-message-id="check-J"]'), row);
  assert.equal(row.querySelector(".msg-entry-text").textContent, "Show — running since 12:00");
  assert.equal(live.document.activeElement, anchor);
  show({ id: "check-J", kind: "err", subject: "Show", text: "failed", href: "/?credential_topic=a", link: "Sign in" }, { replace: true });
  const now = live.$('[data-messages-list] li[data-message-id="check-J"]');
  assert.ok(now.classList.contains("err"));
  assert.equal(now.querySelector(".msg-entry-kind").textContent, "Error");
  assert.equal(now.querySelector(".msg-entry-text").textContent, "Show — failed");
  assert.equal(now.querySelector("a").getAttribute("href"), "/?credential_topic=a");
  assert.equal(now.querySelector("a").textContent, "Sign in");
  assert.ok(!now.classList.contains("unread"));
}
verdict.panelFollowsUpdates = true;

// A waiting check's message can cancel it (its POST form) until it runs.
{
  const live = page({ storage: memoryStorage() });
  const show = (record, options) => live.window.towMessages.showMessage(record, options);
  show({ id: "check-Q", kind: "info", subject: "Show", text: "queued", cancel: "/check/Q/cancel" });
  const form = live.$('#flash form[action="/check/Q/cancel"]');
  assert.ok(form);
  assert.equal(form.getAttribute("method"), "post");
  assert.equal(form.querySelector("button").textContent, "js.messages.cancel");
  show({ id: "check-Q", kind: "info", subject: "Show", text: "running" }, { replace: true, quiet: true });
  assert.equal(live.$('#flash form[action="/check/Q/cancel"]'), null);
  show({ id: "check-R", kind: "info", text: "queued", cancel: "https://evil.example/x" });
  assert.equal(live.$("#flash form"), null); // only this TOW's own address
}
verdict.cancelFromMessage = true;

// Signing out: this tab's message history goes, of every install and sign-in, and nothing of it
// is shown any more.
{
  const store = memoryStorage();
  store.setItem("tow.messages.other-session", "{}");
  const out = page({ storage: store, served: { id: "m7", kind: "ok", subject: "Show", text: "no changes" } });
  assert.ok(store.data.size >= 1);
  const form = build(out.document, ["form", { method: "post", action: "/logout" }]);
  out.document.body.append(form);
  out.document.dispatch(form, "submit");
  assert.equal([...store.data.keys()].filter((key) => key.startsWith("tow.messages.")).length, 0);
  assert.equal(out.$("#msg-host #flash"), null);
  assert.equal(out.menu.hidden, true);
}
verdict.signOutClears = true;

// A message said a moment after it arrives is not said once the owner has signed out meanwhile.
{
  const out = page({ storage: memoryStorage(), served: { id: "m8", kind: "err", subject: "Show", text: "failed" } });
  const form = build(out.document, ["form", { method: "post", action: "/logout" }]);
  out.document.body.append(form);
  out.document.dispatch(form, "submit");
  out.timers.advance(1000);
  assert.equal(out.$("#announce-alert").textContent, "");
  assert.equal(out.$("#announce").textContent, "");
  assert.equal(out.window.towMessages.session(), 1);
}
verdict.signOutSilencesAnnouncements = true;

// Signing out while the list's body is still being read: the old session's list is not shown when
// it arrives; the words of this tab's messages (kept as said, in memory only) go with the session.
{
  const server = fakeServer([{ id: "e1", cursor: "c1", kind: "warn", subject: "Show", text: "Check failed", at: 1800000100 }]);
  const plain = server.fetch;
  let release = null;
  server.fetch = (url, options = {}) => {
    if (url !== "/messages.json" || release) return plain(url, options);
    return plain(url, options).then((answer) => ({
      ...answer,
      json: () => new Promise((resolve) => { release = () => answer.json().then(resolve); }),
    }));
  };
  const out = page({ storage: memoryStorage(), server });
  await settle();
  assert.ok(release, "the list's body is on its way");
  out.window.towMessages.showMessage({ id: "w1", kind: "err", subject: "Show", text: "could not write D:\\TV\\Show" });
  assert.ok(out.window.towMessages.center.words("w1").text.includes("D:\\TV"));
  const form = build(out.document, ["form", { method: "post", action: "/logout" }]);
  out.document.body.append(form);
  out.document.dispatch(form, "submit");
  release();
  await settle();
  assert.equal(out.window.towMessages.shared().items.length, 0);
  assert.equal(out.window.towMessages.shared().loaded, false);
  assert.equal(out.window.towMessages.center.words("w1").text, "");
}
verdict.signOutDropsLateBodiesAndWords = true;

console.log(JSON.stringify(verdict));
