import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const snippet = source.slice(source.indexOf("const problemResult ="), source.indexOf("const undoForm ="));
for (const kind of ["warn", "err", null]) {
  const timers = [], live = {}, alert = {};
  const document = {
    querySelector: () => kind ? { dataset: { problemResult: kind }, textContent: "  Show A — timeout  " } : null,
    getElementById: key => key === "announce-alert" ? alert : live,
  };
  vm.runInNewContext(snippet, { document, window: { setTimeout: fn => timers.push(fn) } });
  assert.equal(timers.length, kind ? 1 : 0);
  timers.forEach(fn => fn());
  assert.equal(kind === "err" ? alert.textContent : live.textContent, kind ? "Show A — timeout" : undefined);
}
vm.runInNewContext(snippet, { document: {
  querySelector: () => ({ dataset: { problemResult: "err" } }), getElementById: () => null,
}, window: { setTimeout: () => { throw Error("no live region"); } } });
console.log(JSON.stringify({ warningAnnouncement: true, errorAnnouncement: true, missingResult: true, missingRegion: true }));
