// One poll answer reaches every reader of healthPoll (sync.js's onPoll): a reader that throws never
// stops the others, and it takes the next answer once it works again. Without sync.js (the sign-in
// and first-start pages) nothing is asked and no reader is called. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const snippet = source.slice(source.indexOf("const healthPoll ="), source.indexOf('const clock = document.getElementById'));
let received = 0, broken = true, badCalls = 0, fetched = 0;
const polls = [];
const context = vm.createContext({
  window: { towSync: { onPoll: (fn) => polls.push(fn), health: () => ({ ok: true }), now() {} } },
  fetch: async () => { fetched++; return { ok: true, json: async () => ({ ok: true }) }; },
  bad: { receive: () => { badCalls++; if (broken) throw Error("synthetic view failure"); } },
  good: { receive: (data) => { if (data) received++; } },
});
vm.runInContext(snippet + "\nhealthPoll.add(bad); healthPoll.add(good);", context);
assert.equal(polls.length, 1); // one listener for every reader
polls[0](null, true);
assert.equal(received, 1);
polls[0](null, true);
assert.equal(received, 2);
broken = false;
polls[0](null, true);
assert.equal(received, 3);
assert.equal(badCalls, 3);
polls[0](null, false); // a failed answer: null to every reader
assert.equal(received, 3);
assert.equal(fetched, 0);
// Without sync.js: inert.
const alone = vm.createContext({ window: {}, fetch: context.fetch, good: context.good });
vm.runInContext(snippet + "\nhealthPoll.add(good); healthPoll.now();", alone);
assert.equal(fetched, 0);
console.log(JSON.stringify({ otherReadersContinue: true, recovery: true, failedAnswerIsNull: true, withoutSyncNothingAsked: true }));
