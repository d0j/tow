// messages.js on a page: it takes over the server's message strip, says it once, collapses it
// into the header icon, shows the history panel and marks what it shows read, keeps one header
// panel open, merges the header's Undo into the message history - and on a reload brings
// nothing back as news. Prints a JSON verdict (every value true).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/messages.js", import.meta.url), "utf8");
const TEXTS = {
  "js.messages.title": "Recent messages", "js.messages.label_unread": "Recent messages: {count} unread",
  "js.messages.collapse": "Collapse", "js.messages.kind_ok": "Done", "js.messages.kind_info": "Note",
  "js.messages.kind_warn": "Warning", "js.messages.kind_err": "Error", "js.messages.undo": "Undo",
};

const page = ({ storage, served = null, undo = null, address = "http://127.0.0.1/?flash=T1&s=name#row-a" }) => {
  const document = fakeDocument();
  const timers = fakeTimers();
  const strip = served && ["div", {
    class: "flash error", id: "flash", role: "alert", "data-message-id": served.id, "data-kind": served.kind,
    "data-subject": served.subject, "data-text": served.text, "data-at": "1800000000", ...(served.undo ? { "data-undo": served.undo } : {}),
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
  };
  const context = {
    window, document, URL, JSON, Math, Number, String, Array, Object, Map, Set, Boolean,
    Date: class extends Date { static now() { return timers.now(); } },
    location: { href: address },
    history: { state: null, replaceState: (_state, _title, url) => replaced.push(url) },
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
  };
  vm.runInNewContext(source, context);
  const $ = (selector) => document.root.querySelector(selector);
  return { document, timers, window, replaced, $, menu: $("[data-messages]") };
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

// The header icon: shown, red, one unread.
assert.equal(first.menu.hidden, false);
assert.ok(first.menu.classList.contains("err"));
assert.equal(first.$("[data-messages-count]").textContent, "1");
assert.equal(first.$("[data-messages-toggle]").getAttribute("aria-label"), "Recent messages: 1 unread");
verdict.iconColour = true;

// Its time runs out: it collapses into the icon, still unread.
first.timers.advance(30000);
assert.equal(first.$("#msg-host #flash"), null);
assert.equal(first.$("[data-messages-count]").textContent, "1");
verdict.collapsesOnTime = true;

// A reload of the same page: the message is history, not news - no strip, nothing said.
const again = page({ storage, served });
again.timers.advance(5000);
assert.equal(again.$("#msg-host #flash"), null);
assert.equal(again.$("#announce-alert").textContent, "");
assert.equal(again.$("[data-messages-count]").textContent, "1");
verdict.reloadIsNotNews = true;

// × collapses into the history and marks it read: the icon stays, muted, without a count.
const third = page({ storage, served: { id: "m2", kind: "ok", subject: "Show", text: "no changes" } });
assert.ok(third.menu.classList.contains("err")); // m1 is still unread
third.document.dispatch(third.$("#flash .flash-x"), "click");
assert.equal(third.$("#msg-host #flash"), null);
assert.equal(third.$("[data-messages-count]").textContent, "1");
verdict.closeReads = true;

// The panel: one header panel at a time; its entries say kind, subject and outcome; what it
// shows is read, Escape closes it and gives the focus back.
const attention = third.$("[data-attention]");
attention.open = true;
third.menu.open = true;
assert.equal(attention.open, false);
const rows = third.document.root.querySelectorAll("[data-messages-list] li");
assert.equal(rows.length, 2);
assert.equal(rows[0].querySelector(".msg-entry-text").textContent, "Show — no changes");
assert.equal(rows[1].querySelector(".msg-entry-kind").textContent, "Error");
rows[1].rect = { top: 400, bottom: 440, height: 40 }; // below the panel's fold
third.timers.advance(300);
assert.equal(third.$("[data-messages-count]").textContent, "1"); // the unseen one stays unread
rows[1].rect = { top: 200, bottom: 240, height: 40 };
third.document.dispatch(third.$("[data-messages-panel]"), "scroll", { bubbles: false });
third.timers.advance(150);
assert.ok(third.$("[data-messages-count]").hidden);
assert.ok(third.menu.classList.contains("muted"));
third.document.dispatch(third.menu, "keydown", { key: "Escape" });
assert.equal(third.menu.open, false);
assert.equal(third.document.activeElement, third.$("[data-messages-toggle]"));
verdict.panelReadsWhatItShows = true;

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
// entry is; a focused control of the entry keeps the focus when it stays; the changed entry is
// read again only when it is shown.
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
  assert.ok(now.classList.contains("unread"));
  now.rect = { top: 150, bottom: 190, height: 40 }; // in the panel's view
  live.timers.advance(300); // shown in the open panel: read
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

console.log(JSON.stringify(verdict));
