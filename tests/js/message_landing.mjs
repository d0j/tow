// A link's target lands below the sticky header and the message strip (M1 of the 1.32 QA: on a
// phone the strip covered the section a link opened). messages.js measures both on the page's
// root (--msg-top, --msg-h), again when the strip goes; app.css's --land-top adds them up.
// Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/messages.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../../src/tow/static/app.css", import.meta.url), "utf8")
  + readFileSync(new URL("../../src/tow/static/messages.css", import.meta.url), "utf8");
const document = fakeDocument();
const timers = fakeTimers();
const root = {};
document.documentElement.style = { setProperty: (name, value) => { root[name] = value; } };
const header = build(document, ["header", { class: "app" }, ["details", { "data-messages": "", hidden: "" },
  ["summary", { "data-messages-toggle": "" }], ["div", { "data-messages-panel": "" }, ["ol", { "data-messages-list": "" }]]]]);
header.rect = { top: 0, bottom: 72, height: 72 };
const host = build(document, ["div", { class: "msg-host", id: "msg-host", "data-namespace": "abc", "data-subject-format": "{subject} — {result}" },
  ["div", { class: "flash ok", id: "flash", "data-message-id": "m1", "data-kind": "ok", "data-subject": "", "data-text": "Saved", "data-at": "1800000000" },
    ["span", { class: "msg-text" }, "Saved"]]]);
host.rect = { top: 72, bottom: 138, height: 66 };
const main = build(document, ["main", { id: "main" }]);
main.append(host);
document.body.append(header, main, build(document, ["script", { id: "tow-i18n" }, "{}"]));
const window = { setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, setInterval: timers.setInterval,
  clearInterval: timers.clearInterval, sessionStorage: memoryStorage(), addEventListener() {} };
vm.runInNewContext(source, {
  window, document, URL, JSON, Math, Number, String, Array, Object, Map, Set, Boolean,
  Date: class extends Date { static now() { return timers.now(); } },
  location: { href: "http://127.0.0.1/settings?open=intervals#acc-intervals" },
  history: { state: null, replaceState() {} }, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
});
const verdict = {};
assert.equal(root["--msg-top"], "72px");
assert.equal(root["--msg-h"], "66px");
verdict.measured = true;

// The strip goes (its x): nothing to clear but the header.
host.rect = { top: 72, bottom: 72, height: 0 };
document.dispatch(host.querySelector(".flash-x"), "click");
assert.equal(host.querySelector(".flash"), null);
assert.equal(root["--msg-h"], "0px");
verdict.measuredAgain = true;

// Every landing target clears both, by the one variable.
const margins = [...css.matchAll(/scroll-margin-top:\s*([^;]+);/g)].map((match) => match[1].trim());
assert.ok(margins.length >= 6, margins);
assert.deepEqual([...new Set(margins)], ["var(--land-top)"]);
assert.match(css, /--land-top:\s*calc\(var\(--msg-top, [\d.]+rem\) \+ var\(--msg-h, 0px\) \+ \.75rem\)/);
verdict.everyTargetClearsBoth = true;

console.log(JSON.stringify(verdict));
