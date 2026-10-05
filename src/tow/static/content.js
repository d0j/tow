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
    let snapshot = null, selected = new Set(), generation = 0, ruleGeneration = 0, page = 0;
    let nodes = [], expanded = new Set();
    const PAGE_SIZE = 200;
    let controller = null, ruleTimer = null;
    let existing = [];
    try { existing = JSON.parse(root.dataset.existing || "[]"); } catch { /* Server remains authoritative. */ }
    let manualWanted = existing, previousMode = mode.value;
    const rememberManual = () => {
      if (snapshot && manual()) manualWanted = snapshot.files.filter((row) => selected.has(row.id));
    };
    const source = () => JSON.stringify([field("url").value.trim(), field("client_id").value]);
    const manual = () => mode.value === "exact";
    const rowSize = (row) => BigInt(row.size);
    const humanSize = (size) => {
      const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
      let scale = 1n, unit = 0;
      while (unit < units.length - 1 && size >= scale * 1024n) { scale *= 1024n; unit++; }
      const number = Number(size * 10n / scale) / 10;
      return number.toLocaleString(locale, { maximumFractionDigits: 1 }) + " " + units[unit];
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
      status.textContent = t(manual() ? "content.js.count" : "content.js.rule", { count: chosen.length, size });
      indices.value = manual() ? JSON.stringify([...selected]) : "";
    };
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
    const resolveRule = async () => {
      if (!snapshot || manual()) { render(); return; }
      const epoch = ++ruleGeneration, current = snapshot, kind = mode.value, value = expression.value;
      selected.clear();
      render();
      const body = new FormData();
      Object.entries({ token: current.token, url: field("url").value, client_id: field("client_id").value, mode: kind, value }).forEach(([key, item]) => body.set(key, item));
      try {
        const response = await fetch("/content/resolve", { method: "POST", body });
        const data = await response.json();
        if (epoch !== ruleGeneration || current !== snapshot || kind !== mode.value || value !== expression.value) return;
        if (!response.ok) throw new Error(data.error);
        selected = new Set(data.indices);
        render();
      } catch (error) {
        if (epoch === ruleGeneration && current === snapshot) status.textContent = error.message || t("content.js.failed");
      }
    };
    const invalidate = () => {
      generation++; ruleGeneration++;
      controller?.abort();
      snapshot = null; selected.clear(); existing = [];
      manualWanted = [];
      token.value = ""; indices.value = "";
      results.hidden = true; loadButton.disabled = false;
      status.textContent = t("content.js.stale");
    };
    const load = async (allowLimited = false, restore = false) => {
      controller?.abort();
      controller = new AbortController();
      const epoch = ++generation, origin = source();
      ruleGeneration++;
      rememberManual();
      const savedToken = token.value, savedIndices = indices.value;
      token.value = ""; indices.value = ""; snapshot = null;
      results.hidden = true;
      status.textContent = t("content.js.loading");
      loadButton.disabled = true;
      limited.hidden = true;
      const body = new FormData();
      body.set("url", field("url").value);
      body.set("client_id", field("client_id").value);
      body.set("allow_limited", String(allowLimited));
      if (restore) body.set("token", savedToken);
      else if (upload.files.length) body.set("torrent", upload.files[0]);
      try {
        const response = await fetch(restore ? "/content/snapshot" : "/content/prepare", { method: "POST", body, signal: controller.signal });
        const data = await response.json();
        if (epoch !== generation || origin !== source()) return;
        if (!response.ok) {
          limited.hidden = data.code !== "content.limited";
          throw new Error(data.error);
        }
        snapshot = data;
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
        if (epoch === generation) loadButton.disabled = false;
      }
    };
    loadButton.addEventListener("click", () => load());
    limited.addEventListener("click", () => load(true));
    upload.addEventListener("change", () => load());
    field("url").addEventListener("input", invalidate);
    field("client_id").addEventListener("change", invalidate);
    const showExpression = () => expression.closest("[data-content-expression]")?.classList.toggle("content-unused", mode.value === "all" || manual());
    showExpression();
    mode.addEventListener("change", () => {
      ruleGeneration++;
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
      if (manual() && (!snapshot || selected.size === 0) && !(existing.length && !token.value && !indices.value)) {
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
