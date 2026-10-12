// Home's "Needs attention" folds into a warning button and remembers that per browser, for one
// set of problems: the same fingerprint keeps it folded after a reload, another one (a new
// problem) opens it by itself and forgets the old choice; no storage or a broken record leaves it
// open. Runs the checked-in attention.js with a tiny DOM, as Home's <head> runs it.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/attention.js", import.meta.url), "utf8");

const page = (fingerprint, storage) => {
  const classes = new Set();
  const loaded = [];
  const focused = [];
  const button = (name) => {
    const listeners = {};
    return { name, addEventListener: (event, fn) => { listeners[event] = fn; }, click: () => listeners.click(), focus: () => focused.push(name) };
  };
  const fold = button("fold");
  const unfold = button("unfold");
  const document = {
    currentScript: { dataset: { fingerprint } },
    documentElement: {
      classList: {
        add: (name) => classes.add(name),
        toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      },
    },
    addEventListener: (event, fn) => event === "DOMContentLoaded" && loaded.push(fn),
    querySelector: (selector) => ({ "[data-attention-fold]": fold, "[data-attention-unfold]": unfold })[selector] || null,
  };
  vm.runInNewContext(source, { document, localStorage: storage, JSON });
  loaded.forEach((fn) => fn());
  return { folded: () => classes.has("attention-folded"), js: classes.has("attention-js"), fold, unfold, focused };
};

const memory = () => {
  const data = new Map();
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
};
const broken = {
  getItem() { throw new Error("SecurityError"); },
  setItem() { throw new Error("SecurityError"); },
  removeItem() { throw new Error("SecurityError"); },
};

const store = memory();
const first = page("aaaa", store);
const openAtFirst = first.js && !first.folded();
first.fold.click();
const foldsAndFocusesTheButton = first.folded() && first.focused.at(-1) === "unfold"
  && store.data.get("tow.attention") === JSON.stringify({ folded: true, fp: "aaaa" });

const reload = page("aaaa", store);
const staysFoldedAfterReload = reload.folded();
reload.unfold.click();
const opensAgainAndForgets = !reload.folded() && reload.focused.at(-1) === "fold" && !store.data.has("tow.attention");

reload.fold.click();
const changed = page("bbbb", store);
const aNewProblemOpensIt = !changed.folded() && !store.data.has("tow.attention");

page("aaaa", store).fold.click();
const none = page("", store);
const noProblemsForgetsTheChoice = !none.folded() && !store.data.has("tow.attention");

store.data.set("tow.attention", "{not json");
const brokenRecordOpens = !page("aaaa", store).folded();
store.data.set("tow.attention", JSON.stringify({ folded: false, fp: "aaaa" }));
const onlyAFoldedRecordFolds = !page("aaaa", store).folded();

const blocked = page("aaaa", broken);
const noStorageStaysOpen = !blocked.folded();
blocked.fold.click();
const noStorageStillFoldsThePage = blocked.folded();

console.log(JSON.stringify({
  openAtFirst,
  foldsAndFocusesTheButton,
  staysFoldedAfterReload,
  opensAgainAndForgets,
  aNewProblemOpensIt,
  noProblemsForgetsTheChoice,
  brokenRecordOpens,
  onlyAFoldedRecordFolds,
  noStorageStaysOpen,
  noStorageStillFoldsThePage,
}));
