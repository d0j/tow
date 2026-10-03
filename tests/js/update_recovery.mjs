import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/updates.js", import.meta.url), "utf8");
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
const fixture = async (options = {}) => {
  const elements = new Map();
  for (const name of ["release-badge", "release-status", "release-install", "update-progress", "update-support",
    "update-rollback", "update-version", "update-apply", "update-previous", "update-reload", "update-log", "update-log-text"]) {
    elements.set(`[data-${name}]`, { hidden: true, disabled: false, open: false, textContent: "", value: "", events: {},
      addEventListener(name, callback) { this.events[name] = callback; } });
  }
  const state = {
    job: { supported: true, active: false, status: "ok", id: "old-job", target: "1.22.20", rollback_version: "1.22.19" },
    posts: 0, logReads: 0, log: "old log", failStatus: false, lost: "", deferredStatus: null, deferredLog: null,
    ...options,
  };
  const timers = new Map();
  let timerId = 0;
  const response = data => ({ ok: true, json: async () => data });
  vm.runInNewContext(source, {
    document: { querySelector: selector => elements.get(selector), documentElement: { lang: "en" } },
    t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, URLSearchParams, AbortSignal, Date,
    window: { confirm: () => true, location: { reload() {} }, setInterval() {},
      setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
      clearTimeout(id) { timers.delete(id); } },
    fetch: async url => {
      if (url === "/updates/install") {
        state.posts++;
        if (state.lost === "before") throw new Error("lost before acceptance");
        state.job = { supported: true, active: true, status: "preparing", id: `job-${state.posts}`, target: "1.22.21" };
        if (state.lost === "after") throw new Error("lost after acceptance");
        return response({ ok: true, id: state.job.id });
      }
      if (url === "/updates/status") {
        if (state.deferredStatus) return state.deferredStatus;
        if (state.failStatus) return { ok: false };
        return response({ ...state.job });
      }
      if (url === "/updates/log") {
        state.logReads++;
        if (state.deferredLog) return state.deferredLog;
        return response({ text: state.log });
      }
      return response({ ok: true, available: true, latest: "1.22.21", checked_at: 1e300 });
    },
  });
  await flush();
  const el = name => elements.get(`[data-${name}]`);
  const click = async name => { el(name).events.click(); await flush(); };
  const poll = async () => {
    const entry = timers.entries().next().value;
    assert.ok(entry, "status polling must be scheduled");
    timers.delete(entry[0]); entry[1](); await flush();
  };
  return { state, el, click, poll, response };
};

// A second operation has a new ID: a lost response must not keep polling the first one.
{
  const f = await fixture();
  await f.click("release-install");
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  await f.poll();
  f.state.lost = "after";
  await f.click("release-install");
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  await f.poll();
  assert.match(f.el("update-progress").textContent, /js.releases.ok/);
  assert.equal(f.el("release-install").disabled, false);
  assert.equal(f.state.posts, 2);
}
// An old success cannot confirm a request whose acceptance is unknown. Never resend it.
{
  const f = await fixture();
  f.state.lost = "before";
  await f.click("release-install");
  await f.poll();
  assert.doesNotMatch(f.el("update-progress").textContent, /js.releases.ok/);
  assert.match(f.el("update-progress").textContent, /js.releases.confirming/);
  assert.equal(f.el("release-install").disabled, true);
  assert.equal(f.el("update-reload").hidden, false);
  assert.equal(f.state.posts, 1);
  f.state.job = { supported: true, active: false, status: "ok", id: "unrelated-job", target: "1.22.19" };
  await f.poll();
  assert.match(f.el("update-progress").textContent, /js.releases.confirming/);
  assert.equal(f.el("release-install").disabled, true);
  f.state.job = { supported: true, active: false, status: "ok", id: "late-job", target: "1.22.21" };
  await f.poll();
  assert.match(f.el("update-progress").textContent, /js.releases.ok/);
  assert.equal(f.el("release-install").disabled, false);
}
// The first status request can fail too; controls recover only after a readable status.
{
  const f = await fixture({ failStatus: true });
  assert.match(f.el("update-support").textContent, /js.releases.status_unavailable/);
  assert.doesNotMatch(f.el("update-support").textContent, /js.releases.unsupported/);
  assert.equal(f.el("update-apply").disabled, true);
  assert.equal(f.el("update-previous").disabled, true);
  f.state.failStatus = false;
  await f.poll();
  assert.equal(f.el("update-apply").disabled, false);
  assert.match(f.el("update-support").textContent, /js.releases.supported/);
}
// A different tab can replace the journal after our operation finishes between polls.
{
  const f = await fixture();
  await f.click("release-install");
  f.state.job = { supported: true, active: true, status: "installing", id: "another-tab", target: "1.22.22" };
  await f.poll();
  assert.match(f.el("update-progress").textContent, /js.releases.operation_changed.*1.22.22/);
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  await f.poll();
  assert.equal(f.el("release-install").disabled, false);
  assert.match(f.el("update-progress").textContent, /js.releases.ok.*1.22.22/);
}
// A transient status failure is not an unsupported installation; retry and disable actions.
{
  const f = await fixture();
  await f.click("release-install");
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  f.state.failStatus = true;
  await f.poll();
  f.state.failStatus = false;
  await f.poll();
  assert.equal(f.el("release-install").disabled, false);
  // A slow status read must not let a click submit a second installation.
  await f.click("release-install");
  let resolve;
  f.state.deferredStatus = new Promise(done => { resolve = done; });
  await f.poll();
  await f.click("release-install");
  assert.equal(f.state.posts, 2);
  const old = { ...f.state.job, active: false, status: "ok" };
  f.state.deferredStatus = null;
  resolve(f.response(old)); await flush();
  assert.equal(f.el("release-install").disabled, false);
}
// An open journal follows progress, including completion, and drops obsolete log reads.
{
  const f = await fixture();
  f.el("update-log").open = true;
  f.el("update-log").events.toggle(); await flush();
  assert.equal(f.el("update-log-text").textContent, "old log");
  await f.click("release-install");
  f.state.log = "installing now";
  await f.poll();
  assert.equal(f.el("update-log-text").textContent, "installing now");
  f.state.log = "finished";
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  await f.poll();
  assert.equal(f.el("update-log-text").textContent, "finished");
  let resolve;
  f.state.deferredLog = new Promise(done => { resolve = done; });
  f.el("update-log").events.toggle(); await flush();
  await f.click("release-install");
  resolve(f.response({ text: "obsolete log" })); await flush();
  assert.notEqual(f.el("update-log-text").textContent, "obsolete log");
  assert.doesNotMatch(f.el("release-status").textContent, /Invalid Date/);
}
// Completion during a slow read must request the final log again after that read ends.
{
  const f = await fixture();
  f.el("update-log").open = true;
  await f.click("release-install");
  let resolve;
  f.state.deferredLog = new Promise(done => { resolve = done; });
  await f.poll();
  f.state.job = { ...f.state.job, active: false, status: "ok" };
  f.state.log = "final log after completion";
  await f.poll();
  f.state.deferredLog = null;
  resolve(f.response({ text: "older log captured while preparing" }));
  await flush();
  assert.equal(f.el("update-log-text").textContent, "final log after completion");
  assert.equal(f.state.logReads, 2); // one in-flight read plus one coalesced final refresh
}
console.log(JSON.stringify({ repeatedLostResponse: true, noStaleSuccess: true, statusRetry: true, liveJournal: true, staleLogIgnored: true, finiteDates: true, finalLogAfterSlowRead: true }));
