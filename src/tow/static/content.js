// One picker for add and edit. Indices belong only to the prepared snapshot.
(() => {
  const messages = JSON.parse(document.getElementById("tow-content-i18n")?.textContent || "{}");
  const t = (key, vars = {}) => String(messages[key] ?? key).replace(/\{(\w+)\}/g, (whole, name) => Object.hasOwn(vars, name) ? String(vars[name]) : whole);
  const initialized = new WeakSet();
  const locale = document.documentElement.lang || undefined;
  const init = (root) => {
    if (initialized.has(root)) return;
    initialized.add(root);
    const form = root.closest("form");
    if (!form) return;
    const field = (name) => form.elements.namedItem(name);
    const mode = field("selection_mode"), expression = field("selection_value");
    // Choosing files needs this script: the page offers it without one only to keep an existing
    // manual selection, so the option is added here, after "all files".
    if (mode.dataset.exactOption && ![...mode.options].some((option) => option.value === "exact")) {
      mode.add(new Option(mode.dataset.exactOption, "exact"), 1);
    }
    const token = field("content_token"), indices = field("selection_indices");
    const status = root.querySelector("[data-content-status]");
    const results = root.querySelector("[data-content-results]");
    const tree = root.querySelector("[data-content-tree]");
    const search = root.querySelector("[data-content-search]");
    const more = root.querySelector("[data-content-more]");
    const previous = root.querySelector("[data-content-prev]");
    const pages = root.querySelector("[data-content-pages]");
    const loadButton = root.querySelector("[data-content-load]");
    const upload = root.querySelector("[data-content-upload]");
    const limited = root.querySelector("[data-content-limited]");
    const fresh = root.querySelector("[data-content-fresh]");
    const cachedHint = root.querySelector("[data-content-cached]");
    const magnet = root.querySelector("[data-content-magnet]");
    let snapshot = null, selected = new Set(), generation = 0, ruleGeneration = 0, page = 0;
    let nodes = [], expanded = new Set();
    const PAGE_SIZE = 200;
    let controller = null, ruleTimer = null, ruleNotice = "";
    let existing = [];
    try { existing = JSON.parse(root.dataset.existing || "[]"); } catch { /* Server remains authoritative. */ }
    const originalExisting = existing;
    let manualWanted = existing, previousMode = mode.value;
    let preparationAttempted = false;
    // Hidden inputs keep a script-set value through form.reset(); Cancel restores these.
    const initialToken = token.value, initialIndices = indices.value;
    const rememberManual = () => {
      if (snapshot && manual()) manualWanted = snapshot.files.filter((row) => selected.has(row.id));
    };
    const source = () => JSON.stringify([field("url").value.trim(), field("client_id").value]);
    const manual = () => mode.value === "exact";
    const rowSize = (row) => BigInt(row.size);
    // The same units and words as the server's sizes (web.bytes.*): "1,5 ГБ", "1.5 GB".
    const humanSize = (size) => {
      const units = ["web.bytes.b", "web.bytes.kb", "web.bytes.mb", "web.bytes.gb", "web.bytes.tb"];
      let scale = 1n, unit = 0;
      while (unit < units.length - 1 && size >= scale * 1024n) { scale *= 1024n; unit++; }
      const number = Number(size * 10n / scale) / 10;
      return t(units[unit], { size: number.toLocaleString(locale, { maximumFractionDigits: 1 }) });
    };
    const buildTree = () => {
      const top = { children: new Map(), path: "", depth: -1 };
      for (const file of snapshot.files) {
        const parts = file.path.split("/");
        let parent = top;
        for (const [depth, name] of parts.entries()) {
          if (!parent.children.has(name)) parent.children.set(name, {
            children: new Map(), name, path: [...parts.slice(0, depth), name].join("/"), depth,
            file: depth === parts.length - 1 ? file : null,
          });
          parent = parent.children.get(name);
        }
      }
      nodes = [...top.children.values()];
      expanded = new Set(); page = 0;
    };
    const eachFile = (node, visit) => {
      if (node.file) visit(node.file);
      else node.children.forEach((child) => eachFile(child, visit));
    };
    const summarize = () => {
      if (!snapshot) return;
      const chosen = snapshot.files.filter((row) => selected.has(row.id));
      const size = humanSize(chosen.reduce((sum, row) => sum + rowSize(row), 0n));
      const summary = !manual() && ruleNotice || t(manual() ? "content.js.count" : "content.js.rule", { count: chosen.length, size });
      if (status.textContent !== summary) status.textContent = summary;
      indices.value = manual() ? JSON.stringify([...selected]) : "";
      showSpace();
    };
    // The free space of the chosen folder, under the folder field (a hint: the add itself
    // decides, counting files already there, and waits for room instead of failing). Compared
    // with the chosen files once their sizes are known; a folder this computer does not see
    // shows nothing.
    const folder = field("save_path"), spaceHint = form.querySelector?.("[data-space-hint]") ?? null;
    let free = null, margin = 0n, spaceTimer = null, spaceAsked = 0;
    const showSpace = () => {
      if (!spaceHint) return;
      if (free === null) {
        spaceHint.hidden = true;
        spaceHint.classList.remove("warn");
        return;
      }
      const chosen = snapshot ? snapshot.files.filter((row) => selected.has(row.id)) : [];
      const need = chosen.length ? chosen.reduce((sum, row) => sum + rowSize(row), 0n) : null;
      const short = need !== null && need + margin > free;
      const text = need === null
        ? t("content.js.space_free", { free: humanSize(free) })
        : t(short ? "content.js.space_short" : "content.js.space_fits", { size: humanSize(need), free: humanSize(free) });
      if (spaceHint.textContent !== text) spaceHint.textContent = text;
      spaceHint.classList.toggle("warn", short);
      spaceHint.hidden = false;
    };
    const askSpace = async () => {
      const asked = ++spaceAsked, path = String(folder?.value ?? "").trim();
      let reply = null;
      if (path && spaceHint && typeof fetch === "function") {
        try {
          const response = await fetch(`/content/space?path=${encodeURIComponent(path)}`, { headers: { Accept: "application/json", "X-TOW-Space": "1" } });
          reply = response.ok ? await response.json() : null;
        } catch {
          reply = null; // no answer: no hint, the add still decides
        }
      }
      if (asked !== spaceAsked) return;
      free = Number.isSafeInteger(reply?.free) ? BigInt(reply.free) : null;
      margin = Number.isSafeInteger(reply?.margin) ? BigInt(reply.margin) : 0n;
      showSpace();
    };
    folder?.addEventListener("input", () => { window.clearTimeout(spaceTimer); spaceTimer = window.setTimeout(askSpace, 400); });
    folder?.addEventListener("change", () => { window.clearTimeout(spaceTimer); askSpace(); });
    askSpace();
    const checkbox = (name, checked, mixed, update, key) => {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = checked;
      input.indeterminate = mixed;
      input.disabled = !manual();
      input.setAttribute("aria-label", name);
      input.dataset.contentKey = key;
      input.addEventListener("change", () => { update(input.checked); render(); });
      return input;
    };
    const render = () => {
      if (!snapshot) return;
      const focused = tree.contains(document.activeElement) ? document.activeElement.dataset.contentKey : null;
      tree.replaceChildren();
      const query = search.value.toLocaleLowerCase();
      const visible = [];
      const collect = (node) => {
        let total = 0, count = 0, matches = false;
        if (node.file) {
          total = 1; count = selected.has(node.file.id) ? 1 : 0;
          matches = node.path.toLocaleLowerCase().includes(query);
        } else node.children.forEach((child) => {
          const result = collect(child);
          total += result.total; count += result.count; matches ||= result.matches;
        });
        Object.assign(node, { total, count, matches });
        return node;
      };
      nodes.forEach(collect);
      const flatten = (node) => {
        if (!node.matches) return;
        visible.push(node);
        if (!node.file && (query || expanded.has(node.path))) node.children.forEach(flatten);
      };
      nodes.forEach(flatten);
      page = Math.min(page, Math.max(0, Math.ceil(visible.length / PAGE_SIZE) - 1));
      const fragment = document.createDocumentFragment();
      visible.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).forEach((node) => {
        const row = document.createElement("div");
        row.className = node.file ? "content-file" : "content-folder";
        row.dataset.depth = String(Math.min(node.depth, 8));
        if (!node.file) {
          const toggle = document.createElement("button");
          toggle.type = "button"; toggle.className = "ghost";
          const open = Boolean(query) || expanded.has(node.path);
          toggle.textContent = open ? "▾" : "▸";
          toggle.disabled = Boolean(query);
          toggle.setAttribute("aria-expanded", String(open));
          toggle.setAttribute("aria-label", t("content.js.expand", { name: node.path }));
          toggle.dataset.contentKey = "folder:" + node.path;
          toggle.addEventListener("click", () => { open ? expanded.delete(node.path) : expanded.add(node.path); render(); });
          row.append(toggle);
        }
        const label = document.createElement("label");
        const name = document.createElement("span");
        name.textContent = node.name + (node.file ? "" : "/");
        name.title = node.path;
        const toggleSelection = (on) => eachFile(node, (file) => on ? selected.add(file.id) : selected.delete(file.id));
        label.append(checkbox(node.file ? node.path : t("content.js.folder", { name: node.path }), node.count === node.total, node.count > 0 && node.count < node.total, toggleSelection, "select:" + node.path), name);
        row.append(label);
        if (node.file) {
          const size = document.createElement("small");
          size.textContent = humanSize(rowSize(node.file));
          size.title = rowSize(node.file).toLocaleString();
          row.append(size);
        }
        fragment.append(row);
      });
      if (!visible.length) fragment.append(document.createTextNode(t("content.js.empty")));
      tree.append(fragment);
      pages.hidden = visible.length <= PAGE_SIZE;
      previous.disabled = page === 0;
      more.disabled = (page + 1) * PAGE_SIZE >= visible.length;
      root.querySelector("[data-content-page]").textContent = t("content.js.page", { page: page + 1, pages: Math.max(1, Math.ceil(visible.length / PAGE_SIZE)) });
      root.querySelector("[data-content-manual-actions]").hidden = !manual();
      summarize();
      if (focused) [...tree.querySelectorAll("[data-content-key]")].find((element) => element.dataset.contentKey === focused)?.focus();
    };
    // The content routes answer JSON; the request middleware refuses in plain text (an
    // expired sign-in, an oversized upload), which must not surface as a parser error.
    const answer = async (response) => {
      const failed = () => new Error(t(response.status === 401 ? "content.js.session" : response.status === 413 ? "content.js.too_large" : "content.js.failed"));
      if (!(response.headers.get("content-type") || "").startsWith("application/json")) throw failed();
      return response.json().catch(() => { throw failed(); });
    };
    const resolveRule = async () => {
      if (!snapshot || manual()) { render(); return; }
      const epoch = ++ruleGeneration, current = snapshot, kind = mode.value, value = expression.value;
      const title = field("title").value, lifecycle = field("tracking_mode").value;
      ruleNotice = t("content.js.rule_loading");
      selected.clear();
      render();
      const body = new FormData();
      Object.entries({ token: current.token, url: field("url").value, client_id: field("client_id").value, mode: kind, value,
        topic_id: root.dataset.topicId || "", title, tracking_mode: lifecycle }).forEach(([key, item]) => body.set(key, item));
      try {
        const response = await fetch("/content/resolve", { method: "POST", body });
        const data = await answer(response);
        if (epoch !== ruleGeneration || current !== snapshot || kind !== mode.value || value !== expression.value
          || title !== field("title").value || lifecycle !== field("tracking_mode").value) return;
        if (!response.ok) throw new Error(data.error);
        selected = new Set(data.indices);
        ruleNotice = data.waiting || "";
        render();
      } catch (error) {
        if (epoch === ruleGeneration && current === snapshot) {
          ruleNotice = error.message || t("content.js.failed");
          status.textContent = ruleNotice;
        }
      }
    };
    const invalidate = () => {
      // Discard the preparation and invalidate a late response. A chosen local file
      // belongs to the link it was chosen for, never to an edited one.
      // "Changed, get contents again" only when there was something to discard.
      const discarded = Boolean(snapshot || token.value || controller);
      generation++; ruleGeneration++;
      controller?.abort(); window.clearTimeout(ruleTimer);
      snapshot = null; selected.clear(); existing = [];
      manualWanted = [];
      ruleNotice = "";
      token.value = ""; indices.value = ""; upload.value = "";
      results.hidden = true; loadButton.disabled = false; magnet.disabled = false;
      limited.disabled = false; limited.hidden = true;
      if (fresh) { fresh.hidden = true; fresh.disabled = false; }
      if (cachedHint) cachedHint.hidden = true;
      status.textContent = discarded ? t("content.js.stale") : "";
      showSpace();
    };
    const load = async (allowLimited = false, restore = false, fromMagnet = false, fromTracker = false) => {
      if (allowLimited && !window.confirm(t("content.limited_confirm"))) return;
      controller?.abort();
      controller = new AbortController();
      const epoch = ++generation, origin = source();
      ruleGeneration++;
      preparationAttempted = true;
      ruleNotice = "";
      rememberManual();
      const savedToken = token.value, savedIndices = indices.value;
      token.value = ""; indices.value = ""; snapshot = null;
      results.hidden = true;
      status.textContent = t(fromMagnet ? "content.js.magnet_loading" : "content.js.loading");
      loadButton.disabled = true; magnet.disabled = true; limited.disabled = true;
      limited.hidden = true;
      if (fresh) { fresh.hidden = true; fresh.disabled = true; }
      if (cachedHint) cachedHint.hidden = true;
      const body = new FormData();
      body.set("url", field("url").value);
      body.set("client_id", field("client_id").value);
      body.set("allow_limited", String(allowLimited));
      body.set("source", fromMagnet ? "magnet" : fromTracker ? "fresh" : "torrent");
      if (restore) body.set("token", savedToken);
      else if (!fromMagnet && !fromTracker && !allowLimited && upload.files.length) body.set("torrent", upload.files[0]);
      try {
        const response = await fetch(restore ? "/content/snapshot" : "/content/prepare", { method: "POST", body, signal: controller.signal });
        const data = await answer(response);
        if (epoch !== generation || origin !== source()) return;
        if (!response.ok) {
          limited.hidden = data.code !== "content.limited";
          if (fresh) fresh.hidden = !["content.cache_invalid", "content.unavailable"].includes(data.code);
          throw new Error(data.error);
        }
        snapshot = data;
        if (fresh) fresh.hidden = data.cached !== true;
        if (cachedHint) {
          cachedHint.hidden = data.cached !== true && data.cache_failed !== true;
          cachedHint.textContent = t(data.cache_failed ? "content.cache_failed" : "content.cached_hint");
        }
        buildTree();
        token.value = data.token;
        const identities = new Set(manualWanted.map((item) => JSON.stringify([item.path, String(item.size)])));
        selected = new Set(data.files.filter((row) => identities.has(JSON.stringify([row.path, String(row.size)]))).map((row) => row.id));
        if (restore && manual()) {
          const saved = JSON.parse(savedIndices || "[]");
          const valid = new Set(data.files.map((row) => row.id));
          selected = new Set(saved.filter((id) => valid.has(id)));
        }
        results.hidden = false;
        await resolveRule();
      } catch (error) {
        if (epoch === generation && error.name !== "AbortError") status.textContent = error.message || t("content.js.failed");
      } finally {
        if (epoch === generation) {
          loadButton.disabled = false; magnet.disabled = false; limited.disabled = false;
          if (fresh) fresh.disabled = false;
        }
      }
    };
    loadButton.addEventListener("click", () => load());
    limited.addEventListener("click", () => load(true, false, false, true));
    fresh?.addEventListener("click", () => load(false, false, false, true));
    magnet.addEventListener("click", () => load(false, false, true));
    upload.addEventListener("change", () => load());
    field("url").addEventListener("input", invalidate);
    field("client_id").addEventListener("change", invalidate);
    const showExpression = () => expression.closest("[data-content-expression]")?.classList.toggle("content-unused", mode.value === "all" || manual());
    showExpression();
    form.addEventListener("reset", () => {
      // Cancel resets the native fields after firing this event. It must also discard
      // prepared state, not revive an abandoned edit or keep its hidden preparation.
      invalidate();
      existing = originalExisting; manualWanted = originalExisting;
      preparationAttempted = false;
      token.value = initialToken; indices.value = initialIndices;
      status.textContent = "";
      window.setTimeout(() => { previousMode = mode.value; showExpression(); }, 0);
    });
    mode.addEventListener("change", () => {
      ruleGeneration++;
      ruleNotice = "";
      showExpression();
      if (previousMode === "exact" && snapshot) manualWanted = snapshot.files.filter((row) => selected.has(row.id));
      previousMode = mode.value;
      if (manual()) {
        const wanted = new Set(manualWanted.map((item) => JSON.stringify([item.path, String(item.size)])));
        selected = new Set((snapshot?.files || []).filter((row) => wanted.has(JSON.stringify([row.path, String(row.size)]))).map((row) => row.id));
        render();
      } else resolveRule();
    });
    expression.addEventListener("input", () => {
      ruleGeneration++;
      window.clearTimeout(ruleTimer);
      ruleTimer = window.setTimeout(resolveRule, 200);
    });
    field("title").addEventListener("input", () => {
      ruleGeneration++;
      window.clearTimeout(ruleTimer);
      if (!manual()) ruleTimer = window.setTimeout(resolveRule, 200);
    });
    field("tracking_mode").addEventListener("change", () => { ruleGeneration++; if (!manual()) resolveRule(); });
    search.addEventListener("input", () => { page = 0; render(); });
    more.addEventListener("click", () => { page++; render(); tree.scrollTop = 0; });
    previous.addEventListener("click", () => { page--; render(); tree.scrollTop = 0; });
    root.querySelector("[data-content-all]").addEventListener("click", () => { selected = new Set(snapshot.files.map((row) => row.id)); render(); });
    root.querySelector("[data-content-none]").addEventListener("click", () => { selected.clear(); render(); });
    const help = root.querySelector("[data-content-help]"), helpText = root.querySelector("[data-content-help-text]");
    help.addEventListener("click", () => { helpText.hidden = !helpText.hidden; help.setAttribute("aria-expanded", String(!helpText.hidden)); });
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !helpText.hidden) {
        event.stopPropagation(); helpText.hidden = true; help.setAttribute("aria-expanded", "false"); help.focus();
      }
    });
    form.addEventListener("submit", (event) => {
      // Only an untouched edit may reuse the saved policy. A pending or failed
      // refresh has cleared its token, not confirmed the user's new selection.
      const unchangedExisting = !preparationAttempted && existing.length && !token.value && !indices.value;
      if (manual() && (!snapshot || selected.size === 0) && !unchangedExisting) {
        event.preventDefault(); event.stopImmediatePropagation();
        status.textContent = t("content.js.choose");
        loadButton.focus();
      }
    }, true);
    if (token.value) load(false, true);
  };
  const scan = (node) => {
    if (!(node instanceof Element)) return;
    if (node.matches("[data-content-picker]")) init(node);
    node.querySelectorAll("[data-content-picker]").forEach(init);
  };
  scan(document.documentElement);
  new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach(scan)))
    .observe(document.body, { childList: true, subtree: true });
})();
