import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const snippet = source.slice(source.indexOf("const whenUnused ="), source.indexOf("const undoForm ="));

function fixture(warning = true) {
  const timers = [];
  const listeners = {};
  const clickers = {};
  const message = { hidden: false, textContent: "Show A: HTTP 429" };
  const attrs = {};
  const button = { hidden: true, setAttribute: (k, v) => { attrs[k] = v; },
    addEventListener: (k, fn) => { clickers[k] = fn; } };
  const close = { addEventListener: (k, fn) => { clickers.close = fn; } };
  const flash = {
    dataset: warning ? { foldTtl: "2" } : {}, isConnected: true, hover: false, focus: false,
    classList: { toggle: (k, v) => { flash.folded = v; } },
    matches: () => flash.hover, contains: () => flash.focus,
    addEventListener: (k, fn) => { listeners[k] = fn; },
    remove: () => { flash.isConnected = false; }, removeAttribute() {},
    getAttribute: () => warning ? "status" : "alert",
    querySelector: (k) => k === ".flash-x" ? close : k === ":scope > span" ? message : warning ? button : null,
  };
  const document = { getElementById: (k) => k === "flash" ? flash : {}, activeElement: {} };
  vm.runInNewContext(snippet, {
    document, window: { setTimeout: (fn, ms) => { timers.push({ fn, ms }); } },
    t: (k) => k, URL, location: { href: "http://127.0.0.1/?flash=synthetic" },
    history: { replaceState() {} },
  });
  const expire = () => timers.find(e => e.ms === 2000)?.fn();
  const leave = (kind) => { flash[kind] = false; listeners[kind === "hover" ? "pointerleave" : "focusout"]();
    timers.filter(e => e.ms === 0).forEach(e => e.fn()); };
  return { flash, message, button, attrs, clickers, timers, expire, leave };
}

const idle = fixture();
assert.equal(idle.button.hidden, false);
idle.expire();
assert.equal(idle.message.hidden, true);
assert.equal(idle.attrs["aria-expanded"], "false");
assert.equal(idle.flash.isConnected, true);
idle.clickers.click();
assert.equal(idle.message.hidden, false);
assert.equal(idle.attrs["aria-expanded"], "true");

for (const kind of ["hover", "focus"]) {
  const used = fixture();
  used.flash[kind] = true;
  used.expire();
  assert.equal(used.message.hidden, false);
  used.leave(kind);
  assert.equal(used.message.hidden, true);
  const manual = fixture();
  manual.flash[kind] = true;
  manual.expire();
  manual.clickers.click(); // fold by hand
  manual.clickers.click(); // reopen by hand
  manual.leave(kind);
  assert.equal(manual.message.hidden, false, "pending automatic fold must respect a manual reopen");
}

const critical = fixture(false);
assert.equal(critical.timers.some(e => e.ms === 2000), false);
assert.equal(critical.message.hidden, false);
idle.clickers.close();
assert.equal(idle.flash.isConnected, false);
console.log(JSON.stringify({ fold: true, reopen: true, focusAndHover: true, manualChoice: true, criticalVisible: true, dismiss: true }));
