// Texts come from the page's language catalog (base.html renders them as JSON: the CSP forbids
// inline scripts). A missing text shows its key; {name} placeholders are filled from `vars`.
const I18N = (() => {
  try {
    return JSON.parse(document.getElementById("tow-i18n")?.textContent || "{}") || {};
  } catch {
    return {};
  }
})();
const t = (key, vars = {}) => String(I18N[key] ?? key).replace(
  /\{(\w+)\}/g,
  (whole, name) => (Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole),
);

const formActionUrl = (form, submitter = null) => new URL(
  submitter?.getAttribute("formaction") || form.getAttribute("action") || form.action || location.href,
  location.href,
);

const isLocalPostForm = (form, submitter = null) => {
  if (!(form instanceof HTMLFormElement) || (form.method || "get").toLowerCase() !== "post") return false;
  try {
    return formActionUrl(form, submitter).origin === location.origin;
  } catch {
    return false;
  }
};

const submitPostForm = async (form, submitter, preparedBody = null) => {
  if (form.dataset.submitting === "1") return;
  form.dataset.submitting = "1";
  try {
    // X-TOW-Fetch: the server answers {"redirect": url} instead of a 303, so the next
    // page is rendered once (by the location change), not twice.
    const response = await fetch(formActionUrl(form, submitter), {
      method: "POST",
      body: preparedBody || (submitter ? new FormData(form, submitter) : new FormData(form)),
      credentials: "same-origin",
      redirect: "follow",
      headers: { Accept: "text/html", "X-TOW-Fetch": "1" },
    });
    if (!response.ok) {
      let detail = (await response.text()).trim();
      try { detail = JSON.parse(detail).detail || detail; } catch { /* HTML or plain text */ }
      detail = String(detail).replace(/\s+/g, " ").slice(0, 240);
      throw new Error(detail || `HTTP ${response.status}`);
    }
    let nextUrl = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      const data = await response.json();
      if (data.redirect) nextUrl = new URL(data.redirect, location.href);
    } else if (response.redirected) {
      nextUrl = new URL(response.url);
    }
    if (!nextUrl) throw new Error(t("js.submit.no_next_page"));
    if (form.dataset.returnTarget) nextUrl.hash = form.dataset.returnTarget;
    window.location.assign(nextUrl);
  } catch (error) {
    delete form.dataset.submitting;
    const autoControl = form.querySelector("[data-auto-submit-control]");
    if (autoControl) {
      autoControl.disabled = false;
      if (autoControl.dataset.initialChecked !== undefined) {
        autoControl.checked = autoControl.dataset.initialChecked === "1";
      }
    }
    if (submitter && !submitter.dataset.keep) submitter.disabled = false;
    if (submitter?.dataset.originalLabel) {
      submitter.textContent = submitter.dataset.originalLabel;
      delete submitter.dataset.originalLabel;
    }
    submitter?.removeAttribute("aria-busy");
    form.removeAttribute("aria-busy");
    form.querySelectorAll("[data-busy-note]").forEach((note) => { note.hidden = true; });
    const errorHost = form.hidden && submitter ? submitter.closest(".edit-actions") || submitter.parentElement : form;
    let message = errorHost.querySelector("[data-form-error]");
    if (!message) {
      message = document.createElement("p");
      message.className = "sub";
      message.dataset.formError = "";
      message.setAttribute("role", "alert");
      errorHost.append(message);
    }
    message.textContent = t("js.submit.failed", { error: error.message || t("js.submit.unknown_error") });
  }
};

document.addEventListener("submit", (e) => {
  if (e.defaultPrevented) return;
  const form = e.target;
  const b = e.submitter;
  const confirmText = b?.dataset.confirm || form.dataset.confirm;
  if (confirmText && !window.confirm(confirmText)) {
    e.preventDefault();
    return;
  }
  // A GET form (a filter or a search) is a plain navigation: a disabled submitter would drop
  // its name=value from the query, so it is left alone.
  if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
  if (form.dataset.nativeSubmit !== undefined) {
    if (b && !b.dataset.keep) {
      b.disabled = true;
      window.setTimeout(() => { b.disabled = false; }, 3000);
    }
    return;
  }
  const act = (form.getAttribute("action") || "").replace(/\/$/, "");
  const useFetch = isLocalPostForm(form, b);
  const preparedBody = useFetch ? (b ? new FormData(form, b) : new FormData(form)) : null;
  if (useFetch) e.preventDefault();
  if (!b || b.dataset.keep) {
    if (useFetch) submitPostForm(form, b, preparedBody);
    return;
  }
  if (act === "/check" || form.classList.contains("ico-form")) {
    b.classList.add("spinning");
    document.querySelectorAll(".hdr-sites .trk, .hdr-clock").forEach((el) => el.classList.add("busy"));
  }
  if (/\/topics\/[^/]+\/check$/.test(act) || /\/sites\/[^/]+\/probe$/.test(act)) {
    b.classList.add("spinning");
    (form.closest(".row-wrap") || form.closest("details"))?.querySelector(".dot")?.classList.add("spinning");
    form.closest(".row-ops")?.querySelector(".row-ico")?.classList.add("busy");
  }
  if (form.matches("[data-browser-auth-start]") && b) {
    b.dataset.originalLabel = b.textContent || t("js.browser_auth.start");
    b.textContent = t("js.browser_auth.starting");
  }
  // A slow action (adding a topic checks it at once: up to a minute) says that it is working.
  if (b.dataset.busyLabel) {
    b.dataset.originalLabel = b.textContent;
    b.replaceChildren(Object.assign(document.createElement("span"), { className: "busy-spin" }), b.dataset.busyLabel);
    b.firstChild.setAttribute("aria-hidden", "true");
    b.setAttribute("aria-busy", "true");
    form.setAttribute("aria-busy", "true");
    form.querySelectorAll("[data-busy-note]").forEach((note) => { note.hidden = false; });
  }
  b.disabled = true;
  if (useFetch) submitPostForm(form, b, preparedBody);
});

// "Обновить все" runs in the background (D1): follow it, then show its result.
const checkJob = new URL(location.href).searchParams.get("check_job");
if (checkJob) {
  let delay = 2000;
  const follow = async () => {
    if (document.hidden) {
      window.setTimeout(follow, delay);
      return;
    }
    try {
      const response = await fetch(`/check/status?job=${encodeURIComponent(checkJob)}`, { cache: "no-store" });
      const data = await response.json();
      if (data.status === "done" || data.status === "failed") {
        // The server keeps the result and gives an address with its token: no text in the URL.
        window.location.assign(data.redirect || "/");
        return;
      }
      if (data.status !== "running") {
        const u = new URL(location.href);
        u.searchParams.delete("check_job");
        history.replaceState({}, "", u.pathname + u.search);
        return;
      }
    } catch {
      // the next poll retries
    }
    delay = Math.min(delay + 1000, 5000);
    window.setTimeout(follow, delay);
  };
  document.querySelectorAll('form[action="/check"] button').forEach((b) => b.classList.add("spinning"));
  window.setTimeout(follow, delay);
}

// D5: the credential prompt is a real modal (focus stays in it, Esc closes it).
document.querySelectorAll("dialog.credential-prompt[open]").forEach((dialog) => {
  dialog.close();
  dialog.showModal();
});

const flash = document.getElementById("flash");
if (flash) {
  const hideFlash = () => {
    flash.remove();
    const u = new URL(location.href);
    if (u.searchParams.has("flash")) {
      u.searchParams.delete("flash");
      history.replaceState({}, "", u.pathname + u.search);
    }
  };
  flash.querySelector(".flash-x")?.addEventListener("click", hideFlash);
  const ttl = Number(flash.dataset.ttl);
  if (ttl > 0) window.setTimeout(hideFlash, ttl * 1000);
}
const undoForm = document.getElementById("undo-form");
if (undoForm) {
  // The undo lasts a while (Settings → Checks): the button counts it down, then goes away.
  const ends = Date.now() + Number(undoForm.dataset.ttl) * 1000;
  const left = undoForm.querySelector("[data-undo-left]");
  const tickUndo = () => {
    const ms = ends - Date.now();
    if (!(ms > 0)) {
      undoForm.remove();
      return;
    }
    const s = Math.ceil(ms / 1000);
    if (left) left.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    window.setTimeout(tickUndo, 500);
  };
  tickUndo();
}

const clock = document.getElementById("next-check");
if (clock) {
  // L5: a visible label says what is counted ("next check in" / "check:" when overdue).
  const clockValue = clock.querySelector?.("[data-clock-value]") || clock;
  const clockLabel = clock.querySelector?.("[data-clock-label]") || null;
  const showClock = (value, counting) => {
    clockValue.textContent = value;
    if (clockLabel) clockLabel.textContent = counting ? t("js.clock.label_next") : t("js.clock.label_state");
  };
  let last = Number(clock.dataset.last) * 1000;
  let iv = Number(clock.dataset.interval) * 1000;
  let checkOk = clock.dataset.checkOk !== "0";
  let checkError = clock.dataset.error || "";
  // While the check is overdue or failed, /health.json is polled: 5 s, doubling up to
  // 60 s while nothing changes, and not at all in a hidden tab (F6: it used to poll every
  // 5 s forever, in every open tab).
  let pollTimer = 0;
  let pollWanted = false;
  let pollDelay = 5000;
  let pollInFlight = false;
  const errorText = (code) => ({
    secrets_migration_required: t("js.clock.secrets_migration_required"),
  })[code] || code;
  const fmt = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const p = (n) => String(n).padStart(2, "0");
    return `${p(h)}:${p(m)}:${p(r)}`;
  };
  const poll = async () => {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const response = await fetch("/health.json", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok || !data.ok) return;
      if (Number.isFinite(Number(data.next_from_ts))) last = Number(data.next_from_ts) * 1000;
      if (Number.isFinite(Number(data.interval_sec)) && Number(data.interval_sec) > 0) iv = Number(data.interval_sec) * 1000;
      checkOk = data.check_ok !== false;
      checkError = data.check_error || "";
      if (checkOk && last && last + iv > Date.now()) {
        pollWanted = false;
        window.clearTimeout(pollTimer);
        pollTimer = 0;
      }
      tick();
    } catch {
      // Keep the last known countdown; the next poll retries.
    } finally {
      pollInFlight = false;
    }
  };
  const schedulePoll = () => {
    if (pollTimer || !pollWanted || document.hidden) return;
    pollTimer = window.setTimeout(async () => {
      pollTimer = 0;
      await poll();
      pollDelay = Math.min(pollDelay * 2, 60000);
      schedulePoll();
    }, pollDelay);
  };
  const ensurePoll = () => {
    if (pollWanted) return;
    pollWanted = true;
    pollDelay = 5000;
    schedulePoll();
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      window.clearTimeout(pollTimer);
      pollTimer = 0;
    } else if (pollWanted) {
      pollDelay = 5000;
      poll().then(schedulePoll);
    }
  });
  const tick = () => {
    if (!last) {
      // Not checked yet is grey (unknown), not red: nothing has failed.
      showClock(t("js.clock.no_data"), false);
      clock.classList.toggle("bad", !checkOk);
      clock.title = t("js.clock.never_checked");
      return;
    }
    const left = last + iv - Date.now();
    if (left <= 0) {
      showClock(checkOk ? t("js.clock.overdue") : t("js.clock.failed"), false);
      clock.classList.add("bad");
      clock.title = checkError ? t("js.clock.last_attempt", { error: errorText(checkError) }) : t("js.clock.overdue_title");
      ensurePoll();
      return;
    }
    clock.classList.toggle("bad", !checkOk);
    showClock(fmt(left), true);
    clock.title = checkOk ? t("js.clock.until_next") : (checkError ? t("js.clock.last_attempt", { error: errorText(checkError) }) : t("js.clock.last_attempt_failed"));
    if (!checkOk) ensurePoll();
  };
  tick();
  setInterval(tick, 1000);
}

// H2: a refused add comes back with the form open: show the reason below the sticky header
// and put the cursor in the field it is about (that field is described by the reason).
const addError = document.getElementById("add-error");
if (addError) {
  const details = addError.closest("details");
  if (details) details.open = true;
  addError.scrollIntoView({ block: "start" });
  const field = document.getElementById(addError.dataset.focusField || "");
  // A field folded away (Sites: "Advanced") is unfolded first: a closed <details> takes no focus.
  for (let fold = field?.closest("details"); fold; fold = fold.parentElement?.closest("details")) fold.open = true;
  (field || addError).focus({ preventScroll: true });
}

document.querySelectorAll("a.plus, a[data-open-new]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const n = document.getElementById("new");
    if (!n) return;
    n.open = true;
    n.querySelector("input, textarea")?.focus();
  });
});
const browserAuthStatus = document.querySelector("[data-browser-auth-status]");
if (browserAuthStatus) {
  const topicId = browserAuthStatus.dataset.topicId || "";
  const operationId = browserAuthStatus.dataset.operationId || "";
  let browserPoll = 0;
  const pollBrowserAuth = async () => {
    if (!topicId || !operationId) return;
    try {
      const url = new URL(`/topics/${encodeURIComponent(topicId)}/tracker-browser-auth/status`, location.origin);
      url.searchParams.set("operation_id", operationId);
      const response = await fetch(url, { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) return;
      if (result.message) browserAuthStatus.textContent = result.message;
      if (result.status === "succeeded") {
        if (browserPoll) window.clearInterval(browserPoll);
        window.setTimeout(() => window.location.assign("/"), 500);
      } else if (result.status === "failed" && browserPoll) {
        window.clearInterval(browserPoll);
        browserPoll = 0;
      }
    } catch {
      // The next poll retries while the dedicated browser is active.
    }
  };
  if (operationId) {
    browserPoll = window.setInterval(pollBrowserAuth, 2000);
    pollBrowserAuth();
  }
}
document.querySelectorAll(".row-ops, .mirror-pick").forEach((el) => {
  el.addEventListener("click", (e) => e.stopPropagation());
});

const topicUrl = document.getElementById("topic-url");
if (topicUrl) {
  let guessSequence = 0;
  const fillTitle = async () => {
    const url = topicUrl.value.trim();
    const titleEl = document.getElementById("topic-title");
    if (!titleEl) return;
    if (!url.startsWith("http://") && !url.startsWith("https://")) return;
    const cur = titleEl.value.trim();
    let slug = url.split("?")[0].split("/").pop() || "";
    try { slug = decodeURIComponent(slug); } catch { /* Keep the raw URL slug. */ }
    slug = slug.replace(/_/g, " ");
    const n = (s) => s.toLowerCase().replace(/[^a-z0-9а-яё]+/gi, "");
    const junk = !cur || /^https?:/i.test(cur) || cur === url || (n(cur) && n(slug) && n(cur) === n(slug));
    if (!junk) return;
    const body = new FormData();
    body.set("url", url);
    const sequence = ++guessSequence;
    try {
      const r = await fetch("/topics/guess-title", { method: "POST", body });
      const j = await r.json();
      if (sequence !== guessSequence || titleEl.value.trim() !== cur) return;
      if (j.ok && j.title) titleEl.value = j.title;
    } catch {
      const msg = document.getElementById("guess-msg");
      if (msg && sequence === guessSequence) msg.textContent = t("js.guess.failed");
    }
  };
  topicUrl.addEventListener("change", fillTitle);
  topicUrl.addEventListener("paste", () => setTimeout(fillTitle, 0));
}

const fromUrl = document.getElementById("from-url");
if (fromUrl) {
  let guessSequence = 0;
  const fill = async () => {
    const url = fromUrl.value.trim();
    const msg = document.getElementById("guess-msg");
    if (!url.startsWith("http://") && !url.startsWith("https://")) return;
    const body = new FormData();
    body.set("url", url);
    const sequence = ++guessSequence;
    try {
      const r = await fetch("/sites/guess", { method: "POST", body });
      const j = await r.json();
      if (sequence !== guessSequence || fromUrl.value.trim() !== url) return;
    if (!j.ok) {
      if (msg) msg.textContent = j.error || t("js.guess.not_understood");
      return;
    }
    const set = (id, v) => {
      const el = document.getElementById(id);
      if (el && v != null && v !== "") el.value = v;
    };
    set("site-name", j.name);
    set("site-regex", j.url_regex);
    set("site-hosts", j.fetch_hosts);
    set("site-dl", j.download_path);
    set("site-login-path", j.login_path || "");
    set("site-topic-path", j.topic_path || "");
    document.getElementById("site-page-dl").checked = Boolean(j.page_download);
    set("site-href-rx", j.download_href_regex || "");
    if (msg) {
      msg.textContent = j.exists
        ? t("js.guess.exists", { name: j.name }) + (j.need_login ? ` — ${t("js.guess.can_save_login")}` : "")
        : t("js.guess.filled", { name: j.name }) + (j.need_login ? ` — ${t("js.guess.need_login")}` : "");
    }
    } catch {
      if (msg && sequence === guessSequence) msg.textContent = t("js.guess.failed");
    }
  };
  fromUrl.addEventListener("change", fill);
  fromUrl.addEventListener("paste", () => setTimeout(fill, 0));
}

const settingsPage = document.querySelector(".settings-page");
if (settingsPage) {
  const settingsForms = [...settingsPage.querySelectorAll("form.settings-form")];
  const accordions = [...settingsPage.querySelectorAll("details.settings-accordion")];
  accordions.forEach((accordion) => {
    accordion.addEventListener("toggle", () => {
      if (!accordion.open) return;
      accordions.forEach((other) => {
        if (other !== accordion) other.open = false;
      });
    });
  });
  const signature = (form) => JSON.stringify(
    [...form.querySelectorAll("input, select, textarea")].map((field) => [
      field.name,
      field.type,
      field.type === "checkbox" || field.type === "radio" ? field.checked : field.value,
    ]),
  );

  settingsForms.forEach((form) => {
    const initial = signature(form);
    const status = form.querySelector(".unsaved-state");
    const checkButton = form.querySelector("[data-check-saved]");
    const updateDirtyState = () => {
      const dirty = signature(form) !== initial;
      form.dataset.dirty = dirty ? "1" : "0";
      if (status) status.hidden = !dirty;
      if (checkButton) {
        // "Check" asks the saved settings: nothing to ask before an address is saved.
        const needsAddress = checkButton.hasAttribute("data-needs-address");
        checkButton.disabled = dirty || needsAddress;
        checkButton.title = dirty ? t("js.settings.save_first") : needsAddress ? t("js.settings.save_address_first") : "";
      }
    };
    form.addEventListener("input", updateDirtyState);
    form.addEventListener("change", updateDirtyState);
    updateDirtyState();
  });

  settingsPage.querySelectorAll("[data-auto-submit]").forEach((form) => {
    const control = form.querySelector("[data-auto-submit-control]");
    const status = form.querySelector("[data-auto-submit-status]");
    if (!control) return;
    control.dataset.initialChecked = control.checked ? "1" : "0";
    control.addEventListener("change", () => {
      if (form.dataset.submitting === "1") return;
      if (status) status.textContent = t("js.settings.applying");
      form.requestSubmit();
      control.disabled = true;
    });
  });

  window.addEventListener("beforeunload", (event) => {
    const hasUnsaved = settingsForms.some((form) => form.dataset.dirty === "1" && form.dataset.submitting !== "1");
    if (!hasUnsaved) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const openName = new URLSearchParams(location.search).get("open");
  const targetId = location.hash.slice(1) || (openName ? `acc-${openName}` : "");
  const target = targetId ? document.getElementById(targetId) : null;
  if (target?.matches("details.settings-accordion")) {
    accordions.forEach((accordion) => {
      accordion.open = accordion === target;
    });
  }
  // The message of a change made in a card is shown once, in that card (not again at the top).
  const panel = target?.querySelector(".settings-panel");
  if (panel && flash) {
    flash.classList.add("settings-result");
    panel.prepend(flash);
  }

  const copyStatus = document.getElementById("copy-command-status");
  const copyText = async (value) => {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch {
        // Plain HTTP on a LAN may not expose the async clipboard API.
      }
    }
    const fallback = document.createElement("textarea");
    fallback.value = value;
    fallback.setAttribute("readonly", "");
    fallback.className = "sr-only";
    document.body.append(fallback);
    fallback.select();
    const copied = document.execCommand("copy");
    fallback.remove();
    return copied;
  };
  settingsPage.querySelectorAll("[data-copy-text]").forEach((button) => {
    button.addEventListener("click", async () => {
      const copied = await copyText(button.dataset.copyText || "");
      if (copyStatus) copyStatus.textContent = copied ? t("js.settings.copied") : t("js.settings.copy_failed");
    });
  });
}

const downloadPop = document.getElementById("download-pop");
const downloadBody = document.getElementById("download-body");
const downloadTitle = document.getElementById("download-title");
if (downloadPop && downloadBody) {
  let downloadOpener = null;
  let loadedDownloads = [];
  let downloadBaseUrl = "";
  const loadDownloadPage = async (url, append = false) => {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || t("js.downloads.open_failed"));
    renderDownloads(data, append);
  };
  const renderDownloads = (j, append = false) => {
    const summary = j.summary || {};
    const expected = summary.expected == null ? "?" : summary.expected;
    if (downloadTitle) downloadTitle.textContent = `${j.topic?.title || t("js.downloads.history")} — ${summary.completed || 0}/${expected}`;
    loadedDownloads = append ? loadedDownloads.concat(j.items || []) : (j.items || []);
    const rows = loadedDownloads.map((item) => {
      const line = document.createElement("div");
      line.className = "log-line";
      const status = item.status === "completed" ? "✓" : "·";
      line.append(status, " ", item.label || item.identity || t("js.downloads.file"));
      if (item.completed_at) line.append(" · ", item.completed_at);
      else if (item.progress != null) line.append(" · ", `${Math.round(Number(item.progress) * 100)}%`);
      return line;
    });
    const event = j.last_event;
    const eventLine = document.createElement("div");
    eventLine.className = "log-line";
    if (event) {
      eventLine.append(t("js.downloads.last_event", { event: event.display || event.label || event.kind || "—" }));
      if (event.event_at) eventLine.append(" · ", event.event_at);
    }
    const content = event ? [eventLine, ...rows] : rows;
    if (j.pagination?.has_more) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "ghost";
      more.textContent = t("js.downloads.more");
      more.addEventListener("click", async () => {
        more.disabled = true;
        try {
          const next = new URL(downloadBaseUrl, location.href);
          next.searchParams.set("offset", String((j.pagination.offset || 0) + (j.items || []).length));
          next.searchParams.set("limit", String(j.pagination.limit || 100));
          await loadDownloadPage(next, true);
        } catch (error) {
          more.disabled = false;
          more.textContent = error.message || t("js.downloads.load_failed");
        }
      });
      content.push(more);
    }
    downloadBody.replaceChildren(...(content.length ? content : [Object.assign(document.createElement("div"), { className: "mut", textContent: t("js.downloads.no_events") })]));
  };
  document.querySelectorAll("a.download-details").forEach((link) => {
    link.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      downloadBody.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: t("js.downloads.loading") }));
      downloadOpener = e.currentTarget;
      downloadBaseUrl = link.href;
      loadedDownloads = [];
      downloadPop.showModal();
      try {
        await loadDownloadPage(link.href);
      } catch (error) {
        downloadBody.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: error.message || t("js.downloads.open_failed") }));
      }
    });
  });
  document.getElementById("download-close")?.addEventListener("click", () => downloadPop.close());
  downloadPop.addEventListener("close", () => downloadOpener?.focus());
  downloadPop.addEventListener("click", (e) => {
    if (e.target === downloadPop) downloadPop.close();
  });
}

// Search and list tools (E1): words match in any order (AND), "ё" = "е", the
// state lives in the URL, matches are highlighted and "/" opens the search.
const fold = (value) => String(value || "").toLowerCase().replaceAll("ё", "е");
const q = document.getElementById("q");
if (q) {
  const searchStatus = document.getElementById("search-status");
  const searchable = [...document.querySelectorAll("[data-q]")];
  searchable.forEach((el) => { el.dataset.qFolded = fold(el.dataset.q); });
  const tools = document.getElementById("list-tools");
  const list = document.getElementById("topics");
  const sortSelect = document.getElementById("list-sort");
  const trackerChips = tools?.querySelector("[data-tracker-chips]");
  const originalOrder = list ? [...list.querySelectorAll(":scope > .row-wrap")] : [];
  const tone = (row) => ["bad", "warn", "new", "mut", "ok"].find((name) => row.querySelector(".dot.lg")?.classList.contains(name)) || "mut";
  const rank = { bad: 0, warn: 1, new: 2, mut: 3, ok: 4 };
  const params = new URL(location.href).searchParams;
  let filter = params.get("f") || "";
  let tracker = params.get("t") || "";
  q.value = params.get("q") || "";
  if (sortSelect) sortSelect.value = params.get("s") || "";

  if (tools && trackerChips) {
    const names = [...new Set(originalOrder.map((row) => row.dataset.tracker).filter(Boolean))].sort();
    if (names.length > 1) {
      trackerChips.append(...names.map((name) => Object.assign(document.createElement("button"), {
        type: "button", className: "chip", textContent: name, value: name,
      })));
    }
    tools.hidden = originalOrder.length < 2;
  }

  const highlight = (el, tokens) => {
    if (el.dataset.text === undefined) el.dataset.text = el.textContent;
    const text = el.dataset.text;
    const folded = fold(text);
    const marks = [];
    tokens.forEach((token) => {
      let at = folded.indexOf(token);
      while (token && at !== -1) {
        marks.push([at, at + token.length]);
        at = folded.indexOf(token, at + token.length);
      }
    });
    if (!marks.length) {
      el.textContent = text;
      return;
    }
    marks.sort((a, b) => a[0] - b[0]);
    const parts = [];
    let cursor = 0;
    marks.forEach(([from, to]) => {
      if (from < cursor) return;
      parts.push(document.createTextNode(text.slice(cursor, from)));
      parts.push(Object.assign(document.createElement("mark"), { textContent: text.slice(from, to) }));
      cursor = to;
    });
    parts.push(document.createTextNode(text.slice(cursor)));
    el.replaceChildren(...parts);
  };

  const remember = () => {
    const u = new URL(location.href);
    const set = (key, value) => (value ? u.searchParams.set(key, value) : u.searchParams.delete(key));
    set("q", q.value.trim());
    set("f", filter);
    set("t", tracker);
    set("s", sortSelect?.value || "");
    history.replaceState({}, "", u.pathname + u.search + u.hash);
  };

  const announce = () => {
    const tokens = fold(q.value).split(/\s+/).filter(Boolean);
    let shown = 0;
    searchable.forEach((el) => {
      let visible = tokens.every((token) => el.dataset.qFolded.includes(token));
      if (visible && filter === "problem") visible = ["bad", "warn"].includes(tone(el));
      if (visible && filter === "new") visible = tone(el) === "new";
      if (visible && filter === "paused") visible = el.classList.contains("is-paused");
      if (visible && tracker) visible = el.dataset.tracker === tracker;
      el.hidden = !visible;
      if (visible) shown += 1;
      el.querySelectorAll("[data-hl]").forEach((target) => highlight(target, visible ? tokens : []));
    });
    tools?.querySelectorAll("[data-filter]").forEach((chip) => {
      chip.classList.toggle("on", chip.dataset.filter === filter);
      chip.setAttribute("aria-pressed", String(chip.dataset.filter === filter));
    });
    trackerChips?.querySelectorAll("button").forEach((chip) => {
      chip.classList.toggle("on", chip.value === tracker);
      chip.setAttribute("aria-pressed", String(chip.value === tracker));
    });
    const narrowed = tokens.length || filter || tracker;
    if (searchStatus) searchStatus.textContent = narrowed ? (shown ? t("js.search.shown_of", { shown, total: searchable.length }) : t("js.search.nothing")) : t("js.search.shown", { shown });
  };

  const sortRows = () => {
    if (!list || !sortSelect) return;
    const mode = sortSelect.value;
    const rows = [...originalOrder];
    if (mode === "name") rows.sort((a, b) => a.dataset.name.localeCompare(b.dataset.name, "ru"));
    if (mode === "event") rows.sort((a, b) => (b.dataset.event || "").localeCompare(a.dataset.event || ""));
    if (mode === "status") rows.sort((a, b) => rank[tone(a)] - rank[tone(b)]);
    list.append(...rows);
  };

  q.addEventListener("input", () => { announce(); remember(); });
  tools?.addEventListener("click", (event) => {
    const chip = event.target.closest("button");
    if (!chip) return;
    if (chip.dataset.filter !== undefined) filter = chip.dataset.filter;
    else tracker = tracker === chip.value ? "" : chip.value;
    announce();
    remember();
  });
  sortSelect?.addEventListener("change", () => { sortRows(); remember(); });
  q.closest("details")?.addEventListener("toggle", (event) => {
    if (!event.currentTarget.open && q.value) {
      q.value = "";
      announce();
      remember();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    const box = q.closest("details");
    if (box) box.open = true;
    q.focus();
  });
  if (q.value) {
    const box = q.closest("details");
    if (box) box.open = true;
  }
  sortRows();
  announce();
}

const paintLog = (body, rows) => {
  if (!rows.length) {
    body.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: t("js.log.empty") }));
    return;
  }
  body.replaceChildren(
    ...rows.map((e) => {
      const div = document.createElement("div");
      div.className = "log-line";
      const at = document.createElement("span");
      at.className = "mut";
      at.textContent = e.at || "";
      const b = document.createElement("b");
      b.textContent = e.label || "";
      div.append(at, " · ", b);
      if (e.detail) div.append(" · ", e.detail);
      if (e.raw) {
        // The raw socket/HTTP-library text stays one click away (the line says it in words).
        const more = document.createElement("details");
        more.className = "raw-error";
        const summary = document.createElement("summary");
        summary.textContent = t("js.log.details");
        const code = document.createElement("code");
        code.textContent = e.raw;
        more.append(summary, code);
        div.append(" ", more);
      }
      return div;
    }),
  );
};

const liveLog = (body, isOpen) => {
  let last = "";
  let timer = 0;
  const tick = async () => {
    if (!isOpen() || document.hidden) return;
    try {
      const r = await fetch("/log.json");
      const j = await r.json();
      const rows = j.rows || [];
      const raw = JSON.stringify(rows);
      if (raw === last) return;
      last = raw;
      paintLog(body, rows);
    } catch {
      if (!last) body.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: t("js.log.failed") }));
    }
  };
  tick();
  timer = window.setInterval(tick, 2000);
  return () => window.clearInterval(timer);
};

const logPop = document.getElementById("log-pop");
const logOpen = document.getElementById("log-open");
const logBody = document.getElementById("log-body");
if (logPop && logOpen && logBody) {
  let logOpener = null;
  let stopPop = null;
  const halt = () => {
    stopPop?.();
    stopPop = null;
  };
  logOpen.addEventListener("click", () => {
    logOpener = document.activeElement;
    halt();
    logPop.showModal();
    stopPop = liveLog(logBody, () => logPop.open);
  });
  document.getElementById("log-close")?.addEventListener("click", () => logPop.close());
  logPop.addEventListener("click", (e) => {
    if (e.target === logPop) logPop.close();
  });
  logPop.addEventListener("close", halt);
  logPop.addEventListener("close", () => logOpener?.focus());
}

// Delegated: edit panels loaded later (M2) have their own "Cancel" buttons.
document.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-close-details]");
  if (!button) return;
  const details = button.closest("details");
  if (!details) return;
  details.querySelectorAll("form").forEach((form) => form.reset());
  details.querySelectorAll("[data-form-error]").forEach((message) => message.remove());
  details.open = false;
  details.querySelector("summary")?.focus();
});

// M2: a Home row's edit panel is fetched when the row opens (200 inline forms made Home
// a megabyte). Without scripts the panel's link opens the same form as a page.
const loadEditPanel = async (details) => {
  const slot = details.querySelector(":scope > .edit-panel[data-edit-src]");
  if (!slot || slot.dataset.loading) return;
  slot.dataset.loading = "1";
  slot.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(slot.dataset.editSrc, { headers: { Accept: "text/html" }, cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const template = document.createElement("template");
    template.innerHTML = (await response.text()).trim();
    const panel = template.content.querySelector(".edit-panel");
    if (!panel) throw new Error(t("js.submit.unknown_error"));
    slot.replaceWith(panel);
  } catch (error) {
    delete slot.dataset.loading;
    slot.removeAttribute("aria-busy");
    const message = Object.assign(document.createElement("p"), {
      className: "sub",
      textContent: t("js.edit.load_failed", { error: error.message || t("js.submit.unknown_error") }),
    });
    message.setAttribute("role", "alert");
    slot.querySelector("[role=alert]")?.remove();
    slot.prepend(message);
  }
};
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (details instanceof HTMLDetailsElement && details.open && details.matches("details.row-edit")) {
    loadEditPanel(details);
  }
}, true);
document.querySelectorAll("details.row-edit[open]").forEach(loadEditPanel);

const accLog = document.getElementById("acc-log");
const accBody = document.getElementById("log-settings-body");
if (accLog && accBody) {
  let stopAcc = null;
  accLog.addEventListener("toggle", () => {
    if (accLog.open) {
      stopAcc?.();
      stopAcc = liveLog(accBody, () => accLog.open);
    } else {
      stopAcc?.();
      stopAcc = null;
    }
  });
}
