// Recent messages: the result of an action - a message the server left after a redirect, a
// failure app.js met, a check's result - is shown once in the strip at the top of <main>, then
// collapses into the header's message icon, coloured by the worst unread one, and stays in this
// tab's history (sessionStorage). Every message goes through window.towMessages.showMessage(record).
// The words a screen reader says come only from the page's two empty live regions, once per
// message: never again on a reload, from the history or from a countdown.
(() => {
  const KINDS = ["ok", "info", "warn", "err"];
  // Time in the foreground before the strip collapses (paused while hovered, focused or hidden).
  const READ_MS = { ok: 10000, info: 10000, warn: 25000, err: 30000 };
  const RANK = { info: 0, ok: 1, warn: 2, err: 3 };
  const LIMIT = 50;
  const MAX_AGE_MS = 24 * 3600 * 1000;
  const PREFIX = "tow.messages.";

  const clean = (value, max) => String(value ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ")
    .trim().slice(0, max);
  // What is stored is plain display words: a link keeps only its host, a path goes.
  const scrub = (value, max = 600) => clean(String(value ?? "")
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>]+/gi, (url) => {
      try {
        return new URL(url).host || "…";
      } catch {
        return "…";
      }
    })
    .replace(/\b[A-Za-z]:\\[^\s"'<>]*/g, "…")
    .replace(/\\\\[^\s"'<>]+/g, "…")
    .replace(/(^|[\s(«"'])\/(?:[^\s/"'<>]+\/)+[^\s"'<>]*/g, "$1…"), max);
  // A message links only to a page of this TOW.
  const localHref = (href) => {
    const value = String(href ?? "");
    return value.startsWith("/") && !value.startsWith("//") && !/[\\\s\u0000-\u001f]/.test(value) ? value.slice(0, 500) : "";
  };
  // "{subject} — {result}" (the server's catalog key, messages.message_text), never the subject
  // twice. Said on its own the result starts as a sentence - unless its words start with a name
  // (``named``, the server's record says so: "qBittorrent …", "nnmclub: …" never get a capital).
  const sentence = (text, named) => (named ? text : text.slice(0, 1).toUpperCase() + text.slice(1));
  const format = (pattern, subject, text, named = false) => {
    if (!subject || text.toLowerCase().startsWith(`${subject} — `.toLowerCase())) return sentence(text, named);
    return String(pattern || "{subject} — {result}").replace("{subject}", () => subject).replace("{result}", () => text);
  };

  // The history and the strip's timer, without the page: tests drive it with a fake clock.
  const createCenter = ({
    now = () => Date.now(),
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id),
    storage = null,
    key = `${PREFIX}local`,
    announce = () => {},
    changed = () => {},
  } = {}) => {
    let memoryOnly = !storage;
    let hidden = false;
    let active = null; // { id, left, holds, timer, since }
    const pending = []; // ids that wait for the strip while it is in use
    const words = new Map(); // id -> the unscrubbed words of a message shown now (never stored)

    const normalize = (raw, stamp = now()) => {
      const at = Number(raw?.at);
      return {
        id: clean(raw?.id, 80) || `m${stamp.toString(36)}${Math.random().toString(36).slice(2, 8)}`,
        kind: KINDS.includes(raw?.kind) ? raw.kind : "ok",
        subject: scrub(raw?.subject, 200),
        text: scrub(raw?.text),
        named: raw?.named === true || raw?.named === "1",
        at: at > 0 ? (at < 1e11 ? at * 1000 : at) : stamp,
        href: localHref(raw?.href),
        link: clean(raw?.link, 80),
        cancel: localHref(raw?.cancel),
        undo: clean(raw?.undo, 64),
        undoUntil: Number(raw?.undoUntil) || 0,
        read: raw?.read === true,
        announced: raw?.announced === true,
      };
    };
    const load = () => {
      if (memoryOnly) return [];
      try {
        // Another install or another sign-in in this tab: its messages go.
        for (const name of storage.keys()) if (name && name.startsWith(PREFIX) && name !== key) storage.remove(name);
        const data = JSON.parse(storage.get(key) || "null");
        return Array.isArray(data?.items) ? data.items.slice(0, LIMIT).map((item) => normalize(item)) : [];
      } catch {
        memoryOnly = true; // blocked or damaged: this page keeps its messages in memory
        return [];
      }
    };
    let items = load();
    const save = () => {
      if (memoryOnly) return;
      try {
        storage.set(key, JSON.stringify({ v: 1, items }));
      } catch {
        memoryOnly = true;
      }
    };
    const find = (id) => items.find((item) => item.id === id) || null;
    const undoLive = (item, at = now()) => Boolean(item?.undo) && item.undoUntil > at;
    // 50 messages of the last 24 hours; the message with a live "Undo" stays.
    const prune = () => {
      const at = now();
      items = items.filter((item) => undoLive(item, at) || at - item.at < MAX_AGE_MS || item.id === active?.id);
      while (items.length > LIMIT) {
        const index = items.findLastIndex((item) => !undoLive(item, at) && item.id !== active?.id);
        if (index < 0) break;
        items.splice(index, 1);
      }
    };
    const startTimer = () => {
      if (!active || active.holds.size || active.timer) return;
      active.since = now();
      const id = active.id;
      active.timer = setTimer(() => {
        if (active?.id !== id) return;
        active.timer = 0;
        collapse("timer");
      }, Math.max(0, active.left));
    };
    const stopTimer = () => {
      if (!active?.timer) return;
      clearTimer(active.timer);
      active.timer = 0;
      active.left -= now() - active.since;
    };
    const display = (item) => {
      if (active) collapse("replaced", false);
      active = { id: item.id, left: READ_MS[item.kind], holds: new Set(hidden ? ["hidden"] : []), timer: 0, since: 0 };
      startTimer();
    };
    const inUse = () => Boolean(active && (active.holds.has("pointer") || active.holds.has("focus")));
    const next = () => {
      while (pending.length && !inUse()) {
        const following = find(pending.shift());
        if (following) {
          display(following);
          return;
        }
      }
    };
    // how: "timer", "closed" (× - read), "panel" (the history opened: it shows them all), "replaced".
    const collapse = (how = "timer", follow = true) => {
      if (!active) return;
      const item = find(active.id);
      stopTimer();
      active = null;
      if (item && how === "closed") item.read = true;
      if (how === "panel") pending.length = 0;
      else if (follow) next();
      save();
      changed();
    };
    const show = (raw, { replace = false, quiet = false } = {}) => {
      const item = normalize(raw);
      const known = find(item.id);
      // A page opened again (reload, Back) brings its message from the history, not as news.
      if (known && !replace) return known;
      if (known) items.splice(items.indexOf(known), 1);
      if (known && quiet) { // an update in place (a check's progress): not news again
        item.read = known.read;
        item.announced = known.announced;
      }
      if (item.undo) items.forEach((other) => { other.undo = ""; }); // one live undo: the newest change's
      items.unshift(item);
      words.set(item.id, { subject: clean(raw?.subject, 200), text: clean(raw?.text, 1000) });
      prune();
      if (!quiet) {
        item.announced = true;
        announce(item, words.get(item.id));
      }
      if (active?.id === item.id) {
        if (!quiet) { // a new result in place: a new reading time
          stopTimer();
          active.left = READ_MS[item.kind];
          startTimer();
        }
      } else if (quiet) {
        // Updated where it is: a collapsed message stays collapsed, nothing pops up.
      } else if (inUse()) {
        if (!pending.includes(item.id)) pending.push(item.id); // never replaced under the pointer or the focus
      } else {
        display(item);
      }
      save();
      changed();
      return item;
    };
    const hold = (reason) => {
      if (reason === "hidden") hidden = true;
      if (!active) return;
      stopTimer();
      active.holds.add(reason);
    };
    const release = (reason) => {
      if (reason === "hidden") hidden = false;
      if (!active || !active.holds.delete(reason)) return;
      if (pending.length && !inUse()) next();
      else startTimer();
    };
    const markRead = (ids) => {
      let any = false;
      for (const id of ids) {
        const item = find(id);
        if (item && !item.read) {
          item.read = true;
          any = true;
        }
      }
      if (any) {
        save();
        changed();
      }
    };
    // A message that another one replaces (a lost check's placeholder, by its result).
    const remove = (id) => {
      if (!find(id)) return;
      if (active?.id === id) collapse("cleared", false);
      items = items.filter((item) => item.id !== id);
      words.delete(id);
      save();
      changed();
    };
    const dismiss = (id) => {
      if (active?.id === id) collapse("closed");
      else markRead([id]);
    };
    // Mark all read; clear the history (the message whose "Undo" still works stays, read).
    const markAllRead = () => markRead(items.map((item) => item.id));
    const clear = () => {
      const at = now();
      items = items.filter((item) => undoLive(item, at));
      items.forEach((item) => { item.read = true; });
      pending.length = 0;
      if (active && !find(active.id)) collapse("cleared", false);
      save();
      changed();
    };
    // The undo the server has now (base.html): a message whose undo it no longer has (used,
    // replaced, run out) loses its button; without a message for it (a page opened later) one is
    // added, already read.
    const setUndo = ({ stamp = "", until = 0, text = "" } = {}) => {
      let current = null;
      for (const item of items) {
        if (!item.undo) continue;
        if (stamp && item.undo === stamp) {
          item.undoUntil = until;
          current = item;
        } else {
          item.undo = "";
        }
      }
      if (stamp && until > now() && !current && text) {
        items.unshift(normalize({ id: `undo-${stamp}`, kind: "info", text, undo: stamp, undoUntil: until, read: true, announced: true }));
      }
      prune();
      save();
      changed();
    };
    const unread = () => items.filter((item) => !item.read);
    return {
      show,
      remove,
      dismiss,
      collapse,
      hold,
      release,
      markRead,
      markAllRead,
      clear,
      setUndo,
      find,
      undoLive: (item) => undoLive(item),
      liveUndo: () => items.find((item) => undoLive(item)) || null,
      items: () => items.slice(),
      active: () => active?.id || "",
      unread: () => unread().length,
      worst: () => unread().reduce((worst, item) => (RANK[item.kind] > RANK[worst] ? item.kind : worst), "info"),
      words: (id) => words.get(id) || { subject: find(id)?.subject || "", text: find(id)?.text || "" },
      stored: () => !memoryOnly,
      // Signed out: this tab's messages go, of every install and sign-in.
      clearStorage: () => {
        items = [];
        pending.length = 0;
        if (active) collapse("cleared", false);
        // Also when a failed write made the history keep its messages in memory only.
        if (!storage) return;
        let names = [key];
        try {
          names = [...new Set([key, ...storage.keys().filter((name) => name && name.startsWith(PREFIX))])];
        } catch {
          // the list cannot be read: this tab's own key at least
        }
        names.forEach((name) => {
          try {
            storage.remove(name);
          } catch {
            // nothing stored, or it cannot be removed
          }
        });
      },
    };
  };
  window.TowMessageCenter = createCenter;
  window.TowScrub = scrub; // checks.js stores a pending check's title with the same scrubbing

  const host = document.getElementById("msg-host");
  if (!host) return;
  const I18N = (() => {
    try {
      return JSON.parse(document.getElementById("tow-i18n")?.textContent || "{}") || {};
    } catch {
      return {};
    }
  })();
  const t = (name, vars = {}) => String(I18N[name] ?? name).replace(
    /\{(\w+)\}/g,
    (whole, field) => (Object.prototype.hasOwnProperty.call(vars, field) ? String(vars[field]) : whole),
  );
  const pattern = host.dataset.subjectFormat || "{subject} — {result}";
  const KIND_WORDS = {
    ok: t("js.messages.kind_ok"), info: t("js.messages.kind_info"),
    warn: t("js.messages.kind_warn"), err: t("js.messages.kind_err"),
  };
  const ICONS = { ok: "check", info: "info", warn: "alert", err: "error" };
  const CLASSES = { ok: "ok", info: "info", warn: "warn", err: "error" };
  const SVG = "http://www.w3.org/2000/svg";

  const menu = document.querySelector("[data-messages]");
  const toggle = menu?.querySelector("[data-messages-toggle]");
  const count = menu?.querySelector("[data-messages-count]");
  const panel = menu?.querySelector("[data-messages-panel]");
  const list = menu?.querySelector("[data-messages-list]");
  const empty = menu?.querySelector("[data-messages-empty]");

  const storage = (() => {
    try {
      const store = window.sessionStorage;
      return {
        get: (name) => store.getItem(name),
        set: (name, value) => store.setItem(name, value),
        remove: (name) => store.removeItem(name),
        keys: () => Array.from({ length: store.length }, (_, index) => store.key(index)),
      };
    } catch {
      return null;
    }
  })();

  // The session of this page's messages: a new one once the owner signs out (or TOW signs the
  // owner out). An answer, a poll or an announcement of the old one does nothing any more.
  let session = 0;
  const saying = new Set();
  const announce = (item, said) => {
    const region = document.getElementById(item.kind === "err" ? "announce-alert" : "announce");
    if (!region) return;
    const prefix = item.kind === "warn" || item.kind === "err" ? `${KIND_WORDS[item.kind]}: ` : "";
    const sentenceSaid = prefix + format(pattern, said.subject, said.text, item.named);
    region.textContent = "";
    // A moment later: a live region changed while the page opens is not said by every reader.
    const mine = session;
    const timer = window.setTimeout(() => {
      saying.delete(timer);
      if (mine === session) region.textContent = sentenceSaid;
    }, 400);
    saying.add(timer);
  };
  // Signed out: a new session; nothing of the old one is said, kept or shown.
  const signOut = () => {
    session += 1;
    saying.forEach((timer) => window.clearTimeout(timer));
    saying.clear();
    ["announce", "announce-alert"].forEach((name) => {
      const region = document.getElementById(name);
      if (region) region.textContent = "";
    });
    center.clearStorage();
    render();
  };

  let render = () => {};
  const center = createCenter({
    storage,
    key: `${PREFIX}${host.dataset.namespace || "local"}`,
    announce,
    changed: () => render(),
  });

  const element = (tag, className = "", text = null) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null) node.textContent = text;
    return node;
  };
  const icon = (name) => {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "i sm");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };
  const leftText = (item) => {
    const seconds = Math.max(0, Math.ceil((item.undoUntil - Date.now()) / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  // "Undo" of the change a message is about: it sends that change's stamp (a newer change since
  // is refused by the server, never undone) and counts down the time it has left.
  const undoForm = (item) => {
    const form = element("form", "flash-undo");
    form.setAttribute("method", "post");
    form.setAttribute("action", "/undo");
    const stamp = element("input");
    stamp.type = "hidden";
    stamp.name = "stamp";
    stamp.value = item.undo;
    const button = element("button", "ghost sm");
    button.type = "submit";
    button.dataset.keep = "";
    button.title = t("js.messages.undo_title");
    const left = element("span", "undo-left", leftText(item));
    left.setAttribute("aria-hidden", "true");
    left.dataset.undoLeft = item.id;
    button.append(`${t("js.messages.undo")} `, left);
    form.append(stamp, button);
    return form;
  };
  const link = (item) => {
    const anchor = element("a", "msg-link", item.link || t("js.messages.details"));
    anchor.setAttribute("href", item.href);
    return anchor;
  };

  // A message's links and buttons (Details, Undo), in one place the strip and the history share.
  // A waiting check's Cancel: the same POST its page had (/check/<job>/cancel).
  const cancelForm = (item) => {
    const form = element("form", "msg-cancel");
    form.setAttribute("method", "post");
    form.setAttribute("action", item.cancel);
    const button = element("button", "ghost sm", t("js.messages.cancel"));
    button.type = "submit";
    form.append(button);
    return form;
  };
  const actionShape = (item) => `${item.href}|${item.link}|${center.undoLive(item)}|${item.cancel || ""}`;
  const actionsOf = (item) => {
    const box = element("span", "msg-actions");
    box.dataset.shape = actionShape(item);
    if (item.href) box.append(link(item));
    if (item.cancel) box.append(cancelForm(item));
    if (center.undoLive(item)) box.append(undoForm(item));
    return box;
  };

  const strip = () => host.querySelector("#flash");
  // The strip's words, colour and actions for ``item``: a message updated in place (a check's
  // progress, then its result) keeps its element and its focus; a control that goes while it
  // has the focus hands it to the strip's x.
  const fillStrip = (box, item) => {
    const said = center.words(item.id);
    box.className = `flash ${CLASSES[item.kind]}`;
    box.dataset.kind = item.kind;
    const kind = box.querySelector(".msg-ico");
    kind.setAttribute("aria-label", KIND_WORDS[item.kind]);
    kind.replaceChildren(icon(ICONS[item.kind]));
    const words = format(pattern, said.subject, said.text, item.named);
    const text = box.querySelector(".msg-text");
    if (text.textContent !== words) text.textContent = words;
    const old = box.querySelector(":scope > .msg-actions");
    if (old?.dataset.shape === actionShape(item)) return;
    const focused = old?.contains(document.activeElement);
    const fresh = actionsOf(item);
    if (old) old.replaceWith(fresh);
    else box.querySelector(".flash-x").before(fresh);
    if (focused) box.querySelector(".flash-x").focus();
  };
  const drawStrip = () => {
    const id = center.active();
    const current = strip();
    const item = id ? center.find(id) : null;
    if (!item) {
      if (current) {
        const hadFocus = current.contains(document.activeElement);
        current.remove();
        if (hadFocus) (menu && !menu.hidden ? toggle : document.getElementById("main"))?.focus?.();
      }
      return;
    }
    if (current?.dataset.messageId === id) {
      fillStrip(current, item);
      // A focus that is no longer in the strip no longer holds its time.
      if (!current.contains(document.activeElement)) center.release("focus");
      return;
    }
    const box = element("div", "flash");
    box.id = "flash";
    box.dataset.messageId = id;
    const kind = element("span", "msg-ico");
    kind.setAttribute("role", "img");
    const close = element("button", "flash-x");
    close.type = "button";
    close.setAttribute("aria-label", t("js.messages.collapse"));
    close.title = t("js.messages.collapse");
    close.append(icon("close"));
    close.addEventListener("click", () => center.dismiss(id));
    box.addEventListener("pointerenter", () => center.hold("pointer"));
    box.addEventListener("pointerleave", () => center.release("pointer"));
    box.addEventListener("focusin", () => center.hold("focus"));
    box.addEventListener("focusout", (event) => {
      if (!box.contains(event.relatedTarget)) center.release("focus");
    });
    box.append(kind, element("span", "msg-text"), close);
    fillStrip(box, item);
    if (current) current.replaceWith(box);
    else host.prepend(box);
  };

  // When a message was said, by the one time rule (app.js's towTime: TOW's zone, "20:31",
  // "15 Oct, 20:31"); the browser's own words only on a page without app.js.
  const when = (at) => {
    if (window.towTime) return window.towTime.element(at);
    const moment = new Date(at);
    const lang = document.documentElement.lang || undefined;
    const today = new Date().toDateString() === moment.toDateString();
    const options = today ? { hour: "2-digit", minute: "2-digit" } : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" };
    const node = element("time", "", moment.toLocaleString(lang, options));
    node.setAttribute("datetime", moment.toISOString());
    return node;
  };
  // One entry of the history; patched in place when its message changes (a check's progress,
  // then its result), so the open panel shows what the strip shows.
  const entrySign = (item) => `${item.kind}|${item.subject}|${item.text}|${item.at}|${actionShape(item)}`;
  const fillEntry = (row, item) => {
    row.className = `msg-entry ${item.kind}${item.read ? "" : " unread"}`;
    row.dataset.sign = entrySign(item);
    const head = element("div", "msg-entry-head");
    head.append(icon(ICONS[item.kind]), element("span", "msg-entry-kind", KIND_WORDS[item.kind]), when(item.at));
    const text = element("p", "msg-entry-text", format(pattern, item.subject, item.text, item.named));
    const oldHead = row.querySelector(":scope > .msg-entry-head");
    const oldText = row.querySelector(":scope > .msg-entry-text");
    if (oldHead) oldHead.replaceWith(head);
    else row.append(head);
    if (oldText) oldText.replaceWith(text);
    else row.append(text);
    const old = row.querySelector(":scope > .msg-actions");
    if (old?.dataset.shape === actionShape(item)) return;
    const focused = old?.contains(document.activeElement);
    const fresh = actionsOf(item);
    fresh.classList.add("msg-entry-actions");
    if (old) old.replaceWith(fresh);
    else row.append(fresh);
    if (focused) (fresh.querySelector("a, button") || toggle)?.focus();
  };
  const entry = (item) => {
    const row = element("li");
    row.dataset.messageId = item.id;
    fillEntry(row, item);
    return row;
  };
  let listShape = "";
  const drawList = () => {
    if (!list || !menu?.open) return;
    const items = center.items();
    const shape = items.map((item) => item.id).join("|");
    let changed = false;
    if (shape !== listShape) {
      const focusedId = list.contains(document.activeElement) ? document.activeElement.closest?.("[data-message-id]")?.dataset.messageId : "";
      listShape = shape;
      const rows = items.map(entry);
      list.replaceChildren(...rows);
      if (focusedId) (rows.find((row) => row.dataset.messageId === focusedId)?.querySelector("a, button") || toggle)?.focus();
      changed = true;
    } else {
      list.querySelectorAll("li[data-message-id]").forEach((row) => {
        const item = center.find(row.dataset.messageId);
        if (item && row.dataset.sign !== entrySign(item)) {
          fillEntry(row, item);
          changed = true;
        }
      });
    }
    list.querySelectorAll("li[data-message-id]").forEach((row) => {
      row.classList.toggle("unread", !center.find(row.dataset.messageId)?.read);
    });
    if (empty) empty.hidden = items.length > 0;
    // A new or changed entry is read only once the open panel shows it.
    if (changed) window.setTimeout(() => markShown(), 300);
  };
  const drawIcon = () => {
    if (!menu || !toggle) return;
    const unread = center.unread();
    const worst = center.worst();
    // Shown while there is a message, a live Undo, or a check this tab still follows (checks.js).
    const shown = center.items().length > 0 || Boolean(center.liveUndo()) || (window.towChecks?.count?.() || 0) > 0;
    if (!shown && menu.contains(document.activeElement)) document.getElementById("log-open")?.focus();
    if (!shown) menu.open = false;
    menu.hidden = !shown;
    KINDS.forEach((kind) => menu.classList.toggle(kind, unread > 0 && worst === kind));
    menu.classList.toggle("muted", unread === 0);
    if (count) {
      count.hidden = unread === 0;
      count.textContent = unread > 99 ? "99+" : String(unread);
    }
    const label = unread ? t("js.messages.label_unread", { count: unread }) : t("js.messages.title");
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  };

  // One countdown for every "Undo" on the page; a run-out one goes (the server refuses it anyway).
  let ticking = 0;
  const tick = () => {
    document.querySelectorAll("[data-undo-left]").forEach((node) => {
      const item = center.find(node.dataset.undoLeft);
      if (item) node.textContent = leftText(item);
    });
    if (!center.liveUndo() || [...document.querySelectorAll("[data-undo-left]")].some((node) => !center.undoLive(center.find(node.dataset.undoLeft)))) {
      render();
    }
  };
  const keepTicking = () => {
    if (center.liveUndo() && !ticking) ticking = window.setInterval(tick, 1000);
    else if (!center.liveUndo() && ticking) {
      window.clearInterval(ticking);
      ticking = 0;
    }
  };

  render = () => {
    drawStrip();
    drawIcon();
    drawList();
    keepTicking();
  };

  // Entries are read when the open panel shows them, not because it opened (one below its fold
  // stays unread).
  const markShown = () => {
    if (!menu?.open || !panel || !list) return;
    const view = panel.getBoundingClientRect();
    const seen = [];
    list.querySelectorAll("li.unread[data-message-id]").forEach((row) => {
      const box = row.getBoundingClientRect();
      if (box.height > 0 && box.bottom > view.top && box.top < view.bottom) seen.push(row.dataset.messageId);
    });
    center.markRead(seen);
  };
  if (menu && toggle) {
    let scrolled = 0;
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      center.collapse("panel"); // the strip's message is in the list now: not shown twice
      listShape = "";
      drawList();
      window.setTimeout(markShown, 300);
    });
    panel?.addEventListener("scroll", () => {
      window.clearTimeout(scrolled);
      scrolled = window.setTimeout(markShown, 150);
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !menu.open) return;
      event.preventDefault();
      event.stopPropagation();
      menu.open = false;
      toggle.focus();
    });
    document.addEventListener("pointerdown", (event) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("focusout", (event) => {
      if (event.relatedTarget && !menu.contains(event.relatedTarget)) menu.open = false;
    });
    menu.querySelector("[data-messages-read]")?.addEventListener("click", () => center.markAllRead());
    menu.querySelector("[data-messages-clear]")?.addEventListener("click", () => {
      center.clear();
      toggle.focus();
    });
  }
  // One header panel open at a time: the recent messages or "Needs attention".
  document.addEventListener("toggle", (event) => {
    const opened = event.target;
    if (!opened?.open || !opened.matches?.("[data-messages], [data-attention]")) return;
    document.querySelectorAll("[data-messages][open], [data-attention][open]").forEach((other) => {
      if (other !== opened) other.open = false;
    });
  }, true);

  // The strip stays in sight under the sticky header while it is shown: a page opened further down
  // (Settings at a card, a row) still shows its result.
  const header = document.querySelector("header.app");
  const place = () => {
    if (header?.getBoundingClientRect) host.style?.setProperty("--msg-top", `${Math.ceil(header.getBoundingClientRect().height)}px`);
  };
  place();
  window.addEventListener?.("resize", place);

  document.addEventListener("visibilitychange", () => (document.hidden ? center.hold("hidden") : center.release("hidden")));
  if (document.hidden) center.hold("hidden");
  // Signing out ends this tab's history with the session.
  document.addEventListener("submit", (event) => {
    if (event.target?.getAttribute?.("action") === "/logout") signOut(); // checks.js stops too
  });

  // The page as the server drew it: its message, its undo; then the address no longer names the
  // message, so a reload does not bring it back as news.
  const stamp = host.dataset.undoStamp || "";
  const left = Number(host.dataset.undoLeft) || 0;
  const until = stamp && left > 0 ? Date.now() + left * 1000 : 0;
  const served = strip();
  if (served) {
    const data = served.dataset;
    const record = {
      id: data.messageId, kind: data.kind, subject: data.subject, text: data.text, at: data.at, named: data.named,
      href: data.href, link: data.link, undo: data.undo || "", undoUntil: data.undo && data.undo === stamp ? until : 0,
    };
    served.remove();
    if (record.text) center.show(record);
  }
  center.setUndo({ stamp, until, text: host.dataset.undoText || "" });
  // Without a script the header keeps its own "Undo"; with one it is the message's.
  const headerUndo = document.querySelector("header.app #undo-form");
  if (headerUndo) headerUndo.hidden = true;
  try {
    const address = new URL(location.href);
    if (address.searchParams.has("flash")) {
      address.searchParams.delete("flash");
      history.replaceState(history.state, "", address.pathname + address.search + address.hash);
    }
  } catch {
    // the address stays as it was
  }
  render();

  window.towMessages = {
    // The one way to show a message: { id?, kind: ok|info|warn|err, subject?, text, href?, link?,
    // undo?, undoUntil? }; options { replace, quiet } update a message in place.
    showMessage: (record, options) => {
      const item = center.show(record, options);
      if (menu?.open) center.collapse("panel");
      return item;
    },
    dismiss: (id) => center.dismiss(id),
    remove: (id) => center.remove(id),
    // The session ended: this tab's history goes (checks.js stops following its checks too).
    signOut,
    session: () => session,
    refresh: () => render(),
    center,
  };
})();
