import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const snippet = source.slice(source.indexOf("const healthPoll ="), source.indexOf('const clock = document.getElementById'));
let now = 0, scheduled = null, received = 0, broken = true, polls = 0;
const context = vm.createContext({
  Date: { now: () => now },
  document: { hidden: false, addEventListener() {} },
  window: { setTimeout: (fn, ms) => { scheduled = { fn, ms }; return 1; }, clearTimeout() {} },
  fetch: async () => { polls++; return { ok: true, json: async () => ({ ok: true }) }; },
  bad: { due: () => 0, shown: () => true, receive: () => { if (broken) throw Error("synthetic view failure"); } },
  good: { due: () => now + 15000, shown: () => true, receive: () => { received++; } },
});
vm.runInContext(snippet + "\nhealthPoll.add(bad); healthPoll.add(good);", context);
await scheduled.fn();
assert.equal(received, 1);
assert.equal(scheduled.ms, 15000, "one broken reader must not spin on its overdue deadline");
now += 15000;
await scheduled.fn();
assert.equal(received, 2);
assert.equal(scheduled.ms, 15000);
broken = false;
await scheduled.fn();
assert.equal(received, 3);
assert.equal(polls, 3);
// A lone broken reader still retries, but no faster than once a minute.
const isolated = vm.createContext({ ...context, bad: context.bad });
broken = true;
vm.runInContext(snippet + "\nhealthPoll.add(bad);", isolated);
await scheduled.fn();
assert.equal(scheduled.ms, 60000);
console.log(JSON.stringify({ otherReadersContinue: true, boundedFailure: true, recovery: true, noSpin: true }));
