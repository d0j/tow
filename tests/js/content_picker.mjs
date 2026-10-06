// Execute the real picker with a minimal DOM and controlled asynchronous responses.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.value = ""; this.files = []; this.hidden = false;
    this.classList = { toggle() {} };
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  fire(name) { for (const fn of this.listeners[name] || []) fn({}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...nodes) { this.children.push(...nodes.flatMap((n) => n.tagName === "fragment" ? n.children : [n])); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  contains(node) { return this === node || this.children.some((n) => n instanceof Element && n.contains(node)); }
  focus() {}
  matches(selector) { return selector === "[data-content-picker]" && this.isPicker; }
  querySelectorAll(selector) {
    const all = this.children.flatMap((n) => n instanceof Element ? [n, ...n.querySelectorAll("*")] : []);
    if (selector === "[data-content-key]") return all.filter((n) => n.dataset.contentKey);
    return all;
  }
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const files = [
  { id: 0, path: "Season 1/A*.mkv", size: "9007199254740993" },
  { id: 1, path: "Season 1/B.mkv", size: "200" },
  ...Array.from({ length: 19998 }, (_, n) => ({ id: n + 2, path: `Extras/note-${n}.txt`, size: "0" })),
];
function setup(saved = null, existing = []) {
  const root = new Element(); root.isPicker = true; root.dataset.existing = JSON.stringify(existing);
  const fields = Object.fromEntries(["selection_mode", "selection_value", "content_token", "selection_indices", "url", "client_id", "title", "tracking_mode"].map((key) => [key, new Element()]));
  fields.tracking_mode.value = "watch";
  fields.selection_mode.value = "exact"; fields.url.value = "https://tracker.example/topic/1"; fields.client_id.value = "main";
  if (saved) { fields.content_token.value = saved.token; fields.selection_indices.value = "[1]"; }
  fields.selection_value.closest = () => new Element();
  const form = new Element(); form.elements = { namedItem: (name) => fields[name] };
  root.closest = () => form;
  const controls = Object.fromEntries(["status", "results", "tree", "search", "more", "prev", "pages", "page", "load", "upload", "limited", "fresh", "cached", "magnet", "manual-actions", "all", "none", "help", "help-text"].map((key) => [key, new Element()]));
  root.querySelector = (key) => controls[key.replace(/\[data-content-(.*)\]/, "$1")];
  const document = {
    activeElement: null, documentElement: root, body: root,
    getElementById: () => ({ textContent: "{}" }),
    createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element("fragment"),
    createTextNode: (text) => ({ textContent: text }),
  };
  root.querySelectorAll = () => [];
  const requests = [];
  vm.runInNewContext(fs.readFileSync(new URL("../../src/tow/static/content.js", import.meta.url), "utf8"), {
    document, Element, FormData, AbortController,
    MutationObserver: class { observe() {} },
    window: { setTimeout, clearTimeout, confirm: () => controls.limited.approved === true },
    fetch: (url, options) => new Promise((resolve) => requests.push({ url, options, respond: (data, ok = true) => resolve({ ok, json: async () => data }) })),
  });
  return { fields, controls, requests, form, root };
}
const find = (tree, name) => tree.querySelectorAll("*").find((n) => n.attributes["aria-label"] === name);
const reply = async (request, data, ok = true) => { request.respond(data, ok); await tick(); };
const snapshot = { token: "a".repeat(32), files };
const submitAllowed = (form) => {
  let prevented = false;
  for (const fn of form.listeners.submit) fn({ preventDefault: () => { prevented = true; }, stopImmediatePropagation() {} });
  return !prevented;
};

const a = setup();
assert.equal(a.requests.length, 0, "no automatic tracker access");
a.controls.load.fire("click");
await reply(a.requests[0], snapshot);
assert.equal(a.controls.tree.children.length, 2, "folders initially collapsed");
find(a.controls.tree, "Season 1/A*.mkv"); // not rendered until expanded
const folderToggle = a.controls.tree.querySelectorAll("*").find((n) => n.dataset.contentKey === "folder:Season 1");
folderToggle.fire("click");
let file = find(a.controls.tree, "Season 1/A*.mkv"); file.checked = true; file.fire("change");
assert.equal(a.fields.selection_indices.value, "[0]");
let folder = a.controls.tree.querySelectorAll("*").find((n) => n.dataset.contentKey === "select:Season 1");
assert.equal(folder.indeterminate, true, "tri-state folder");
a.controls.search.value = "B.mkv"; a.controls.search.fire("input");
assert.equal(a.fields.selection_indices.value, "[0]", "search preserves hidden selection");
assert.ok(find(a.controls.tree, "Season 1/B.mkv"));
a.controls.search.value = "Extras"; a.controls.search.fire("input");
assert.equal(a.controls.tree.children.length, 200, "DOM bounded even for 20,000 files");
a.controls.more.fire("click");
assert.equal(a.controls.tree.children.length, 200, "paging replaces rows");
a.controls.search.value = "note-123.txt"; a.controls.search.fire("input");
folder = a.controls.tree.querySelectorAll("*").find((n) => n.dataset.contentKey === "select:Extras");
folder.checked = true; folder.fire("change");
assert.equal(JSON.parse(a.fields.selection_indices.value).length, 19999, "folder affects whole subtree, not search subset");
a.controls.none.fire("click");
assert.equal(a.fields.selection_indices.value, "[]");
let prevented = false;
for (const fn of a.form.listeners.submit) fn({ preventDefault: () => { prevented = true; }, stopImmediatePropagation() {} });
assert.equal(prevented, true, "empty manual selection cannot save");

const b = setup(); b.controls.load.fire("click");
b.fields.url.value = "https://tracker.example/topic/2"; b.fields.url.fire("input");
await reply(b.requests[0], snapshot);
assert.equal(b.fields.content_token.value, "", "late metadata cannot replace source edits");

const c = setup({ token: snapshot.token });
assert.equal(c.requests[0].url, "/content/snapshot", "refused form restores without second download");
await reply(c.requests[0], snapshot);
assert.equal(c.fields.selection_indices.value, "[1]");
c.fields.selection_mode.value = "files"; c.fields.selection_value.value = "*.mkv"; c.fields.selection_mode.fire("change");
const pendingRule = c.requests.at(-1);
c.fields.selection_mode.value = "exact"; c.fields.selection_mode.fire("change");
await reply(pendingRule, { indices: [0, 1] });
assert.equal(c.fields.selection_indices.value, "[1]", "late rules never override manual choice");
c.controls.all.fire("click");
assert.equal(JSON.parse(c.fields.selection_indices.value).length, 20000);
c.fields.client_id.value = "other"; c.fields.client_id.fire("change");
assert.equal(c.fields.content_token.value, "", "client edits invalidate preparation");

const d = setup();
d.controls.upload.files = [new Blob(["local metadata"])];
d.controls.magnet.fire("click");
assert.equal(d.requests.length, 1, "magnet uses one explicit action only");
assert.equal(d.requests[0].options.body.get("source"), "magnet");
assert.equal(d.requests[0].options.body.has("torrent"), false, "native request never mixes uploaded metadata");
assert.equal(d.controls.magnet.disabled, true);
await reply(d.requests[0], snapshot);
assert.equal(d.controls.magnet.disabled, false);

const e = setup();
e.controls.magnet.fire("click");
e.form.fire("reset");
await reply(e.requests[0], snapshot);
assert.equal(e.fields.content_token.value, "", "cancel prevents a late native response from reviving an edit");
assert.equal(e.controls.status.textContent, "");
assert.equal(e.controls.results.hidden, true);

const f = setup(null, [files[0]]);
assert.equal(submitAllowed(f.form), true, "unchanged existing choice needs no metadata request");
f.controls.load.fire("click");
await reply(f.requests[0], snapshot);
f.controls.none.fire("click");
f.controls.search.value = "B.mkv"; f.controls.search.fire("input");
file = find(f.controls.tree, "Season 1/B.mkv"); file.checked = true; file.fire("change");
assert.equal(f.fields.selection_indices.value, "[1]");
f.controls.load.fire("click");
assert.equal(submitAllowed(f.form), false, "refresh must not silently save the old existing choice");
f.requests[1].respond({ error: "metadata unavailable" }, false); await tick();
assert.equal(submitAllowed(f.form), false, "failed refresh must not silently save the old existing choice");
f.controls.load.fire("click");
await reply(f.requests[2], snapshot);
assert.equal(f.fields.selection_indices.value, "[1]", "retry retains the user's intended choice");
assert.equal(submitAllowed(f.form), true);
f.controls.load.fire("click");
f.form.fire("reset");
f.fields.content_token.value = ""; f.fields.selection_indices.value = "";
assert.equal(submitAllowed(f.form), true, "cancel restores unchanged existing-policy submission");
await reply(f.requests[3], snapshot);
assert.equal(f.controls.results.hidden, true);
assert.equal(f.fields.content_token.value, "");
assert.equal(submitAllowed(f.form), true, "late cancelled response cannot restore the refresh guard");

const g = setup();
g.root.dataset.topicId = "fixture";
g.fields.title.value = "Show Season 2";
g.controls.load.fire("click"); await reply(g.requests[0], snapshot);
g.fields.selection_mode.value = "episodes"; g.fields.selection_value.value = "S02E01";
g.fields.selection_mode.fire("change");
assert.equal(g.requests[1].options.body.get("topic_id"), "fixture");
assert.equal(g.requests[1].options.body.get("title"), "Show Season 2");
assert.equal(g.requests[1].options.body.get("tracking_mode"), "watch");
await reply(g.requests[1], { indices: [1] });
g.fields.tracking_mode.value = "once"; g.fields.tracking_mode.fire("change");
const staleOnce = g.requests.at(-1);
g.fields.tracking_mode.value = "watch"; g.fields.tracking_mode.fire("change");
const pendingWatch = g.requests.at(-1);
const waitingForResponse = g.controls.status.textContent;
await reply(staleOnce, { indices: [0] });
assert.equal(g.controls.status.textContent, waitingForResponse, "late lifecycle reply cannot replace the current preview");
await reply(pendingWatch, { indices: [], waiting: "Waiting for future episodes" });
assert.equal(g.controls.status.textContent, "Waiting for future episodes");
g.controls.search.value = "Extras"; g.controls.search.fire("input");
assert.equal(g.controls.status.textContent, "Waiting for future episodes", "search preserves waiting status");
g.controls.more.fire("click");
assert.equal(g.controls.status.textContent, "Waiting for future episodes", "paging preserves waiting status");
g.fields.selection_value.value = "invalid"; g.fields.selection_mode.fire("change");
g.requests.at(-1).respond({ error: "Rule invalid" }, false); await tick();
g.controls.prev.fire("click");
assert.equal(g.controls.status.textContent, "Rule invalid", "paging preserves rule errors");
g.controls.search.value = "B.mkv"; g.controls.search.fire("input");
assert.equal(g.controls.status.textContent, "Rule invalid", "search preserves rule errors");
g.fields.selection_value.value = "S02E01"; g.fields.selection_mode.fire("change");
g.controls.search.fire("input");
assert.equal(g.controls.status.textContent, "content.js.rule_loading", "search preserves pending status");
await reply(g.requests.at(-1), { indices: [1] });
assert.equal(g.controls.status.textContent, "content.js.rule", "successful retry clears the old error");
g.fields.selection_mode.value = "exact"; g.fields.selection_mode.fire("change");
assert.equal(g.controls.status.textContent, "content.js.count", "manual mode drops the old rule status");
g.fields.selection_mode.value = "episodes"; g.fields.selection_mode.fire("change");
await reply(g.requests.at(-1), { indices: [1] });
g.fields.tracking_mode.value = "once"; g.fields.tracking_mode.fire("change");
const staleTitle = g.requests.at(-1);
g.fields.title.value = "Show Season 3"; g.fields.title.fire("input");
await reply(staleTitle, { indices: [0] });
await new Promise((resolve) => setTimeout(resolve, 210));
assert.equal(g.requests.at(-1).options.body.get("title"), "Show Season 3", "editing the title refreshes rule context");
g.form.fire("reset");
await reply(g.requests.at(-1), { indices: [1] });
assert.equal(g.controls.results.hidden, true);


const cached = setup();
cached.controls.load.fire("click");
await reply(cached.requests[0], { ...snapshot, cached: true });
assert.equal(cached.controls.cached.hidden, false);
assert.equal(cached.controls.fresh.hidden, false);
cached.controls.fresh.fire("click");
assert.equal(cached.requests.at(-1).options.body.get("source"), "fresh");
await reply(cached.requests.at(-1), { code: "content.limited", error: "Limit warning" }, false);
const countBeforeConsent = cached.requests.length;
cached.controls.limited.fire("click");
assert.equal(cached.requests.length, countBeforeConsent, "declined quota confirmation sends no request");
cached.controls.limited.approved = true;
cached.controls.limited.fire("click");
assert.equal(cached.requests.at(-1).options.body.get("allow_limited"), "true");
await reply(cached.requests.at(-1), { ...snapshot, cached: false });
assert.equal(cached.controls.cached.hidden, true);
assert.equal(cached.controls.fresh.hidden, true);
console.log(JSON.stringify({ safeSelection: true, boundedTree: true, staleResponses: true, restoredDraft: true, refreshSubmission: true, ruleContext: true, cacheLabel: true, explicitFresh: true, quotaConsent: true }));
