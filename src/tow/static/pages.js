// Sites, Settings and History follow another device or TOW while they are open (tow.web.page_sync):
// when the page area of /sync.json names another revision, the page asks /page/fragments for what
// it draws now and puts in only what differs - a Sites row, a Settings section, History's newest
// lines - never over what the owner is working in.
//
// Components. A script that attaches listeners to an element or keeps it (a form's unsaved state,
// the theme's background save, a file chooser, a row's icons) registers it here with
// ``towLifecycle.register(name, selector, mount)``: ``mount(element)`` runs once per element (at
// once for the page's own, and for every element put in later) and returns its cleanup, which runs
// when the element is taken out. Mounting twice does nothing; a cleanup runs once. What is
// delegated on the document (a secret's eye, Cancel, the confirmations) needs nothing.
//
// Protection. A fragment the owner is in is never replaced or removed: a form with unsaved words, a
// form being sent or saved, a refused save waiting for a choice, a field with the focus, the focus
// anywhere in it while the owner used it in the last 10 s, the pointer pressed in it, or any open
// dialog (a confirmation answers for the form behind it). Such a fragment says that it changed
// elsewhere and follows once the owner leaves it. A section of Settings in use still takes its
// other cards and its summary when only its cards are in use. What is kept as the owner left it:
// which sections, cards and details are open, the focus, the scroll position, Settings' search,
// History's filters (the server's request variant), its bounds and one line per event.
(() => {
  const TOUCH_MS = 10000;
  const HASH = /^[0-9a-f]{32}$/;

  // ------------------------------------------------------------------ components, without the page
  // ``root``: where ``register`` mounts at once (the document); tests give their own.
  const createLifecycle = ({ root = () => (typeof document === "undefined" ? null : document) } = {}) => {
    const components = new Map(); // name -> {selector, mount}
    const live = new Map(); // element -> Map(name -> cleanup)
    const within = (node, selector) => {
      const found = [];
      if (!node || typeof node !== "object") return found;
      if (node.matches?.(selector)) found.push(node);
      node.querySelectorAll?.(selector)?.forEach?.((element) => found.push(element));
      return found;
    };
    const cleanup = (element) => {
      const byName = live.get(element);
      if (!byName) return 0;
      live.delete(element);
      let ran = 0;
      byName.forEach((done) => {
        ran += 1;
        try {
          done?.();
        } catch {
          // one broken cleanup never stops the others
        }
      });
      return ran;
    };
    const mountOne = (name, spec, element) => {
      let byName = live.get(element);
      if (byName?.has(name)) return false;
      if (!byName) live.set(element, (byName = new Map()));
      let done = null;
      try {
        done = spec.mount(element) || null;
      } catch {
        done = null; // the element stays as the server drew it
      }
      byName.set(name, typeof done === "function" ? done : null);
      return true;
    };
    // Elements taken out without an unmount (a script of their own): their cleanups run too.
    const prune = () => [...live.keys()].forEach((element) => { if (element.isConnected === false) cleanup(element); });
    return {
      register: (name, selector, mount) => {
        const spec = { selector, mount };
        components.set(name, spec);
        within(root(), selector).forEach((element) => mountOne(name, spec, element));
      },
      mount: (node) => {
        prune();
        let mounted = 0;
        components.forEach((spec, name) => within(node, spec.selector).forEach((element) => { if (mountOne(name, spec, element)) mounted += 1; }));
        return mounted;
      },
      unmount: (node) => {
        let ran = 0;
        [...live.keys()].forEach((element) => {
          if (element === node || node?.contains?.(element)) ran += cleanup(element);
        });
        return ran;
      },
      mounted: (element, name) => Boolean(live.get(element)?.has(name)),
      size: () => live.size,
    };
  };

  // ------------------------------------------------------------------ a keyed list, without the page
  // ``current``: what the page shows, in its order ({key, hash}; hash "" for a line drawn before its
  // digest was known); ``wanted``: the server's, in its order ({id, hash, html}). Duplicates go, a
  // fragment the server no longer has goes, a changed one is drawn again, a new one comes after the
  // one before it in the server's order - except under the owner's hands (``isProtected``): kept,
  // with a note, and ``waiting`` says so.
  const reconcile = ({
    current,
    wanted,
    isProtected = () => false,
    replace,
    insert, // (fragment, previousKey | null)
    remove,
    note = () => {}, // (item, "changed" | "removed" | "")
    adopt = () => {}, // (item, fragment): the page's own drawing of it, its digest now known
    partial = () => {}, // (item, fragment): what of a protected one may change (Settings' cards)
  }) => {
    const result = { placed: [], removed: [], waiting: [], kept: [] };
    const want = new Map();
    wanted.forEach((fragment) => {
      if (fragment && typeof fragment.id === "string" && typeof fragment.html === "string" && !want.has(fragment.id)) want.set(fragment.id, fragment);
    });
    const present = new Map();
    for (const item of current) {
      if (present.has(item.key)) {
        remove(item); // one line per event, one row per site
        result.removed.push(item.key);
        continue;
      }
      if (want.has(item.key)) {
        present.set(item.key, item);
      } else if (isProtected(item)) {
        note(item, "removed");
        result.kept.push(item.key);
      } else {
        remove(item);
        result.removed.push(item.key);
      }
    }
    let previous = null;
    for (const fragment of want.values()) {
      const item = present.get(fragment.id);
      if (!item) {
        insert(fragment, previous);
        result.placed.push(fragment.id);
      } else if (!item.hash) {
        adopt(item, fragment);
      } else if (item.hash !== fragment.hash) {
        if (isProtected(item)) {
          partial(item, fragment);
          note(item, "changed");
          result.waiting.push(fragment.id);
        } else {
          replace(item, fragment);
          result.placed.push(fragment.id);
        }
      } else {
        note(item, "");
      }
      previous = fragment.id;
    }
    return result;
  };

  // ------------------------------------------------------------------ History's lines, without the page
  // The keys History shows once the server's newest window is merged in: the window's, in its order
  // - a line once, never more than ``limit`` - and lines under the owner's hands that it no longer
  // has stay where they were.
  const mergeHistory = (shown, entries, limit, isProtected = () => false) => {
    const lines = shown.map((key) => ({ key, hash: "h" }));
    const out = [...lines];
    const window = entries.slice(0, limit);
    reconcile({
      current: lines,
      wanted: window,
      isProtected: (item) => isProtected(item.key),
      replace: () => {},
      insert: (fragment, previousKey) => {
        const at = previousKey === null ? 0 : out.findIndex((item) => item.key === previousKey) + 1;
        out.splice(at, 0, { key: fragment.id, hash: fragment.hash });
      },
      remove: (item) => {
        const at = out.indexOf(item);
        if (at >= 0) out.splice(at, 1);
      },
    });
    return out.map((item) => item.key);
  };

  // ------------------------------------------------------------------ the page area, without the page
  // ``page(payload)``: the page area of /sync.json; the fragments of its revision are fetched and
  // applied whole (a newer revision's too: the server drew all of it from one sample). The area
  // counts as applied once they are on the page (``ack``); what waits for the owner is applied again
  // when ``settle`` finds it free.
  // A request that failed (offline, an error, no answer in 10 s) is asked again by ``settle`` - after
  // every successful poll, also a 304 that brings the area no more - 5, 10, 20, 40, then every 60 s
  // after the failure, never sooner; a newer revision is asked for at once.
  const BACKOFF_MS = [5000, 10000, 20000, 40000, 60000];
  const createPageSync = ({ fetchFragments, apply, ack = () => {}, signedOut = () => {}, retry = () => {}, now = () => Date.now() }) => {
    let generation = 0;
    let asking = 0; // the generation whose request is on its way
    let last = null; // {body, result}
    let failed = null; // {payload, failures, at}
    const page = async (payload) => {
      if (!payload || typeof payload.rev !== "string" || !HASH.test(payload.rev)) return false;
      generation += 1;
      const mine = generation;
      const before = failed?.payload.rev === payload.rev ? failed.failures : 0;
      asking = mine;
      let answer = null;
      try {
        answer = await fetchFragments(payload.rev);
      } catch {
        answer = null;
      }
      if (asking === mine) asking = 0;
      if (mine !== generation) return false;
      if (answer?.status === 401 || answer?.status === 403) {
        generation += 1;
        failed = null;
        signedOut(answer.status);
        return false;
      }
      const body = answer?.status === 200 ? answer.body : null;
      if (!body || body.ok !== true || typeof body.rev !== "string") {
        failed = { payload, failures: before + 1, at: now() + BACKOFF_MS[Math.min(before + 1, BACKOFF_MS.length) - 1] };
        return false;
      }
      failed = null;
      const result = apply(body);
      last = { body, result };
      ack(body.rev);
      if (body.stale) retry();
      return true;
    };
    const settle = () => {
      let did = false;
      if (failed && !asking && now() >= failed.at) {
        page(failed.payload).catch(() => {});
        did = true;
      }
      if (!last || !(last.result?.waiting?.length || last.result?.kept?.length)) return did;
      last = { body: last.body, result: apply(last.body) };
      return true;
    };
    return {
      page,
      settle,
      cancel: () => { generation += 1; asking = 0; last = null; failed = null; },
      waiting: () => [...(last?.result?.waiting || []), ...(last?.result?.kept || [])],
    };
  };

  const lifecycle = createLifecycle();
  window.TowPages = { createLifecycle, reconcile, mergeHistory, createPageSync };
  window.towLifecycle = lifecycle;
  if (typeof document === "undefined" || !window.towSync || !document.getElementById?.("main")) return;

  // ------------------------------------------------------------------ the page
  const I18N = (() => {
    try {
      return JSON.parse(document.getElementById("tow-i18n")?.textContent || "{}") || {};
    } catch {
      return {};
    }
  })();
  const t = (key) => String(I18N[key] ?? key);
  const main = document.getElementById("main");
  const list = (() => {
    const walker = document.createTreeWalker(main, NodeFilter.SHOW_COMMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const found = /^frags:(\/[a-z]+):([0-9a-f]{32})$/.exec(node.data);
      if (found) return { page: found[1], rev: found[2], container: node.parentNode };
    }
    const history = document.getElementById("history-log");
    if (history) return { page: "/history", rev: "", container: history };
    return null;
  })();
  if (!list) return;
  const filters = list.page === "/history" ? {
    group: list.container.dataset.historyGroup || "",
    q: list.container.dataset.historyQ || "",
  } : {};
  if ((filters.q || "").length > 200) return; // the server follows no longer search: the page stays as drawn
  const limit = Number(list.container.dataset.historyLimit) || 300;

  // What the owner is doing.
  const FIELDS = "input, select, textarea, [contenteditable]";
  let touched = { node: null, at: 0 };
  let pressed = null;
  // Only the owner's gestures (a key, the pointer, typing, a change): a focus the page put back
  // after a save is not one.
  ["input", "keydown", "keyup", "pointerdown", "change"].forEach((name) => document.addEventListener(name, (event) => {
    touched = { node: event.target, at: Date.now() };
    if (name === "pointerdown") pressed = event.target;
  }, true));
  ["pointerup", "pointercancel"].forEach((name) => document.addEventListener(name, () => { pressed = null; }, true));
  const { changedField } = window.TowSync; // sync.js's: one rule of what the owner changed
  const drafted = (element) => [...element.querySelectorAll("form")].some((form) => !form.matches("[data-theme-form], [data-auto-submit]")
    && [...form.querySelectorAll("input, select, textarea")].some(changedField));
  const protectedElement = (element) => {
    if (!element || element.nodeType !== 1) return false;
    if (document.querySelector("dialog[open]")) return true;
    if (element.matches('[data-dirty="1"], [data-submitting="1"], [data-saving="1"], [aria-busy="true"]')) return true;
    if (element.querySelector('form[data-dirty="1"], form[data-submitting="1"], [data-saving="1"], [aria-busy="true"], [data-loading], .edit-conflict')) return true;
    if (drafted(element)) return true;
    const active = document.activeElement;
    const focused = Boolean(active && active !== document.body && element.contains(active));
    if (focused && active.matches?.(FIELDS)) return true;
    if (focused && touched.node && element.contains(touched.node) && Date.now() - touched.at < TOUCH_MS) return true;
    return Boolean(pressed && element.contains(pressed));
  };

  // Which details are open, by the closest id around them and their place there.
  const disclosureKeys = (root) => {
    const keys = new Map();
    const all = [...(root.matches?.("details") ? [root] : []), ...root.querySelectorAll("details")];
    const counts = new Map();
    all.forEach((details) => {
      const owner = details.id ? details : details.parentElement?.closest("[id]");
      const base = details.id ? `#${details.id}` : `${owner?.id || ""}>${details.className}`;
      const index = counts.get(base) || 0;
      counts.set(base, index + 1);
      keys.set(`${base}|${index}`, details);
    });
    return keys;
  };
  const carryDisclosures = (oldElements, freshElements) => {
    const before = new Map();
    oldElements.forEach((element) => disclosureKeys(element).forEach((details, key) => before.set(key, details.open)));
    freshElements.forEach((element) => disclosureKeys(element).forEach((details, key) => {
      if (before.has(key)) details.open = before.get(key);
    }));
  };
  // The focus by what it is, when a change took its element away.
  const focusIdentity = () => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    const details = active.closest?.("summary")?.parentElement;
    return { element: active, id: active.id || "", summaryOf: details?.id || "" };
  };
  const refocus = (was) => {
    if (!was || was.element.isConnected) return;
    if (document.activeElement && document.activeElement !== document.body) return; // the owner moved on
    const target = (was.id && document.getElementById(was.id))
      || (was.summaryOf && document.getElementById(was.summaryOf)?.querySelector(":scope > summary"));
    target?.focus?.({ preventScroll: true });
  };
  // One visible element keeps its place on the screen while the ones above it change.
  const stickyTop = () => Number.parseFloat(window.getComputedStyle?.(document.documentElement)?.getPropertyValue("--msg-top")) || 0;
  const anchorOf = (elements) => {
    if (!window.scrollY) return null; // at the top: the newest shows
    const top = stickyTop();
    const element = elements.find((each) => each.isConnected && !each.hidden && each.getBoundingClientRect().bottom > top);
    return element ? { element, key: element.id || element.dataset?.event || "", top: element.getBoundingClientRect().top } : null;
  };
  const restoreAnchor = (anchor) => {
    if (!anchor) return;
    const element = anchor.element.isConnected ? anchor.element
      : (anchor.key && (document.getElementById(anchor.key) || list.container.querySelector(`[data-event="${anchor.key}"]`)));
    if (!element) return;
    const shift = element.getBoundingClientRect().top - anchor.top;
    if (shift) window.scrollBy(0, shift);
  };
  const parse = (html) => {
    const template = document.createElement("template");
    template.innerHTML = html;
    return template.content;
  };
  const elementsOf = (nodes) => nodes.filter((node) => node.nodeType === 1);
  const putIn = (nodes) => elementsOf(nodes).forEach((element) => {
    lifecycle.mount(element);
    window.towTime?.prepare?.(element);
  });
  const takeOut = (nodes) => elementsOf(nodes).forEach((element) => lifecycle.unmount(element));

  // The note on what waits for the owner: in its edit panel, its card or its section.
  const NOTES = { changed: "js.sync.section_changed", removed: "js.sync.site_removed" };
  const noteIn = (host, kind) => {
    if (!host) return;
    let node = host.querySelector(":scope > [data-sync-note]");
    if (!kind) {
      node?.remove();
      return;
    }
    if (!node) {
      node = document.createElement("p");
      node.className = "field-hint sync-note";
      node.setAttribute("role", "status");
      node.setAttribute("data-sync-note", "");
      host.prepend(node);
    }
    node.textContent = t(kind === "removed" && list.page === "/sites" ? NOTES.removed : NOTES.changed);
  };
  const noteHosts = (element) => {
    const cards = [...element.querySelectorAll(".integration-card[id]")].filter(protectedElement);
    if (cards.length) return cards.map((card) => card.querySelector(".card-body") || card);
    return [element.querySelector(".edit-panel, .settings-panel") || element];
  };

  // ---- Sites and Settings: fragments between their comments in the list's element
  const slots = () => {
    const found = [];
    let open = null;
    list.container.childNodes.forEach((node) => {
      if (node.nodeType === 8) {
        const start = /^frag:([A-Za-z0-9_-]+)(?::([0-9a-f]{32}))?$/.exec(node.data);
        if (start) {
          open = { key: start[1], hash: start[2] || "", start: node, nodes: [] };
          return;
        }
        const end = /^\/frag:([A-Za-z0-9_-]+)$/.exec(node.data);
        if (end && open && end[1] === open.key) {
          open.end = node;
          found.push(open);
          open = null;
          return;
        }
      }
      if (open) open.nodes.push(node);
    });
    return found;
  };
  const replaceElement = (old, fresh) => {
    carryDisclosures([old], [fresh]);
    lifecycle.unmount(old);
    old.replaceWith(fresh);
    putIn([fresh]);
  };
  // A Settings section in use: its summary and the cards not in use follow now (the same cards,
  // in the same order); the rest once the owner leaves it.
  const partialSection = (slot, fragment) => {
    const section = elementsOf(slot.nodes)[0];
    const fresh = elementsOf([...parse(fragment.html).childNodes])[0];
    if (!section || !fresh || list.page !== "/settings") return;
    const summary = section.querySelector(":scope > summary");
    const freshSummary = fresh.querySelector(":scope > summary");
    if (summary && freshSummary && !summary.contains(document.activeElement)) summary.replaceChildren(...freshSummary.childNodes);
    const cards = (root) => [...root.querySelectorAll(".integration-card[id]")];
    const now = cards(section);
    const next = cards(fresh);
    if (now.length !== next.length || now.some((card, index) => card.id !== next[index].id)) return;
    now.forEach((card, index) => { if (!protectedElement(card)) replaceElement(card, next[index]); });
  };
  const slotAdapter = {
    isProtected: (slot) => elementsOf(slot.nodes).some(protectedElement),
    replace: (slot, fragment) => {
      const fresh = parse(fragment.html);
      const added = [...fresh.childNodes];
      const old = elementsOf(slot.nodes);
      carryDisclosures(old, elementsOf(added));
      takeOut(slot.nodes);
      slot.nodes.forEach((node) => node.remove());
      slot.start.data = `frag:${fragment.id}:${fragment.hash}`;
      slot.end.parentNode.insertBefore(fresh, slot.end);
      putIn(added);
    },
    insert: (fragment, previousKey) => {
      const all = slots();
      const after = previousKey ? all.find((slot) => slot.key === previousKey) : null;
      const before = after ? after.end.nextSibling : all[0]?.start || null;
      const piece = document.createDocumentFragment();
      const fresh = parse(fragment.html);
      const added = [...fresh.childNodes];
      piece.append(document.createComment(`frag:${fragment.id}:${fragment.hash}`), fresh, document.createComment(`/frag:${fragment.id}`));
      list.container.insertBefore(piece, before);
      putIn(added);
    },
    remove: (slot) => {
      takeOut(slot.nodes);
      [slot.start, ...slot.nodes, slot.end].forEach((node) => node.remove());
    },
    note: (slot, kind) => {
      const element = elementsOf(slot.nodes)[0];
      if (!element) return;
      if (!kind) element.querySelectorAll("[data-sync-note]").forEach((node) => node.remove());
      else noteHosts(element).forEach((host) => noteIn(host, kind));
    },
    partial: partialSection,
  };
  const applySlots = (body) => {
    const fragments = Array.isArray(body.fragments) ? body.fragments : [];
    const anchor = anchorOf(elementsOf(slots().flatMap((slot) => slot.nodes)));
    const focus = focusIdentity();
    const result = reconcile({ current: slots(), wanted: fragments, ...slotAdapter });
    if (result.placed.length || result.removed.length) {
      window.towList?.refresh?.({ keep: true }); // the search knows the new rows and sections
      restoreAnchor(anchor);
      refocus(focus);
    }
    return result;
  };

  // ---- History: one line per event, the newest window of its filters
  const lines = () => [...list.container.querySelectorAll(":scope > .log-line[data-event]")];
  const lineAdapter = {
    isProtected: (item) => protectedElement(item.element),
    replace: (item, fragment) => {
      const fresh = elementsOf([...parse(fragment.html).childNodes])[0];
      if (!fresh) return;
      fresh.dataset.hash = fragment.hash;
      replaceElement(item.element, fresh);
    },
    insert: (fragment, previousKey) => {
      const fresh = elementsOf([...parse(fragment.html).childNodes])[0];
      if (!fresh) return;
      fresh.dataset.hash = fragment.hash;
      const after = previousKey ? lines().find((line) => line.dataset.event === previousKey) : null;
      // A line that takes the newest of its kind's address takes it from the line that had it.
      if (fresh.id) document.getElementById(fresh.id)?.removeAttribute("id");
      list.container.insertBefore(fresh, after ? after.nextSibling : list.container.firstChild);
      putIn([fresh]);
    },
    remove: (item) => {
      lifecycle.unmount(item.element);
      item.element.remove();
    },
    adopt: (item, fragment) => {
      item.element.dataset.hash = fragment.hash;
    },
    note: () => {},
  };
  const applyHistory = (body) => {
    const entries = (Array.isArray(body.entries) ? body.entries : []).slice(0, Number(body.limit) || limit);
    const anchor = anchorOf(lines());
    const focus = focusIdentity();
    const result = reconcile({
      current: lines().map((element) => ({ key: element.dataset.event, hash: element.dataset.hash || "", element })),
      wanted: entries,
      ...lineAdapter,
    });
    const shown = lines().length;
    if (shown) list.container.querySelectorAll(":scope > .history-empty").forEach((node) => node.remove());
    const note = document.querySelector("[data-history-note]");
    if (note) note.hidden = shown < (Number(body.limit) || limit);
    if (result.placed.length || result.removed.length) {
      restoreAnchor(anchor);
      refocus(focus);
    }
    return result;
  };

  const sync = window.towSync;
  const pageSync = createPageSync({
    // 10 s at most, its body included (sync.js's request): a slower one is given up and asked again.
    fetchFragments: (rev) => {
      const params = new URLSearchParams({ page: list.page, rev });
      Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
      return sync.request(`/page/fragments?${params}`, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    },
    apply: (body) => (list.page === "/history" ? applyHistory(body) : applySlots(body)),
    ack: (rev) => sync.client?.ack?.("page", rev),
    signedOut: (status) => sync.client?.signOut?.(status),
    retry: () => window.setTimeout(() => sync.client?.nudge?.("hint"), 1000),
  });
  sync.page({ page: list.page, ...filters });
  // The page knows the revision it was drawn with: it asks for nothing until another one is named.
  if (list.rev) sync.client?.ack?.("page", list.rev);
  sync.on("page", (payload) => {
    if (sync.client?.stopped?.()) return;
    pageSync.page(payload).catch?.(() => {});
  }, { manual: true });
  // What waited for the owner: once the owner left it (a moment after the last activity) and after
  // every answer.
  let settleTimer = 0;
  const later = () => {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(() => pageSync.settle(), 300);
  };
  ["focusout", "pointerup", "submit", "reset", "change", "close"].forEach((name) => document.addEventListener(name, later, true));
  sync.onPoll((_, ok) => { if (ok) pageSync.settle(); });
  // Signed out: the fragments on their way are not put in, and nothing is asked or applied again.
  sync.onSignOut?.(() => {
    window.clearTimeout(settleTimer);
    pageSync.cancel();
  });
  window.towPages = { sync: pageSync, protectedElement, page: list.page };
})();
