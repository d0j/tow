// L1 (QA Q2): a language segment saves and the page comes back as /settings?open=language#acc-language
// with autofocus on the chosen segment. Browsers skip autofocus when the address has a fragment, so
// app.js puts the focus there; never over a focus the page already moved, never without a fragment.
// Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");

const run = ({ hash, focused = "" }) => {
  const document = fakeDocument();
  document.body.append(
    build(document, ["script", { id: "tow-i18n" }, "{}"]),
    build(document, ["details", { class: "settings-section", id: "acc-clients" }, ["summary", {}, "Clients"], ["input", { id: "q_host" }]]),
    build(document, ["details", { class: "settings-section", id: "acc-language", open: "" },
      ["summary", {}, "Language"],
      ["form", { method: "post", action: "/settings/language" },
        ["button", { type: "submit", name: "language", value: "auto", id: "seg-auto" }, "Auto"],
        ["button", { type: "submit", name: "language", value: "en", id: "seg-en", autofocus: "" }, "English"]]]),
  );
  if (focused) document.getElementById(focused).focus();
  const loads = [];
  const context = {
    document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
    WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
    Event: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    location: { href: `http://127.0.0.1/settings?open=language${hash}`, origin: "http://127.0.0.1", pathname: "/settings", search: "?open=language", hash },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: () => Promise.reject(new Error("no request expected")),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    innerWidth: 1280, innerHeight: 800,
  };
  context.window = { ...context, addEventListener(type, fn) { if (type === "load") loads.push(fn); }, confirm: () => true };
  vm.runInNewContext(source, context);
  return { document, load: () => loads.forEach((fn) => fn()) };
};

const verdict = {};

// Landed on the section: the chosen segment has the focus (failed before: <body>).
const landed = run({ hash: "#acc-language" });
assert.equal(landed.document.activeElement.id, "seg-en");
verdict.chosenSegmentFocused = true;

// Chromium's own move to the fragment once the page has loaded leaves the focus on <body>: back.
landed.document.activeElement = landed.document.body;
landed.load();
assert.equal(landed.document.activeElement.id, "seg-en");
verdict.keptAfterLoad = true;

// A focus the page already moved stays where it is.
const moved = run({ hash: "#acc-language", focused: "q_host" });
assert.equal(moved.document.activeElement.id, "q_host");
verdict.otherFocusKept = true;

// Without a fragment the browser applies autofocus itself; the script does nothing.
const plain = run({ hash: "" });
assert.equal(plain.document.activeElement, plain.document.body);
verdict.noFragmentNoChange = true;

console.log(JSON.stringify(verdict));
