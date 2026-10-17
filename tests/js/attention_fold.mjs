import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/attention.js", import.meta.url), "utf8");
function fixture({ polling = true } = {}) {
  const listeners = {};
  const document = { activeElement: null, addEventListener: (key, fn) => { listeners[key] = fn; } };
  class Node {
    constructor() { this.dataset = {}; this.children = []; this.attrs = {}; this.events = {}; this.open = false;
      this.hidden = false; this.classList = { toggle: (key, value) => { this.attrs[key] = value; } }; }
    setAttribute(key, value) { this.attrs[key] = value; }
    append(node) { node.parent = this; this.children.push(node); }
    replaceChildren(...nodes) {
      for (const old of this.children) {
        if (old.contains(document.activeElement)) document.activeElement = null;
        old.parent = null;
      }
      this.children = []; nodes.forEach(node => this.append(node));
    }
    get firstElementChild() { return this.children[0]; }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    closest() { return this.dataset.issueId ? this : this.parent?.closest(); }
    focus() { document.activeElement = this; }
    addEventListener(key, fn) { this.events[key] = fn; }
  }
  const menu = new Node(), toggle = new Node(), counter = new Node(), list = new Node(), log = new Node();
  menu.dataset.fingerprint = "old";
  menu.append(toggle); menu.append(counter); menu.append(list);
  menu.querySelector = key => ({ "[data-attention-toggle]": toggle, "[data-attention-count]": counter,
    "[data-attention-list]": list })[key];
  document.querySelector = () => menu;
  document.getElementById = () => log;
  document.createElement = () => new Node();
  document.createElementNS = () => new Node();
  let reader, now = 0, registrations = 0;
  const context = { document, Date: { now: () => now } };
  if (polling) context.healthPoll = { add: value => { reader = value; registrations++; } };
  vm.runInNewContext(source, context);
  listeners.DOMContentLoaded();
  return { menu, toggle, counter, list, log, document, listeners, registrations,
    receive: value => reader.receive(value), advance: value => { now = value; } };
}
const entry = (id = "a") => ({ id, text: "<Show A> — rate limited", severity: "warn", href: "/?open=a#row-a" });
const snapshot = (entries = [entry()], fingerprint = "new") => ({ attention: {
  entries, count: entries.length, label: `Current problems: ${entries.length}`, fingerprint, tone: entries.length ? "warn" : "",
} });
const test = fixture();
assert.equal(test.menu.open, false);
assert.equal(test.registrations, 1);
test.receive(snapshot());
assert.equal(test.menu.hidden, false);
assert.equal(test.menu.open, false);
assert.equal(test.counter.textContent, "1");
assert.equal(test.toggle.attrs["aria-label"], "Current problems: 1");
assert.equal(test.list.firstElementChild.firstElementChild.firstElementChild.textContent, entry().text);
assert.equal(test.list.firstElementChild.firstElementChild.attrs.href, entry().href);
assert.equal(test.list.firstElementChild.firstElementChild.children[1].firstElementChild.attrs.href, "#i-next");
test.menu.open = true;
test.list.firstElementChild.firstElementChild.focus();
test.receive(snapshot([entry(), entry("b")], "two"));
assert.equal(test.document.activeElement, test.list.firstElementChild.firstElementChild);
assert.equal(test.menu.open, true);
test.receive(snapshot([], ""));
assert.equal(test.menu.hidden, true);
assert.equal(test.menu.open, false);
assert.equal(test.document.activeElement, test.log);
test.receive(snapshot());
assert.equal(test.menu.hidden, false);
assert.equal(test.menu.open, false);
for (const invalid of [null, {}, { attention: null }, { attention: { ...snapshot().attention, count: 0 } },
  { attention: { ...snapshot().attention, count: 1.5 } },
  snapshot([null]), snapshot([false]), snapshot([{ ...entry(), href: "//evil.example" }]),
  snapshot([{ ...entry(), href: "/\\evil" }])]) {
  test.receive(invalid);
  assert.equal(test.menu.hidden, false);
  assert.equal(test.counter.textContent, "1");
}
// The next valid answer of the page's poll (sync.js's cadence, not this list's) is taken again.
test.advance(60000); test.receive(snapshot([entry("a"), entry("b")]));
assert.equal(test.counter.textContent, "2");
test.menu.open = true;
test.menu.events.keydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
assert.equal(test.menu.open, false);
assert.equal(test.document.activeElement, test.toggle);
test.menu.open = true; test.listeners.pointerdown({ target: test.toggle });
assert.equal(test.menu.open, true);
test.listeners.pointerdown({ target: {} });
assert.equal(test.menu.open, false);
test.menu.open = true;
test.menu.events.focusout({ relatedTarget: test.log });
assert.equal(test.menu.open, false);
test.receive(snapshot(Array.from({ length: 101 }, (_, n) => entry(String(n))), "many"));
assert.equal(test.counter.textContent, "99+");
assert.equal(test.toggle.attrs["aria-label"], "Current problems: 101");
const native = fixture({ polling: false });
native.menu.open = true;
native.menu.events.keydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
assert.equal(native.menu.open, false);
console.log(JSON.stringify({ closedInitially: true, sharedPoll: true, safeText: true, retainedFocus: true,
  recoveryHides: true, recurrenceClosed: true, invalidPreserves: true, nextAnswerTaken: true,
  escapeAndOutside: true, nativeFallback: true, cappedCount: true }));
