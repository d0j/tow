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

console.log(JSON.stringify(verdict));
