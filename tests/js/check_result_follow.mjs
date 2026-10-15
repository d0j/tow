// Home follows a manual check (?check_job=<id>, or a result still waiting: ?check_result=<id>)
// and opens its result when it ends. Runs the checked-in app.js with a tiny DOM and fake timers
// for one scenario (the first argument); prints a JSON record of what the page did.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const scenario = process.argv[2];
const scenarios = {
  // The result page of a check that was still waiting: the same address (and #fragment) when it ends.
  pending_result: {
    href: "http://127.0.0.1/?check_result=J1#check-result",
    pending: true,
    replies: [{ status: "done", flash: "", redirect: "/?check_result=J1#check-result" }],
  },
  // The page that follows a check: the result's own address takes its place.
  following: {
    href: "http://127.0.0.1/?check_job=J1",
    pending: false,
    replies: [
      { status: "queued", position: 2, progress: "Queued: position 2." },
      { status: "running", progress: "Checking since 12:00." },
      { status: "done", flash: "", redirect: "/?check_result=J1&credential_topic=t1#check-result" },
    ],
  },
  // TOW restarted meanwhile: the job is unknown, and the address stops asking for it.
  vanished: {
    href: "http://127.0.0.1/?check_result=J1&s=name#check-result",
    pending: true,
    replies: [{ status: "unknown" }],
  },
};
const { href, pending, replies } = scenarios[scenario];
const done = { assigned: [], replaced: [], reloaded: 0, history: [], progress: [] };
const timers = [];
const node = (name) => ({ name, textContent: "", hidden: false });
const resultNote = node("pending");
const status = node("status");
const note = node("note");
const document = {
  hidden: false,
  body: { name: "body" },
  activeElement: null,
  getElementById: (id) => (id === "check-activity" && !pending ? note : null),
  querySelector: (selector) => {
    if (selector === "[data-check-result-pending]") return pending ? resultNote : null;
    if (selector === "#check-activity [data-check-status]") return pending ? null : status;
    return null;
  },
  querySelectorAll: () => [],
  addEventListener() {},
  createElement: () => ({ append() {}, setAttribute() {}, classList: { add() {} } }),
};
const window = {
  setTimeout: (fn) => timers.push(fn), setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  location: {
    href,
    origin: "http://127.0.0.1",
    assign: (url) => done.assigned.push(String(url)),
    replace: (url) => done.replaced.push(String(url)),
    reload: () => { done.reloaded += 1; },
  },
  addEventListener() {},
};
let replyIndex = 0;
Object.assign(globalThis, {
  window, document, location: window.location,
  history: { replaceState: (_state, _title, url) => done.history.push(url) },
  setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  FormData: class {}, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async (url) => {
    if (!String(url).startsWith("/check/status?job=J1")) return { ok: true, status: 200, json: async () => ({}) };
    const reply = replies[Math.min(replyIndex, replies.length - 1)];
    replyIndex += 1;
    return { ok: true, status: 200, json: async () => reply };
  },
});
require("../../src/tow/static/app.js");

for (let poll = 0; poll < replies.length && timers.length; poll += 1) {
  const fn = timers.shift();
  await fn();
  if (status.textContent) done.progress.push(status.textContent);
}
done.pendingText = resultNote.textContent;
done.note = note.textContent;
console.log(JSON.stringify(done));
