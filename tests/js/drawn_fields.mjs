// The fields of a page as a browser shows them once it has loaded them (the HTML standard: a
// field holds the value its markup gives, a box its checked attribute, a select of one value the
// last option marked selected or else its first one that can be chosen, a select of many exactly
// the marked ones), judged by sync.js's one rule of what the owner changed (pages.js uses it too).
// Reads the fields from the JSON file named on the command line (tests/test_web_page_sync.py
// draws them from a real page); prints {"changed": [names of the fields counted as changed]}.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/sync.js", import.meta.url), "utf8");
const window = {};
vm.runInNewContext(source, { window, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, Boolean, Date });
const { changedField } = window.TowSync;

const loaded = (field) => {
  if (field.tag === "select") {
    const options = field.options.map((option) => ({ ...option, selected: false }));
    if (field.multiple) options.forEach((option) => { option.selected = option.defaultSelected; });
    else {
      const marked = options.map((option) => option.defaultSelected).lastIndexOf(true);
      const shown = marked >= 0 ? marked : options.findIndex((option) => !option.disabled);
      if (shown >= 0) options[shown].selected = true;
    }
    return { tagName: "SELECT", type: field.multiple ? "select-multiple" : "select-one", multiple: field.multiple, disabled: field.disabled, options };
  }
  return {
    tagName: field.tag.toUpperCase(),
    type: field.type,
    disabled: field.disabled,
    value: field.value,
    defaultValue: field.value,
    checked: field.checked,
    defaultChecked: field.checked,
    files: [],
  };
};

const fields = JSON.parse(readFileSync(process.argv[2], "utf8"));
console.log(JSON.stringify({ changed: fields.filter((field) => changedField(loaded(field))).map((field) => field.name) }));
