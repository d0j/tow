// app.js's Home list tools after sync.js changed the rows (towList.refresh): the server's order is
// the list's (the owner's own sort on top), the search finds a row that came, the status filter
// reads the new row's tone, a new site is a choice, a gone row is forgotten - and a row the owner
// works in is neither hidden by a filter applied again nor moved until the work ends. Runs app.js's
// list tools on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, FakeElement, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const snippet = source.slice(source.indexOf("// Search and list tools (E1)"), source.indexOf("const paintLog ="));

Object.defineProperty(FakeElement.prototype, "options", { get() { return this.querySelectorAll("option"); } });
const document = fakeDocument();
document.createTextNode = (text) => String(text);
const row = (id, { tone = "ok", tracker = "rutor", name = id } = {}) => build(document, ["div", {
  class: "row-wrap", id: `row-${id}`, "data-q": `${name} ${tracker}`, "data-tracker": tracker, "data-name": name,
}, ["details", { class: "row-edit" }, ["summary", {}, ["span", { class: `dot lg ${tone}` }], ["span", { class: "clip", "data-hl": "" }, name],
  ["span", { class: "topic-tracker", title: tracker === "newsite" ? "New Site" : "Rutor" }, tracker]]]]);
const tools = build(document, ["div", { id: "list-tools" },
  ["button", { "data-filter": "", class: "chip on" }, "All"], ["button", { "data-filter": "problem", class: "chip" }, "Problems"],
  ["select", { id: "list-tracker" }, ["option", { value: "" }, "All sites"], ["option", { value: "rutor" }, "Rutor"]],
  ["select", { id: "list-sort" }, ["option", { value: "" }, "Added"], ["option", { value: "name" }, "Name"]]]);
const list = build(document, ["div", { id: "topics" }]);
const q = build(document, ["input", { id: "q" }]);
document.body.append(q, tools, list);
list.append(row("a", { name: "alpha" }), row("b", { name: "bravo", tone: "bad" }), row("c", { name: "charlie" }));
const guarded = new Set();
const window = {
  towSync: { protectedRow: (el) => guarded.has(el.id) },
  towRows: { replaced() {} },
  setTimeout: () => 0, clearTimeout() {},
};
const context = vm.createContext({
  window, document, location: { href: "http://127.0.0.1/" }, URL, Intl, localStorage: memoryStorage(),
  history: { replaceState() {} }, Event: class { constructor(type) { this.type = type; } },
  Option: class { constructor(text, value) { const option = document.createElement("option"); option.setAttribute("value", value); option.textContent = text; return option; } },
  t: (key, vars = {}) => `${key}${JSON.stringify(vars)}`,
});
vm.runInContext(snippet, context);
const ids = () => list.querySelectorAll(".row-wrap").map((el) => el.id.slice(4));
const shown = () => list.querySelectorAll(".row-wrap").filter((el) => !el.hidden).map((el) => el.id.slice(4));
const verdict = {};

// A row came (inserted by sync.js before "a") and one went: the server's order, the search finds
// the new one, the gone one is forgotten.
{
  const fresh = row("n", { name: "november", tracker: "newsite" });
  document.getElementById("row-a").before(fresh);
  document.getElementById("row-c").remove();
  window.towList.refresh({ order: ["a", "n", "b"] });
  assert.deepEqual(ids(), ["a", "n", "b"]);
  q.value = "november";
  window.towList.refresh({ order: ["a", "n", "b"] });
  assert.deepEqual(shown(), ["n"]);
  q.value = "";
  window.towList.refresh({ order: ["a", "n", "b"] });
  assert.deepEqual(shown(), ["a", "n", "b"]);
}
verdict.membershipOrderAndSearch = true;

// A new site is a choice of the site filter, named by its title.
{
  const options = document.getElementById("list-tracker").options.map((option) => [option.value, option.textContent]);
  assert.deepEqual(options, [["", "All sites"], ["rutor", "Rutor"], ["newsite", "New Site"]]);
}
verdict.trackerChoiceAdded = true;

// The status filter reads a redrawn row's tone (a new element: its own tone, not the old one's).
{
  document.dispatch(tools.querySelector('[data-filter="problem"]'), "click");
  assert.deepEqual(shown(), ["b"]);
  const redrawn = row("a", { name: "alpha", tone: "bad" });
  document.getElementById("row-a").replaceWith(redrawn);
  window.towList.refresh({ order: ["a", "n", "b"] });
  assert.deepEqual(shown(), ["a", "b"]);
}
verdict.toneCacheFollowsRedraw = true;

// The row being edited is not hidden by the filter applied again: it turned green elsewhere,
// but it stays in sight until the work ends.
{
  guarded.add("row-a");
  const green = row("a", { name: "alpha", tone: "ok" });
  document.getElementById("row-a").replaceWith(green);
  guarded.add("row-a");
  window.towList.refresh({ order: ["a", "n", "b"], keep: true });
  assert.deepEqual(shown(), ["a", "b"]);
  guarded.delete("row-a");
  window.towList.refresh({ order: ["a", "n", "b"], keep: true });
  assert.deepEqual(shown(), ["b"]);
  document.dispatch(tools.querySelector('[data-filter=""]'), "click");
}
verdict.filterKeepsProtectedRow = true;

// A new order waits while a row is worked in, then applies.
{
  guarded.add("row-n");
  window.towList.refresh({ order: ["b", "n", "a"], keep: true });
  assert.deepEqual(ids(), ["a", "n", "b"]);
  guarded.clear();
  window.towList.refresh({ order: ["b", "n", "a"], keep: true });
  assert.deepEqual(ids(), ["b", "n", "a"]);
}
verdict.reorderWaitsForTheOwner = true;

// The owner's own sort stays on top of the server's order.
{
  const sort = document.getElementById("list-sort");
  sort.value = "name";
  (sort.listeners.change || []).forEach((fn) => fn({ target: sort }));
  assert.deepEqual(ids(), ["a", "b", "n"]); // alpha, bravo, november
  window.towList.refresh({ order: ["n", "b", "a"], keep: true });
  assert.deepEqual(ids(), ["a", "b", "n"]);
}
verdict.localSortKept = true;

console.log(JSON.stringify(verdict));
