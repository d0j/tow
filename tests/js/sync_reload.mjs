// sync.js's reloads without the page (createReloader): an idle page reloads once its delay has
// passed; a page in use is told instead and reloads once idle; activity during the delay cancels
// it; the same reason and target never reload twice in ten minutes (no loop when the server keeps
// saying "another version"); without storage there is only the notice. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { fakeTimers, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/sync.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, Boolean, Date });
const { createReloader } = window.TowSync;

const harness = ({ storage = memoryStorage() } = {}) => {
  const clock = fakeTimers();
  let idle = true;
  let reloads = 0;
  const notices = [];
  const reloader = createReloader({
    idle: () => idle, reload: () => { reloads += 1; }, notice: (reason, target) => notices.push([reason, target]),
    storage, now: clock.now, setTimer: clock.setTimeout, clearTimer: clock.clearTimeout,
  });
  return { clock, reloader, notices, reloads: () => reloads, busy: (on) => { idle = !on; } };
};
const verdict = {};

// Idle: reloaded after the delay, silently.
{
  const test = harness();
  test.reloader.want("version", "1.34.0|s2", { delay: 5000 });
  test.clock.advance(4999);
  assert.equal(test.reloads(), 0);
  test.clock.advance(1);
  assert.equal(test.reloads(), 1);
  assert.deepEqual(test.notices, []);
}
verdict.idleReloadsAfterDelay = true;

// A new version is said at once, also on an idle page (why it reloads 5 s later).
{
  const test = harness();
  test.reloader.want("version", "1.34.0|s2", { delay: 5000, announce: true });
  assert.equal(JSON.stringify(test.notices), JSON.stringify([["version", "1.34.0|s2"]]));
  test.clock.advance(5000);
  assert.equal(test.reloads(), 1);
}
verdict.announcedAtOnce = true;

// Activity during the delay cancels it and says why; idle again, the next check starts it again.
{
  const test = harness();
  test.reloader.want("version", "1.34.0|s2", { delay: 5000 });
  test.clock.advance(3000);
  test.reloader.activity();
  test.clock.advance(10000);
  assert.equal(test.reloads(), 0);
  assert.equal(JSON.stringify(test.notices), JSON.stringify([["version", "1.34.0|s2"]]));
  test.reloader.check();
  test.clock.advance(5000);
  assert.equal(test.reloads(), 1);
}
verdict.activityCancels = true;

// A page in use is told at once and reloads only once idle.
{
  const test = harness();
  test.busy(true);
  test.reloader.want("language", "ru", { delay: 1000 });
  test.clock.advance(60000);
  assert.equal(test.reloads(), 0);
  assert.equal(JSON.stringify(test.notices), JSON.stringify([["language", "ru"]]));
  test.busy(false);
  test.reloader.check();
  test.clock.advance(1000);
  assert.equal(test.reloads(), 1);
}
verdict.busyPageToldThenReloaded = true;

// No loop: the page after the reload is told the same again - it does not reload again.
{
  const storage = memoryStorage();
  const first = harness({ storage });
  first.reloader.want("version", "9.9.9|s9", { delay: 5000 });
  first.clock.advance(5000);
  assert.equal(first.reloads(), 1);
  const again = harness({ storage }); // the reloaded page, the same tab's storage
  again.reloader.want("version", "9.9.9|s9", { delay: 5000 });
  again.clock.advance(60000);
  assert.equal(again.reloads(), 0);
  assert.equal(JSON.stringify(again.notices), JSON.stringify([["version", "9.9.9|s9"]]));
  // A newer target is a new reason to reload once.
  again.reloader.want("version", "9.9.10|s10", { delay: 5000 });
  again.clock.advance(5000);
  assert.equal(again.reloads(), 1);
}
verdict.noReloadLoop = true;

// Without storage (it cannot remember a reload): only the notice, never a reload.
{
  const test = harness({ storage: null });
  test.reloader.want("version", "1.34.0|s2", { delay: 5000 });
  test.clock.advance(60000);
  assert.equal(test.reloads(), 0);
  assert.equal(test.notices.length, 1);
}
verdict.noStorageNoticeOnly = true;

// The reason gone (the server's version is the page's again): the notice goes, nothing reloads.
{
  const test = harness();
  test.busy(true);
  test.reloader.want("version", "1.34.0|s2", { delay: 5000 });
  test.reloader.drop("version");
  test.busy(false);
  test.reloader.check();
  test.clock.advance(60000);
  assert.equal(test.reloads(), 0);
  assert.equal(JSON.stringify(test.notices), JSON.stringify([["version", "1.34.0|s2"], ["version", ""]]));
}
verdict.droppedReasonClears = true;

console.log(JSON.stringify(verdict));
