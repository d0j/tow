// A small DOM for the page scripts' tests: elements with attributes, classes, data-*, children,
// events and the selectors the scripts use (tag, #id, .class, [attr], [attr=value], descendants,
// lists). Enough for messages.js and app.js's message paths; nothing is laid out.
const camel = (name) => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
const kebab = (name) => name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);

const parseCompound = (text) => {
  const part = { tag: "", id: "", classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g;
  let match;
  while ((match = re.exec(text))) {
    if (match[1]) part.tag = match[1].toLowerCase();
    else if (match[2]) part.id = match[2];
    else if (match[3]) part.classes.push(match[3]);
    else part.attrs.push([match[4], match[5]]);
  }
  return part;
};
const matchesCompound = (node, part) => {
  if (!(node instanceof FakeElement)) return false;
  if (part.tag && node.tagName.toLowerCase() !== part.tag) return false;
  if (part.id && node.id !== part.id) return false;
  if (part.classes.some((name) => !node.classList.contains(name))) return false;
  return part.attrs.every(([name, value]) => {
    const actual = node.getAttribute(name);
    return actual !== null && (value === undefined || actual === value);
  });
};
const matchesSelector = (node, selector) => selector.split(",").some((alternative) => {
  const parts = alternative.trim().replace(/^:scope\s*>?\s*/, "").split(/\s+/).map(parseCompound);
  if (!matchesCompound(node, parts.at(-1))) return false;
  let index = parts.length - 2;
  for (let up = node.parent; up && index >= 0; up = up.parent) if (matchesCompound(up, parts[index])) index -= 1;
  return index < 0;
});

export class FakeElement {
  constructor(tagName, document) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = document;
    this.attributes = {};
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.text = "";
    this.rect = { top: 0, bottom: 20, height: 20 };
    const element = this;
    this.dataset = new Proxy({}, {
      get: (_, key) => element.attributes[`data-${kebab(String(key))}`],
      set: (_, key, value) => { element.attributes[`data-${kebab(String(key))}`] = String(value); return true; },
      deleteProperty: (_, key) => { delete element.attributes[`data-${kebab(String(key))}`]; return true; },
      has: (_, key) => `data-${kebab(String(key))}` in element.attributes,
    });
    this.classList = {
      list: () => (element.attributes.class || "").split(/\s+/).filter(Boolean),
      contains: (name) => element.classList.list().includes(name),
      add: (...names) => { element.attributes.class = [...new Set([...element.classList.list(), ...names])].join(" "); },
      remove: (...names) => { element.attributes.class = element.classList.list().filter((n) => !names.includes(n)).join(" "); },
      toggle: (name, force) => {
        const on = force === undefined ? !element.classList.contains(name) : Boolean(force);
        if (on) element.classList.add(name); else element.classList.remove(name);
        return on;
      },
    };
  }
  get id() { return this.attributes.id || ""; }
  set id(value) { this.attributes.id = String(value); }
  get className() { return this.attributes.class || ""; }
  set className(value) { this.attributes.class = String(value); }
  get hidden() { return "hidden" in this.attributes; }
  set hidden(value) { if (value) this.attributes.hidden = ""; else delete this.attributes.hidden; }
  get open() { return "open" in this.attributes; }
  set open(value) {
    const was = this.open;
    if (value) this.attributes.open = ""; else delete this.attributes.open;
    if (was !== Boolean(value)) this.ownerDocument.dispatch(this, "toggle", { bubbles: false });
  }
  get title() { return this.attributes.title || ""; }
  set title(value) { this.attributes.title = String(value); }
  get type() { return this.attributes.type || ""; }
  set type(value) { this.attributes.type = String(value); }
  get name() { return this.attributes.name || ""; }
  set name(value) { this.attributes.name = String(value); }
  get value() { return this.attributes.value || ""; }
  set value(value) { this.attributes.value = String(value); }
  get method() { return this.attributes.method || ""; }
  get action() { return this.attributes.action || ""; }
  get disabled() { return "disabled" in this.attributes; }
  set disabled(value) { if (value) this.attributes.disabled = ""; else delete this.attributes.disabled; }
  getClientRects() { return [this.rect]; }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.text + this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join(""); }
  set textContent(value) { this.children.forEach((child) => { if (child instanceof FakeElement) child.parent = null; }); this.children = []; this.text = String(value); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  hasAttribute(name) { return name in this.attributes; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof FakeElement) { node.remove(); node.parent = this; }
      this.children.push(node);
    }
  }
  prepend(...nodes) {
    for (const node of nodes.reverse()) {
      if (node instanceof FakeElement) { node.remove(); node.parent = this; }
      this.children.unshift(node);
    }
  }
  replaceChildren(...nodes) { this.textContent = ""; this.append(...nodes); }
  remove() {
    if (!this.parent) return;
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }
  replaceWith(node) {
    const parent = this.parent;
    const index = parent.children.indexOf(this);
    this.remove();
    node.remove();
    node.parent = parent;
    parent.children.splice(index, 0, node);
  }
  get isConnected() { let node = this; while (node.parent) node = node.parent; return node === this.ownerDocument.root; }
  contains(node) { return node === this || this.children.some((child) => child instanceof FakeElement && child.contains(node)); }
  closest(selector) { for (let node = this; node; node = node.parent) if (matchesSelector(node, selector)) return node; return null; }
  matches(selector) { return matchesSelector(this, selector); }
  descendants() { return this.children.filter((c) => c instanceof FakeElement).flatMap((c) => [c, ...c.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter((node) => matchesSelector(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  focus() { this.ownerDocument.activeElement = this; }
  getBoundingClientRect() { return this.rect; }
}

export const fakeDocument = () => {
  const document = {
    listeners: {},
    hidden: false,
    documentElement: { lang: "en" },
    createElement: (tag) => new FakeElement(tag, document),
    createElementNS: (_, tag) => new FakeElement(tag, document),
    getElementById: (id) => document.root.querySelector(`#${id}`),
    querySelector: (selector) => document.root.querySelector(selector),
    querySelectorAll: (selector) => document.root.querySelectorAll(selector),
    addEventListener: (type, fn, capture) => { (document.listeners[type] ||= []).push({ fn, capture: Boolean(capture) }); },
    // Capture listeners of the document, the target's own, then (bubbling) its ancestors' and the document's.
    dispatch(target, type, init = {}) {
      const event = {
        type, target, defaultPrevented: false, bubbles: init.bubbles !== false, ...init,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; },
      };
      for (const { fn, capture } of document.listeners[type] || []) if (capture) fn(event);
      const path = [];
      for (let node = target; node instanceof FakeElement; node = node.parent) path.push(node);
      for (const node of event.bubbles ? path : path.slice(0, 1)) {
        for (const fn of node.listeners[type] || []) { event.currentTarget = node; fn(event); }
        if (event.stopped) return event;
      }
      if (event.bubbles) for (const { fn, capture } of document.listeners[type] || []) if (!capture) fn(event);
      return event;
    },
  };
  document.root = new FakeElement("html", document);
  document.body = new FakeElement("body", document);
  document.root.append(document.body);
  document.activeElement = document.body;
  return document;
};

// Build elements from a tiny description: ["tag", {attributes}, ...children].
export const build = (document, spec) => {
  if (typeof spec === "string") return spec;
  const [tag, attributes = {}, ...children] = spec;
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  node.append(...children.map((child) => build(document, child)));
  return node;
};

export const fakeTimers = () => {
  let now = 1_800_000_000_000;
  let next = 1;
  const timers = new Map();
  const api = {
    now: () => now,
    setTimeout: (fn, ms = 0) => { timers.set(next, { at: now + ms, fn, every: 0 }); return next++; },
    setInterval: (fn, ms) => { timers.set(next, { at: now + ms, fn, every: ms }); return next++; },
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.every) timer.at += timer.every; else timers.delete(id);
        timer.fn();
      }
      now = end;
    },
  };
  return api;
};

export const memoryStorage = (data = new Map()) => ({
  data,
  get length() { return data.size; },
  key: (index) => [...data.keys()][index] ?? null,
  getItem: (key) => (data.has(key) ? data.get(key) : null),
  setItem: (key, value) => data.set(key, String(value)),
  removeItem: (key) => data.delete(key),
});
