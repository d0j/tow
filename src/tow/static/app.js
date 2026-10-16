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

// The one time rule, as the server writes it (tow.web.text.short_time): today "20:31"; another day
// of this year "15 Oct, 20:31"; another year "15 Oct 2025, 20:31"; a time still to come always
// dated. 24 h, in TOW's own zone (its computer's), not the browser's. base.html gives the zone's
// name when the server knows it (_tz_name, "Europe/Berlin": the browser's rules give every moment
// its own offset, summer time included) and its UTC offset now (_tz_now). Without the name the
// offset of now is right only for a time written now (a live message): a time the server wrote is
// then never written again here, and a script writes an earlier time from the server's own words
// ({iso, short, full}, tow.web.text.time_parts). ``value``: milliseconds or seconds since 1970 (a
// number or digits), or an ISO text; ``now`` for the tests.
const towTime = (() => {
  const DAY = 86400000;
  const offsetNow = String(I18N._tz_now ?? "").trim() ? Number(I18N._tz_now) : NaN;
  const calendar = (() => {
    const name = String(I18N._tz_name || "").trim();
    if (!name || typeof Intl === "undefined") return null;
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: name, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "numeric", second: "numeric",
      });
    } catch {
      return null; // a name this browser does not know: the offset of now, as without one
    }
  })();
  const parts = (ms) => {
    if (calendar) {
      const field = Object.fromEntries(calendar.formatToParts(new Date(ms)).map((part) => [part.type, part.value]));
      const at = { y: Number(field.year), m: Number(field.month), d: Number(field.day), h: Number(field.hour) % 24, min: Number(field.minute), s: Number(field.second) };
      at.offset = Math.round((Date.UTC(at.y, at.m - 1, at.d, at.h, at.min, at.s) - Math.floor(ms / 1000) * 1000) / 60000);
      return at;
    }
    if (!Number.isFinite(offsetNow)) { // a page without TOW's zone: the browser's own
      const d = new Date(ms);
      return { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate(), h: d.getHours(), min: d.getMinutes(), s: d.getSeconds(), offset: -d.getTimezoneOffset() };
    }
    const d = new Date(ms + offsetNow * 60000);
    return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), min: d.getUTCMinutes(), s: d.getUTCSeconds(), offset: offsetNow };
  };
  const msOf = (value) => {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return NaN;
    const number = typeof value === "number" ? value : /^\s*\d+(\.\d+)?\s*$/.test(String(value)) ? Number(value) : NaN;
    if (Number.isFinite(number)) return number > 0 ? (number < 1e11 ? number * 1000 : number) : NaN;
    return Date.parse(String(value));
  };
  const two = (n) => String(n).padStart(2, "0");
  const zone = (minutes) => (minutes ? `UTC${minutes > 0 ? "+" : "-"}${two(Math.floor(Math.abs(minutes) / 60))}:${two(Math.abs(minutes) % 60)}` : "UTC");
  const short = (value, now = Date.now()) => {
    const ms = msOf(value);
    if (!Number.isFinite(ms)) return value === null || value === undefined ? "" : String(value);
    const at = parts(ms);
    const today = parts(now);
    const clock = `${two(at.h)}:${two(at.min)}`;
    if (at.y === today.y && at.m === today.m && at.d === today.d && ms <= now + 60000) return clock;
    return t(at.y === today.y ? "time.this_year" : "time.other_year", { day: at.d, month: t(`time.month_short.${at.m}`), year: at.y, time: clock });
  };
  // The full time (the tooltip): the language's date pattern with seconds, and the UTC offset.
  const full = (value) => {
    const ms = msOf(value);
    if (!Number.isFinite(ms)) return "";
    const at = parts(ms);
    const fields = { Y: String(at.y), m: two(at.m), d: two(at.d), H: two(at.h), M: two(at.min), S: two(at.s) };
    const date = String(I18N._datetime || "%Y-%m-%d %H:%M:%S").replace(/%([YmdHMS])/g, (_, field) => fields[field]);
    return `${date} ${zone(at.offset)}`;
  };
  // The ISO time with TOW's offset (a <time>'s datetime); "" for no time.
  const iso = (value) => {
    const ms = msOf(value);
    if (!Number.isFinite(ms)) return "";
    const at = parts(ms);
    const sign = at.offset < 0 ? "-" : "+";
    return `${at.y}-${two(at.m)}-${two(at.d)}T${two(at.h)}:${two(at.min)}:${two(at.s)}${sign}${two(Math.floor(Math.abs(at.offset) / 60))}:${two(Math.abs(at.offset) % 60)}`;
  };
  // Milliseconds to the next midnight in TOW's zone (when "today" changes).
  const untilMidnight = (now = Date.now()) => {
    const at = parts(now);
    return Math.max(60000, DAY - ((at.h * 60 + at.min) * 60 + at.s) * 1000);
  };
  // A <time> by the rule, its full time the tooltip; a value with no time in it is shown as
  // ``fallback`` (the words the server sent). ``value`` may be the server's own words for it
  // ({iso, short, full}): without the zone's name they are shown as they are.
  const element = (value, fallback = "") => {
    const node = document.createElement("time");
    const given = value !== null && typeof value === "object" ? value : null;
    const source = given ? given.iso : value;
    const when = iso(source);
    if (!when) {
      node.textContent = fallback || (given || value === null || value === undefined ? "" : String(value));
      return node;
    }
    node.setAttribute("data-short", "");
    if (given && !calendar) {
      node.setAttribute("datetime", String(given.iso));
      node.setAttribute("tabindex", "0");
      node.title = String(given.full || "");
      node.textContent = String(given.short || fallback || when);
      return node;
    }
    node.setAttribute("datetime", when);
    node.setAttribute("tabindex", "0"); // its full time on focus or a tap (timeTip below)
    node.title = full(source);
    node.textContent = short(source);
    // Written now by TOW's offset of now: right for today, so it follows midnight too.
    if (!calendar) node.setAttribute("data-live", "");
    keepToday();
    return node;
  };
  // "Today" changes at midnight and while the tab sleeps: a time written by the rule is written
  // again then, and when the tab is shown again - every one (time[data-short]) when the zone's
  // rules are known, else only those a script wrote now (time[data-live]): an older time the
  // server wrote has its summer or winter offset, which the offset of now would get wrong. Only a
  // page that shows such a time keeps a midnight timer.
  const LIVE = calendar ? "time[data-short][datetime]" : "time[data-live][datetime]";
  const rewrite = () => document.querySelectorAll(LIVE).forEach((node) => {
    const text = short(node.getAttribute("datetime"));
    if (text && node.textContent !== text) node.textContent = text;
  });
  let midnight = 0;
  const keepToday = () => {
    if (midnight) return;
    midnight = window.setTimeout(() => {
      midnight = 0;
      rewrite();
      keepToday();
    }, untilMidnight() + 1000) || -1;
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) rewrite(); });
  if (document.querySelector(LIVE)) keepToday();
  return { short, full, iso, element, untilMidnight };
})();
window.towTime = towTime;

// The full time of a short one (time[data-short]: its seconds and UTC offset) for a keyboard and a
// touch screen, not only a mouse's tooltip: such a time takes the focus, and its focus or a tap
// shows the full time in one shared tip beside it (role="tooltip", the time described by it);
// Escape, a tap elsewhere, leaving it or a scroll hides it. A time inside a control - a row's
// summary, a link, a button, a label - is left as it is: the control has the focus there, and the
// tooltip still has the full time. ``towTime.prepare(root)`` readies times a script put in later.
const timeTip = (() => {
  const TIMES = "time[data-short][title]";
  const usable = (node) => Boolean(node?.matches?.(TIMES)) && !node.closest("summary, a, button, label");
  const prepare = (root = document) => {
    const nodes = [...(root.matches?.(TIMES) ? [root] : []), ...root.querySelectorAll(TIMES)];
    nodes.forEach((node) => {
      if (usable(node) && !node.hasAttribute("tabindex")) node.setAttribute("tabindex", "0");
    });
  };
  let tip = null;
  let owner = null;
  const hide = () => {
    if (!owner) return;
    owner.removeAttribute("aria-describedby");
    owner = null;
    tip.hidden = true;
  };
  const show = (node) => {
    if (!node.title) return;
    if (!tip) {
      tip = document.createElement("span");
      tip.id = "time-tip";
      tip.className = "time-tip";
      tip.setAttribute("role", "tooltip");
    }
    // Inside an open dialog (the Log, a download list) the tip lives there: the dialog is above the page.
    (node.closest("dialog") || document.body).append(tip);
    if (owner && owner !== node) owner.removeAttribute("aria-describedby");
    owner = node;
    tip.textContent = node.title;
    node.setAttribute("aria-describedby", tip.id);
    tip.hidden = false;
    // Under the time, kept on the screen (above it near the bottom).
    const box = node.getBoundingClientRect();
    const size = tip.getBoundingClientRect();
    const wide = window.innerWidth || 0;
    const high = window.innerHeight || 0;
    const x = wide ? Math.max(8, Math.min(box.left, wide - size.width - 8)) : box.left;
    const below = box.bottom + 4;
    const y = high && below + size.height > high - 8 ? Math.max(8, box.top - size.height - 4) : below;
    tip.style?.setProperty("--tip-x", `${Math.round(x)}px`);
    tip.style?.setProperty("--tip-y", `${Math.round(y)}px`);
  };
  document.addEventListener("focusin", (event) => { if (usable(event.target)) show(event.target); });
  document.addEventListener("focusout", (event) => { if (event.target === owner) hide(); });
  document.addEventListener("click", (event) => {
    const node = event.target.closest?.(TIMES);
    if (usable(node)) show(node);
    else if (owner && !tip.contains(event.target)) hide();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !owner) return;
    hide(); // only the tip: the row or form around the time stays open
    event.preventDefault();
    event.stopImmediatePropagation?.();
  });
  window.addEventListener("scroll", hide, { passive: true, capture: true });
  window.addEventListener("resize", hide);
  prepare();
  return { prepare };
})();
towTime.prepare = timeTip.prepare;

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

// A5: an action posted from a row or from the header reloads the page, and the focus went back
// to the page's start. Just before the next page opens, the row and the action are kept for this
// tab; the next page puts the focus on the same button of the same row, else the row's summary
// (an edit was saved), else the row that took a deleted row's place, else the same header
// button, else the message. Without storage the page opens as before.
const FOCUS_KEY = "tow.focus.return";
const rowOf = (element) => element?.closest?.(".row-wrap[id]") || null;
const siblingRow = (row, step) => {
  let next = row;
  do next = step > 0 ? next.nextElementSibling : next.previousElementSibling;
  while (next && !(next.matches(".row-wrap[id]") && !next.hidden));
  return next?.id || "";
};
const rememberFocus = (form, nextUrl) => {
  const row = rowOf(form);
  if (!row && !form.closest?.("header.app, #flash")) return;
  try {
    sessionStorage.setItem(FOCUS_KEY, JSON.stringify({
      path: nextUrl.pathname, action: form.getAttribute("action") || "", at: Date.now(),
      row: row?.id || "", next: row ? siblingRow(row, 1) : "", previous: row ? siblingRow(row, -1) : "",
    }));
  } catch {
    // no storage: the next page opens with the focus at its start, as before
  }
};
const restoreFocus = () => {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(FOCUS_KEY) || "null");
    sessionStorage.removeItem(FOCUS_KEY);
  } catch {
    return;
  }
  if (!saved || saved.path !== location.pathname || !(Date.now() - saved.at < 120000)) return;
  // A refused add puts the focus in its field; a page that already moved the focus keeps it.
  if (document.getElementById("add-error") || (document.activeElement && document.activeElement !== document.body)) return;
  const buttonOf = (root) => [...(root?.querySelectorAll("form[action]") || [])]
    .find((form) => form.getAttribute("action") === saved.action)?.querySelector("button");
  const row = saved.row ? document.getElementById(saved.row) : null;
  let target = null;
  if (row) target = buttonOf(row.querySelector(":scope > .row-ops")) || row.querySelector(":scope > details > summary");
  else if (saved.row) target = [saved.next, saved.previous].map((id) => document.getElementById(id)).find(Boolean)?.querySelector(":scope > details > summary");
  else target = buttonOf(document.querySelector("header.app"));
  if (!target || target.closest("[hidden]")) {
    target = document.getElementById("flash");
    if (target && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  }
  if (!target) return;
  target.scrollIntoView({ block: "center" });
  target.focus({ preventScroll: true });
};

// A message about what the page could not do goes where every message goes (messages.js), named
// by the topic or site of its row (data-subject); never into the form, where it covered the row.
const showMessage = (record, options) => window.towMessages?.showMessage(record, options);
const subjectOf = (element) => element?.closest?.("[data-subject]")?.dataset.subject || "";
// The answer to a change was lost (the connection dropped, an error page): the change may have
// been made. Said as not confirmed, with the page to look at - never sent again by itself.
const unconfirmed = (element) => showMessage({
  kind: "err", subject: subjectOf(element), text: t("js.messages.unconfirmed"),
  href: location.pathname + location.search, link: t("js.messages.reload"),
});
class Refused extends Error {}

// A form back as it was before it was sent: enabled, its own label, no spinner.
const restoreForm = (form, submitter) => {
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
  submitter?.classList?.remove("spinning");
  form.removeAttribute("aria-busy");
  form.querySelectorAll("[data-busy-note]").forEach((note) => { note.hidden = true; });
  (form.closest(".row-wrap") || form.closest("details"))?.querySelector(".dot")?.classList.remove("spinning");
  form.closest(".row-ops")?.querySelector(".row-ico")?.classList.remove("busy");
  if (form.closest("header.app")) document.querySelectorAll(".hdr-sites .trk, .hdr-clock").forEach((el) => el.classList.remove("busy"));
};

// D10: a Check goes with an action id this tab keeps before sending it (checks.js); TOW answers
// with an acknowledgement, and the result comes as a message where the page is - no page opens.
const CHECK_ACTIONS = /^\/(check|topics\/([^/]+)\/(check|adopt|replace-revision))$/;
const beginCheck = (form, submitter, body) => {
  let path = "";
  try {
    path = formActionUrl(form, submitter).pathname;
  } catch {
    return "";
  }
  const match = CHECK_ACTIONS.exec(path);
  if (!match || !window.towChecks || typeof body?.set !== "function") return "";
  const action = window.towChecks.begin({ subject: subjectOf(form), topic: match[2] ? decodeURIComponent(match[2]) : "" });
  if (action) body.set("action_id", action);
  return action || "";
};

const submitPostForm = async (form, submitter, preparedBody = null) => {
  if (form.dataset.submitting === "1") return;
  form.dataset.submitting = "1";
  const body = preparedBody || (submitter ? new FormData(form, submitter) : new FormData(form));
  const action = beginCheck(form, submitter, body);
  // Signed out while the answer is on its way: what it brings belongs to the old session.
  const session = window.towMessages?.session?.();
  const ended = () => session !== undefined && window.towMessages?.session?.() !== session;
  // A check is forgotten only on a whole, definite answer that TOW did not take it (its refusal
  // in words, a page to open); a broken or unreadable answer leaves it to the lookup by its id.
  let definite = false;
  try {
    // X-TOW-Fetch: the server answers {"redirect": url} instead of a 303, so the next
    // page is rendered once (by the location change), not twice.
    const response = await fetch(formActionUrl(form, submitter), {
      method: "POST",
      body,
      credentials: "same-origin",
      redirect: "follow",
      headers: { Accept: "text/html", "X-TOW-Fetch": "1" },
    });
    if (!response.ok) {
      // TOW's own refusal says why, in words (JSON, or a plain text before any page); an error
      // page's HTML is never shown - then nobody knows what was done.
      const type = response.headers.get("content-type") || "";
      const body = (await response.text()).trim();
      let detail = "";
      if (type.includes("application/json")) {
        try {
          const data = JSON.parse(body);
          detail = data.error || data.detail || "";
        } catch {
          detail = "";
        }
      } else if (type.includes("text/plain") && response.status < 500) {
        detail = body;
      }
      detail = typeof detail === "string" ? detail.replace(/\s+/g, " ").slice(0, 240) : "";
      if (!detail) throw new Error(`HTTP ${response.status}`);
      definite = true;
      throw new Refused(detail);
    }
    let nextUrl = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      const data = await response.json();
      if (ended()) return;
      if (typeof data.cancelled === "boolean") {
        // A waiting check's Cancel from its message: the check's own entry (removed from the
        // queue) says it; only a Cancel that came too late has words of its own. No page opens.
        definite = true;
        if (data.message) showMessage(data.message);
        restoreForm(form, submitter);
        return;
      }
      if (action && data.accepted) {
        // Accepted: this tab follows it; the page stays.
        window.towChecks.acknowledge(action, data);
        // Only while it is still followed: a result polling has shown already stays the last word.
        if (data.message && window.towChecks.pending?.(action) !== false) showMessage(data.message);
        restoreForm(form, submitter);
        return;
      }
      if (data.redirect) nextUrl = new URL(data.redirect, location.href);
    } else if (response.redirected) {
      nextUrl = new URL(response.url);
    }
    if (!nextUrl) throw new Error(t("js.submit.no_next_page"));
    definite = true;
    if (action) window.towChecks.forget(action); // answered with a page (a refusal, a full queue)
    if (form.dataset.returnTarget) nextUrl.hash = form.dataset.returnTarget;
    rememberFocus(form, nextUrl);
    window.location.assign(nextUrl);
  } catch (error) {
    restoreForm(form, submitter);
    if (ended()) return;
    if (action && !definite) {
      // The answer was lost or broke off: TOW may have the check. It is looked up by its action id,
      // never sent again - and said so only while it is still followed (its result may have come
      // by polling while the answer's body was still on its way).
      if (window.towChecks.pending?.(action) !== false) {
        showMessage({ id: `check-${action}`, kind: "info", subject: subjectOf(form), text: t("js.checks.sent") });
      }
      return;
    }
    if (action) window.towChecks.forget(action);
    if (error instanceof Refused) {
      showMessage({ kind: "err", subject: subjectOf(form), text: t("js.submit.failed", { error: error.message }) });
    } else {
      unconfirmed(form);
    }
  }
};

// A slow action (a check that waits on a client or a site, a copy, a restore) says that it is
// working: its button shows data-busy-label with a spinner until the next page opens; a failed
// request puts the label back (submitPostForm).
const showBusy = (form, b) => {
  if (!b?.dataset.busyLabel || b.dataset.originalLabel) return;
  b.dataset.originalLabel = b.textContent;
  b.replaceChildren(Object.assign(document.createElement("span"), { className: "busy-spin" }), b.dataset.busyLabel);
  b.firstChild.setAttribute("aria-hidden", "true");
  b.setAttribute("aria-busy", "true");
  form.setAttribute("aria-busy", "true");
  form.querySelectorAll("[data-busy-note]").forEach((note) => { note.hidden = false; });
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
  // Adding a topic checks it at once: up to a minute.
  showBusy(form, b);
  b.disabled = true;
  if (useFetch) submitPostForm(form, b, preparedBody);
});

// A check's progress and result: checks.js. Check all's report closes in place: only its own
// parameter leaves the address (the filters and the open row stay); without a script the close
// is a link to the same address.
document.addEventListener("click", (event) => {
  const close = event.target.closest?.("[data-check-result-close]");
  if (!close) return;
  event.preventDefault();
  close.closest("#check-result")?.remove();
  history.replaceState(history.state, "", close.getAttribute("href"));
  document.getElementById("main")?.focus?.();
});

// D5: the credential prompt is a real modal (focus stays in it, Esc closes it).
document.querySelectorAll("dialog.credential-prompt[open]").forEach((dialog) => {
  const topic = new URL(location.href).searchParams.get("credential_topic") || "";
  dialog.close();
  dialog.showModal();
  // Closed by Esc: the address no longer opens it again on a reload, and the focus goes to the
  // topic's row (it stayed on a field of the closed dialog). The close event of the reopening
  // above arrives while the dialog is open again and changes nothing.
  dialog.addEventListener("close", () => {
    if (dialog.open) return;
    const u = new URL(location.href);
    u.searchParams.delete("credential_topic");
    u.searchParams.delete("browser_auth_id");
    history.replaceState({}, "", u.pathname + u.search + u.hash);
    const summary = topic ? document.getElementById(`row-${topic}`)?.querySelector(":scope > details > summary") : null;
    if (summary) summary.focus();
    else document.activeElement?.blur?.();
  });
});

// A message after an action, its Undo and the header's recent messages are messages.js's.

// One /health.json poll serves the header countdown and every personal timer. Each reader says
// when it next needs fresh data (null: not now); one answer updates them all. A hidden tab
// never polls; showing it again asks at once when a reader is waiting.
const healthPoll = (() => {
  const readers = [];
  const failedUntil = new Map();
  let timer = 0;
  let inFlight = false;
  const plan = () => {
    window.clearTimeout(timer);
    timer = 0;
    const due = readers.map((reader) => {
      const at = reader.due();
      return at === null ? null : Math.max(at, failedUntil.get(reader) || 0);
    }).filter((at) => at !== null);
    if (document.hidden || inFlight || !due.length) return;
    timer = window.setTimeout(poll, Math.max(0, Math.min(...due) - Date.now()));
  };
  const poll = async () => {
    timer = 0;
    if (inFlight) return;
    inFlight = true;
    let data = null;
    try {
      const response = await fetch("/health.json", { cache: "no-store" });
      const body = await response.json();
      if (response.ok && body.ok) data = body;
    } catch {
      // No answer: every reader keeps what it knows and retries later.
    }
    inFlight = false;
    readers.forEach((reader) => {
      try {
        reader.receive(data);
        failedUntil.delete(reader);
      } catch {
        // One failed view must neither stop other readers nor spin on its overdue deadline.
        failedUntil.set(reader, Date.now() + 60000);
      }
    });
    plan();
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      window.clearTimeout(timer);
      timer = 0;
    } else if (readers.filter((reader) => reader.shown()).length) {
      poll();
    } else {
      plan();
    }
  });
  return {
    add: (reader) => { readers.push(reader); plan(); },
    plan,
    // Now (a check has ended: the header's health and "Needs attention" are asked at once).
    now: () => { if (!inFlight && !document.hidden) poll(); },
  };
})();

const clock = document.getElementById("next-check");
if (clock) {
  // Compact countdown; its meaning and failures are the tooltip and the hidden text before the
  // value (a failed check is said in words, not by the red alone). Both change only on a change.
  const clockValue = clock.querySelector?.("[data-clock-value]") || clock;
  const clockLabel = clock.querySelector?.("[data-clock-words]");
  const showClock = (value) => {
    if (clockValue.textContent !== value) clockValue.textContent = value;
  };
  const showWords = (words) => {
    if (clock.title !== words) clock.title = words;
    if (clockLabel && clockLabel.textContent !== `${words} `) clockLabel.textContent = `${words} `;
  };
  let last = Number(clock.dataset.last) * 1000;
  let iv = Number(clock.dataset.interval) * 1000;
  let checkOk = clock.dataset.checkOk !== "0";
  // Why the last attempt failed, in words: the page's, then each /health.json answer's.
  let checkError = clock.dataset.errorText || clock.dataset.error || "";
  // While the check is overdue or failed, the countdown asks for /health.json: after 5 s,
  // then doubling up to 60 s while nothing changes (F6: it used to poll every 5 s forever).
  let pollDue = null;
  let pollDelay = 5000;
  const fmt = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const p = (n) => String(n).padStart(2, "0");
    return `${p(h)}:${p(m)}:${p(r)}`;
  };
  const ensurePoll = () => {
    if (pollDue !== null) return;
    pollDelay = 5000;
    pollDue = Date.now() + pollDelay;
    healthPoll.plan();
  };
  const tick = () => {
    if (!last) {
      // Not checked yet is grey (unknown), not red: nothing has failed.
      showClock("—");
      clock.classList.toggle("bad", !checkOk);
      showWords(t("js.clock.never_checked"));
      return;
    }
    const left = last + iv - Date.now();
    if (left <= 0) {
      showClock("00:00:00");
      clock.classList.add("bad");
      showWords(checkError ? t("js.clock.last_attempt", { error: checkError }) : t("js.clock.overdue_title"));
      ensurePoll();
      return;
    }
    clock.classList.toggle("bad", !checkOk);
    showClock(fmt(left));
    showWords(checkOk ? t("js.clock.until_next") : (checkError ? t("js.clock.last_attempt", { error: checkError }) : t("js.clock.last_attempt_failed")));
    if (!checkOk) ensurePoll();
  };
  healthPoll.add({
    due: () => pollDue,
    shown: () => {
      if (pollDue === null) return false;
      pollDelay = 5000;
      return true;
    },
    receive: (data) => {
      const waiting = pollDue !== null;
      if (data) {
        if (Number.isFinite(Number(data.next_from_ts))) last = Number(data.next_from_ts) * 1000;
        if (Number.isFinite(Number(data.interval_sec)) && Number(data.interval_sec) > 0) iv = Number(data.interval_sec) * 1000;
        checkOk = data.check_ok !== false;
        checkError = data.check_error_text || data.check_error || "";
        if (checkOk && last && last + iv > Date.now()) pollDue = null;
        tick();
      }
      if (waiting && pollDue !== null) {
        pollDelay = Math.min(pollDelay * 2, 60000);
        pollDue = Date.now() + pollDelay;
      }
    },
  });
  tick();
  setInterval(tick, 1000);
}

// Dates of the personal timers come from the supervisor, not a browser-side reset after a
// check or a page reload.
// A Home row checks.js has drawn again when its check ended: the header asks for its health at
// once, and the search below takes the new row in place of the old one.
// What a row needs beyond the page's delegated listeners (its icons keep their clicks), for
// the rows the page opened with and for a row drawn again.
const initRow = (row) => {
  row.querySelectorAll(".row-ops, .mirror-pick").forEach((el) => {
    el.addEventListener("click", (e) => e.stopPropagation());
  });
};
window.towRows = { health: () => healthPoll.now(), replaced: (_old, row) => initRow(row) };

const topicTimerNodes = Array.from(document.querySelectorAll("[data-topic-timer]"));
if (topicTimerNodes.length) {
  let serverNow = Number(topicTimerNodes[0].dataset.timerNow) * 1000;
  let sampledAt = performance.now();
  let timersDue = Date.now();
  let failedPolls = 0;
  const phaseKeys = {
    scheduled: "js.timer.scheduled", paused: "js.timer.paused", done: "js.timer.done",
    stopped: "js.timer.stopped", waiting: "js.timer.waiting", queued: "js.timer.queued", running: "js.timer.running",
  };
  // Each second only the timers on screen are drawn, and only what changed is written (500
  // timers rewrote their text, tooltip and label every second: ~20 ms/s on a 2000-topic Home).
  // A timer coming into view is drawn when the scroll pauses. The value is visible text; its
  // meaning is the tooltip and the hidden words before it (no aria-label on a span without a
  // role).
  const p = (n) => String(n).padStart(2, "0");
  const drawn = new Map();
  const onScreen = typeof IntersectionObserver === "undefined" ? null : new Set();
  const drawTimer = (node, now) => {
    const state = node.dataset.timerState;
    const at = Number(node.dataset.timerAt) * 1000;
    const seconds = Math.max(0, Math.ceil((at - now) / 1000));
    const text = state === "scheduled" ? `${p(Math.floor(seconds / 3600))}:${p(Math.floor(seconds % 3600 / 60))}:${p(seconds % 60)}` : ["queued", "running"].includes(state) ? "00:00:00" : "—";
    const phase = state === "scheduled" && !seconds ? "queued" : state;
    const title = `${t("js.timer.title", { minutes: node.dataset.timerMinutes })} · ${t(phaseKeys[phase] || phaseKeys.waiting)}`;
    let last = drawn.get(node);
    if (!last) {
      last = { value: node.querySelector("[data-timer-value]"), words: node.querySelector("[data-timer-words]"), text: null, title: null };
      drawn.set(node, last);
    }
    if (last.text !== text) {
      last.text = text;
      last.value.textContent = text;
    }
    if (last.title !== title) {
      last.title = title;
      node.title = title;
      if (last.words) last.words.textContent = `${title} `;
    }
  };
  const tickTimers = () => {
    const now = serverNow + performance.now() - sampledAt;
    for (const node of onScreen || topicTimerNodes) {
      if (node.isConnected) drawTimer(node, now);
    }
  };
  if (onScreen) {
    // Timers that come into view are drawn once the scroll pauses (or at the next tick): drawing
    // them on every scroll frame cost a layout per frame.
    let arrived = 0;
    const watcher = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) onScreen.add(entry.target);
        else onScreen.delete(entry.target);
      });
      window.clearTimeout(arrived);
      arrived = window.setTimeout(tickTimers, 150);
    });
    topicTimerNodes.forEach((node) => watcher.observe(node));
  }
  healthPoll.add({
    due: () => timersDue,
    shown: () => timersDue !== null,
    receive: (data) => {
      if (data && Number.isFinite(data.now_ts)) {
        failedPolls = 0;
        serverNow = data.now_ts * 1000;
        sampledAt = performance.now();
        for (const node of topicTimerNodes) {
          const item = data.topic_timers?.[node.dataset.topicTimer];
          if (!item) { node.remove(); continue; }
          node.dataset.timerState = item.state;
          node.dataset.timerAt = item.next_at;
          node.dataset.timerMinutes = item.minutes;
        }
      } else {
        // No response does not create a new deadline or report a successful check.
        failedPolls += 1;
        for (const node of topicTimerNodes) {
          if (!["paused", "done"].includes(node.dataset.timerState)) node.dataset.timerState = "stopped";
        }
      }
      tickTimers();
      if (!topicTimerNodes.some((node) => node.isConnected)) {
        timersDue = null;
        return;
      }
      const urgent = topicTimerNodes.some((node) => node.isConnected && (["queued", "running", "waiting"].includes(node.dataset.timerState) || (node.dataset.timerState === "scheduled" && Number(node.dataset.timerAt) <= serverNow / 1000)));
      timersDue = Date.now() + (failedPolls ? Math.min(5000 * 2 ** Math.min(failedPolls - 1, 4), 60000) : urgent ? 5000 : 30000);
    },
  });
  tickTimers();
  setInterval(() => { if (!document.hidden) tickTimers(); }, 1000);
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

// Esc folds the open add form (Home: a topic, Sites: a site) back to the header's "+" that
// opened it, keeping what was typed; a menu, a list or a dialog that is open takes the Esc
// first (they stop or prevent it).
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
  const add = document.getElementById("new");
  if (!add?.open || document.querySelector("dialog[open]")) return;
  const focus = document.activeElement;
  if (focus && focus !== document.body && !add.contains(focus)) return;
  event.preventDefault();
  add.open = false;
  document.querySelector('a.plus[href="#new"]')?.focus();
});

// Esc folds an open row (Home: a topic, Sites: a site) whose edit panel holds the focus, and the
// focus goes back to the row's summary (A15); what was typed stays. A list, a menu or a dialog
// that is open takes the Esc first.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
  if (document.querySelector("dialog[open]")) return;
  const row = document.activeElement?.closest?.("details.row-edit[open]");
  if (!row) return;
  event.preventDefault();
  row.open = false;
  row.querySelector(":scope > summary")?.focus();
});

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
initRow(document);

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
    // The title TOW puts into the field goes with the form: a name other than it is the owner's.
    const guessedEl = document.getElementById("topic-guessed-title");
    const body = new FormData();
    body.set("url", url);
    const sequence = ++guessSequence;
    try {
      const r = await fetch("/topics/guess-title", { method: "POST", body });
      const j = await r.json();
      if (sequence !== guessSequence || titleEl.value.trim() !== cur) return;
      if (j.ok && j.title) {
        if (guessedEl) guessedEl.value = j.title;
        titleEl.value = j.title;
        titleEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
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
  const autoFilled = new Map();
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
    // Only empty fields, or ones an earlier link filled, take the guess: what the owner typed
    // stays, and the message says it was kept.
    let kept = 0;
    const set = (id, v) => {
      const el = document.getElementById(id);
      if (!el || v == null || v === "" || el.value === v) return;
      if (el.value.trim() !== "" && el.value !== autoFilled.get(id)) {
        kept += 1;
        return;
      }
      el.value = v;
      autoFilled.set(id, v);
    };
    set("site-name", j.name);
    set("site-regex", j.url_regex);
    set("site-hosts", j.fetch_hosts);
    set("site-dl", j.download_path);
    set("site-login-path", j.login_path || "");
    set("site-topic-path", j.topic_path || "");
    const pageDl = document.getElementById("site-page-dl");
    if (pageDl && pageDl.checked === Boolean(autoFilled.get("site-page-dl"))) {
      pageDl.checked = Boolean(j.page_download);
      autoFilled.set("site-page-dl", pageDl.checked);
    }
    set("site-href-rx", j.download_href_regex || "");
    if (msg) {
      const said = j.exists
        ? t("js.guess.exists", { name: j.name }) + (j.need_login ? ` — ${t("js.guess.can_save_login")}` : "")
        : t("js.guess.filled", { name: j.name }) + (j.need_login ? ` — ${t("js.guess.need_login")}` : "");
      // Two sentences: "…a login is needed. Fields you had filled in yourself were kept."
      msg.textContent = kept ? `${said.replace(/[.\s]+$/, "")}. ${t("js.guess.kept_typed")}` : said;
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

  // Settings → Theme: the page takes the chosen colours at once, then the choice is saved in the
  // background (A6). Arrow keys move between the options and every move is a change: a save that
  // reloaded the page took the focus away at each step (WCAG 3.2.2). The focus stays on the
  // option, the section's pill names the choice, and the hidden status says it was saved; the
  // last of quick moves is the one saved.
  settingsPage.querySelectorAll("[data-theme-form]").forEach((form) => {
    // Its own pill: the section is Language and theme, and the first pill is the language's.
    const pill = form.closest("details")?.querySelector(":scope > summary [data-theme-pill]");
    let sequence = 0;
    let timer = 0;
    let pending = null;
    const save = async (mine, label) => {
      pending = null;
      try {
        // keepalive: a choice made just before leaving the page is still saved.
        const response = await fetch(formActionUrl(form), {
          method: "POST", body: new FormData(form), credentials: "same-origin", keepalive: true,
          headers: { Accept: "text/html", "X-TOW-Fetch": "1" },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        // A refusal (the data file busy, a theme TOW does not have) is a redirect too: its
        // message's kind tells it from "saved", and it is shown as the server worded it.
        const data = (response.headers.get("content-type") || "").includes("application/json") ? await response.json() : {};
        if (mine !== sequence) return;
        if (data.flash && data.flash.kind !== "ok") showMessage(data.flash);
        // One message for the theme: quick moves update it, they do not pile up.
        else showMessage({ id: "theme-saved", kind: "ok", text: t("js.settings.theme_saved", { theme: label }) }, { replace: true });
      } catch {
        if (mine === sequence) unconfirmed(form);
      }
    };
    form.addEventListener("change", (event) => {
      const value = event.target.value;
      if (value === "light" || value === "dark") document.documentElement.dataset.theme = value;
      else delete document.documentElement.dataset.theme;
      const label = event.target.closest("label")?.textContent.trim() || value;
      if (pill) pill.textContent = label;
      const mine = ++sequence;
      window.clearTimeout(timer);
      pending = () => save(mine, label);
      timer = window.setTimeout(pending, 250);
    });
    window.addEventListener("pagehide", () => {
      window.clearTimeout(timer);
      pending?.();
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
  // The address names a section, or a card inside one (#client-<id>): that section is the open one.
  const target = (targetId ? document.getElementById(targetId) : null)?.closest("details.settings-accordion") || null;
  if (target) {
    accordions.forEach((accordion) => {
      accordion.open = accordion === target;
    });
  }

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
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    fallback.remove();
    return copied;
  };
  // Copied, or not: a message as any other (the command stays on the page to copy by hand).
  settingsPage.querySelectorAll("[data-copy-text]").forEach((button) => {
    button.addEventListener("click", async () => {
      const copied = await copyText(button.dataset.copyText || "");
      showMessage(copied
        ? { id: "copy-command", kind: "ok", text: t("js.settings.copied") }
        : { id: "copy-command", kind: "err", text: t("js.settings.copy_failed") }, { replace: true });
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
    const progressText = summary.verifying ? t("js.downloads.verifying") : `${summary.completed || 0}/${expected}`;
    if (downloadTitle) downloadTitle.textContent = `${j.topic?.title || t("js.downloads.history")} — ${progressText}`;
    loadedDownloads = append ? loadedDownloads.concat(j.items || []) : (j.items || []);
    const rows = loadedDownloads.map((item) => {
      const line = document.createElement("div");
      line.className = "log-line";
      const status = item.status === "completed" ? "✓" : "·";
      line.append(status, " ", item.label || item.identity || t("js.downloads.file"));
      // The time by the one rule (the server's words for the stored time); its written words when it is not one.
      if (item.completed_at) line.append(" · ", towTime.element(item.completed_time || item.completed_observed_at || item.completed_at, item.completed_at));
      else if (item.progress != null) line.append(" · ", `${Math.round(Number(item.progress) * 100)}%`);
      return line;
    });
    const event = j.last_event;
    const eventLine = document.createElement("div");
    eventLine.className = "log-line";
    if (event) {
      eventLine.append(t("js.downloads.last_event", { event: event.display || event.label || event.kind || "—" }));
      if (event.event_at) eventLine.append(" · ", towTime.element(event.time || event.at || event.event_at, event.event_at));
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
  // Delegated: a row drawn again when its check ended (checks.js) has a new link.
  document.addEventListener("click", async (e) => {
    const link = e.target.closest?.("a.download-details");
    if (!link) return;
    e.preventDefault();
    e.stopPropagation();
    downloadBody.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: t("js.downloads.loading") }));
    // The focus comes back to the link, or to its row's summary when the row is open (the
    // link is then hidden and its words in the summary were clicked).
    downloadOpener = link.getClientRects().length ? link : link.closest(".row-wrap")?.querySelector("summary");
    downloadBaseUrl = link.href || link.getAttribute("href");
    loadedDownloads = [];
    downloadPop.showModal();
    try {
      await loadDownloadPage(downloadBaseUrl);
    } catch (error) {
      downloadBody.replaceChildren(Object.assign(document.createElement("div"), { className: "mut", textContent: error.message || t("js.downloads.open_failed") }));
    }
  });
  // An open row shows its progress as words in its summary (the link covers only a closed row,
  // A2): a click on them opens the same episodes instead of folding the row.
  document.addEventListener("click", (event) => {
    const words = event.target.closest?.(".progress-ghost");
    const link = words?.closest(".row-wrap")?.querySelector(":scope > .row-progress > a.download-details");
    if (!link) return;
    event.preventDefault();
    link.click();
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
// Both radio menus keep their native selects as the single source of the value.
const initChoiceMenu = (select, name) => {
  const picker = document.getElementById(`${name}-picker`);
  const toggle = document.getElementById(`${name}-toggle`);
  const menu = document.getElementById(`${name}-menu`);
  if (!select || !picker || !toggle || !menu) return;
  const items = [...menu.querySelectorAll("[data-choice-value]")];
  if (!items.length) return;
  const sync = () => {
    items.forEach((item) => item.setAttribute("aria-checked", String(item.dataset.choiceValue === select.value)));
    const current = items.find((item) => item.dataset.choiceValue === select.value) || items[0];
    const label = `${toggle.dataset.menuLabel}: ${current.querySelector("span").textContent}`;
    toggle.title = label;
    toggle.setAttribute("aria-label", label);
    // The option's own icon (a sort order has one, a site has none) - not its selected mark.
    const icon = current.querySelector(":scope > svg use");
    if (icon) toggle.querySelector(":scope > svg use").setAttribute("href", icon.getAttribute("href"));
    const text = toggle.querySelector("span");
    if (text) text.textContent = current.querySelector("span").textContent;
    toggle.classList.toggle("on", Boolean(select.value));
  };
  const close = (restoreFocus = false) => {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    if (restoreFocus) toggle.focus();
  };
  const open = (index = 0) => {
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    items[index].focus();
  };
  toggle.addEventListener("click", () => menu.hidden ? open() : close(true));
  toggle.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    open(event.key === "ArrowUp" ? items.length - 1 : 0);
  });
  menu.addEventListener("click", (event) => {
    const item = event.target.closest("[data-choice-value]");
    if (!item || !menu.contains(item)) return;
    select.value = item.dataset.choiceValue;
    select.dispatchEvent(new Event("change"));
    close(true);
  });
  menu.addEventListener("keydown", (event) => {
    const index = items.indexOf(document.activeElement);
    const next = { ArrowDown: (index + 1) % items.length, ArrowUp: (index + items.length - 1) % items.length, Home: 0, End: items.length - 1 };
    if (Object.hasOwn(next, event.key)) {
      event.preventDefault();
      items[next[event.key]].focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    } else if (event.key === "Tab") {
      // Continue native tab order from the opener; never trap focus in the popup.
      close(true);
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !picker.contains(event.target)) close(menu.contains(document.activeElement));
  });
  picker.addEventListener("focusout", (event) => {
    if (!picker.contains(event.relatedTarget)) close();
  });
  select.addEventListener("change", sync);
  sync();
  select.hidden = true;
  picker.hidden = false;
};
const q = document.getElementById("q");
if (q) {
  const searchStatus = document.getElementById("search-status");
  // P2: what a keystroke needs is read once, at load: each row's folded words, its highlighted
  // texts and its status (a 2000-row Home spent ~250 ms per key querying and rewriting rows).
  const searchable = [...document.querySelectorAll("[data-q]")];
  const haystacks = new Map(searchable.map((el) => [el, fold(el.dataset.q)]));
  const marked = new Map(searchable.map((el) => [el, [...el.querySelectorAll("[data-hl]")]]));
  const markedWith = new WeakMap();  // the words a text is highlighted with now ("" = none)
  const tools = document.getElementById("list-tools");
  const list = document.getElementById("topics");
  const sortSelect = document.getElementById("list-sort");
  const trackerSelect = document.getElementById("list-tracker");
  const originalOrder = list ? [...list.querySelectorAll(":scope > .row-wrap")] : [];
  const tones = new Map();
  const tone = (row) => {
    if (!tones.has(row)) tones.set(row, ["bad", "warn", "new", "mut", "ok"].find((name) => row.querySelector(".dot.lg")?.classList.contains(name)) || "mut");
    return tones.get(row);
  };
  const rank = { bad: 0, warn: 1, new: 2, mut: 3, ok: 4 };
  const params = new URL(location.href).searchParams;
  let filter = params.get("f") || "";
  let tracker = params.get("t") || "";
  q.value = params.get("q") || "";
  // The chosen order stays on this device: a page opened without one in its address (the Home
  // icon, a new tab, a restart) gets the last order chosen here. Storage may be off: then as before.
  const SORT_KEY = "tow.home.sort";
  const storedSort = () => {
    try {
      return localStorage.getItem(SORT_KEY);
    } catch {
      return null;
    }
  };
  if (sortSelect) {
    const mode = params.get("s") ?? storedSort() ?? "";
    sortSelect.value = [...sortSelect.options].some((option) => option.value === mode) ? mode : "";
    initChoiceMenu(sortSelect, "sort");
  }
  if (trackerSelect) {
    trackerSelect.value = [...trackerSelect.options].some((option) => option.value === tracker) ? tracker : "";
    tracker = trackerSelect.value;
    initChoiceMenu(trackerSelect, "tracker");
  }
  if (tools) tools.hidden = originalOrder.length < 2 && !tracker;

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
    if (sortSelect) {
      try {
        localStorage.setItem(SORT_KEY, sortSelect.value);
      } catch {
        // no storage (a private window, blocked site data): the address still keeps the order
      }
    }
  };

  // Only what changes is written: a row's hidden state when it turns, a text's marks when its
  // words differ from the ones it shows (a hidden row shows none).
  const announce = () => {
    const tokens = fold(q.value).split(/\s+/).filter(Boolean);
    const words = tokens.join(" ");
    let shown = 0;
    searchable.forEach((el) => {
      const haystack = haystacks.get(el);
      let visible = tokens.every((token) => haystack.includes(token));
      if (visible && filter === "problem") visible = ["bad", "warn"].includes(tone(el));
      if (visible && filter === "new") visible = tone(el) === "new";
      if (visible && filter === "paused") visible = el.classList.contains("is-paused");
      if (visible && tracker) visible = el.dataset.tracker === tracker;
      if (el.hidden === visible) el.hidden = !visible;
      if (visible) shown += 1;
      const want = visible ? words : "";
      marked.get(el).forEach((target) => {
        if ((markedWith.get(target) ?? "") === want) return;
        markedWith.set(target, want);
        highlight(target, visible ? tokens : []);
      });
    });
    tools?.querySelectorAll("[data-filter]").forEach((chip) => {
      chip.classList.toggle("on", chip.dataset.filter === filter);
      chip.setAttribute("aria-pressed", String(chip.dataset.filter === filter));
    });
    const narrowed = tokens.length || filter || tracker;
    // A filter kept in the address that matches nothing must not leave bare headers.
    const listEmpty = document.getElementById("list-empty");
    if (listEmpty && listEmpty.hidden !== !(narrowed && !shown)) listEmpty.hidden = !(narrowed && !shown);
    const said = narrowed ? (shown ? t("js.search.shown_of", { shown, total: searchable.length }) : t("js.search.nothing")) : t("js.search.shown", { shown });
    if (searchStatus && searchStatus.textContent !== said) searchStatus.textContent = said;
  };

  // P1: the rows move only when the order changes: re-appending 2000 rows in the order they
  // already had cost a full layout (~1.2 s) on every load.
  const byName = new Intl.Collator("ru");
  const sortRows = () => {
    if (!list || !sortSelect) return;
    const mode = sortSelect.value;
    const rows = [...originalOrder];
    if (mode === "name") rows.sort((a, b) => byName.compare(a.dataset.name, b.dataset.name));
    if (mode === "event") rows.sort((a, b) => (b.dataset.event || "").localeCompare(a.dataset.event || ""));
    if (mode === "status") rows.sort((a, b) => rank[tone(a)] - rank[tone(b)]);
    const now = list.querySelectorAll(":scope > .row-wrap");
    if (rows.length === now.length && rows.every((row, index) => row === now[index])) return;
    list.append(...rows);
  };

  // P2: typing waits for a pause of 130 ms before the list is filtered (a keystroke cost ~250 ms
  // on a 2000-row Home, so fast typing queued them up).
  let typing = 0;
  q.addEventListener("input", () => {
    window.clearTimeout(typing);
    typing = window.setTimeout(() => { announce(); remember(); }, 130);
  });
  tools?.addEventListener("click", (event) => {
    const chip = event.target.closest("button");
    if (!chip || !chip.hasAttribute("data-filter")) return;
    filter = chip.dataset.filter;
    announce();
    remember();
  });
  trackerSelect?.addEventListener("change", () => {
    tracker = trackerSelect.value;
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
  // The search closes like the menus: Escape clears it first, then closes it; a click elsewhere
  // closes an empty one (a search that still filters the list stays in sight).
  const searchBox = q.closest("details");
  if (searchBox) {
    q.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (q.value) {
        q.value = "";
        q.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        searchBox.open = false;
        searchBox.querySelector("summary")?.focus();
      }
    });
    document.addEventListener("click", (event) => {
      if (searchBox.open && !q.value && !searchBox.contains(event.target)) searchBox.open = false;
    });
  }
  sortRows();
  announce();
  // The search, the filters and the order know the new row; one that no longer matches hides.
  const initReplaced = window.towRows.replaced;
  window.towRows.replaced = (old, row) => {
    initReplaced(old, row);
    for (const rows of [searchable, originalOrder]) {
      const index = rows.indexOf(old);
      if (index >= 0) rows[index] = row;
    }
    haystacks.delete(old);
    marked.delete(old);
    tones.delete(old);
    haystacks.set(row, fold(row.dataset.q));
    marked.set(row, [...row.querySelectorAll("[data-hl]")]);
    announce();
  };
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
      // The line's time by the one rule (the server's words for its stored time, else "iso");
      // else the server's written time.
      if (e.at || e.iso) at.append(towTime.element(e.time || e.iso || e.at, e.at));
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
  details.open = false;
  details.querySelector("summary")?.focus();
});

// A secret field's eye (_secret.html: data-reveal="<field id>"): it shows what was typed or
// pasted in the field and hides it again - it only changes the field's type; a saved secret is
// never in the page to show. Delegated, so a panel loaded later has it too. A click leaves the
// focus and the caret in the field (the keyboard's stays on the eye); the eye says its state
// (aria-pressed) and what it does next ("Hide “Bot token”").
const revealName = (button, field) => button.dataset.revealName
  || [...document.querySelectorAll("label[for]")].find((label) => label.getAttribute("for") === field.id)?.textContent.trim()
  || "";
const setRevealed = (button, field, shown) => {
  field.type = shown ? "text" : "password";
  button.setAttribute("aria-pressed", String(shown));
  const said = t(shown ? "web.secret.hide" : "web.secret.show", { field: revealName(button, field) });
  button.setAttribute("aria-label", said);
  button.title = said;
  button.querySelector("use")?.setAttribute("href", shown ? "#i-eye-off" : "#i-eye");
};
document.addEventListener("pointerdown", (event) => {
  if (event.target.closest?.("[data-reveal]")) event.preventDefault();
});
document.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-reveal]");
  const field = button ? document.getElementById(button.dataset.reveal) : null;
  if (!field || !["password", "text"].includes(field.type)) return;
  event.preventDefault();
  const focused = document.activeElement === field;
  const { selectionStart: start, selectionEnd: end, selectionDirection: direction } = field;
  setRevealed(button, field, field.type === "password");
  if (!focused) return;
  field.focus();
  try {
    field.setSelectionRange(start, end, direction || "none");
  } catch {
    // a field without a caret: the focus is enough
  }
});
// Sent or reset (Cancel), a shown secret is hidden again first: the next page, the emptied form
// and a password manager see a password field.
const hideRevealed = (form) => form?.querySelectorAll?.('[data-reveal][aria-pressed="true"]').forEach((button) => {
  const field = document.getElementById(button.dataset.reveal);
  if (field) setRevealed(button, field, false);
});
document.addEventListener("submit", (event) => hideRevealed(event.target), true);
document.addEventListener("reset", (event) => hideRevealed(event.target), true);

// Fields that count only with a box in one state (data-enabled-by="<box id>", and
// data-enabled-when="unchecked" for a box that turns them off): disabled otherwise, so the browser
// neither sends nor validates what the page hides - a password typed before browser sign-in was
// chosen, a too short network password once the box is clear. The server ignores them the same way.
const syncEnabledGroups = () => document.querySelectorAll("[data-enabled-by]").forEach((group) => {
  const box = document.getElementById(group.dataset.enabledBy);
  if (!box) return;
  const on = group.dataset.enabledWhen === "unchecked" ? !box.checked : Boolean(box.checked);
  group.querySelectorAll("input, select, textarea, button").forEach((control) => { control.disabled = !on; });
});
document.addEventListener("change", (event) => {
  if (event.target?.type === "checkbox") syncEnabledGroups();
});
// A reset (Cancel) puts the boxes back after its event: the groups follow once it has.
document.addEventListener("reset", () => setTimeout(syncEnabledGroups, 0));
syncEnabledGroups();

// Editable recent folders: the arrow shows every recent path, even when the input is filled.
// Keep native text editing; a highlighted suggestion changes the value only when accepted.
let openFolders = null;
const initSaveFolders = (root = document) => {
  root.querySelectorAll('.folder-input input[list="save-roots"]').forEach((input) => {
    const button = input.parentElement.querySelector("[data-save-folders]");
    const values = Array.from(document.getElementById("save-roots")?.options || [], (option) => option.value).slice(0, 10);
    if (!button || !values.length) return; // no scripts/history: native text input still works
    input.removeAttribute("list");
    const list = document.createElement("ul");
    list.id = `${input.id}-recent`;
    list.className = "folder-options";
    list.hidden = true;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", button.getAttribute("aria-label"));
    const options = values.map((value, index) => {
      const option = document.createElement("li");
      option.id = `${list.id}-${index}`;
      option.textContent = value;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      list.append(option);
      return option;
    });
    input.parentElement.append(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "none");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", list.id);
    button.setAttribute("aria-expanded", "false");
    button.hidden = false;
    let active = -1;
    const highlight = (index) => {
      active = index;
      options.forEach((option, i) => option.setAttribute("aria-selected", String(i === active)));
      if (active < 0) input.removeAttribute("aria-activedescendant");
      else {
        input.setAttribute("aria-activedescendant", options[active].id);
        options[active].scrollIntoView({ block: "nearest" });
      }
    };
    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-expanded", "false");
      highlight(-1);
      if (openFolders?.input === input) openFolders = null;
    };
    const open = () => {
      if (openFolders?.input !== input) openFolders?.close();
      list.hidden = false;
      list.classList.remove("above");
      const bounds = list.getBoundingClientRect();
      list.classList.toggle("above", bounds.bottom > window.innerHeight && input.getBoundingClientRect().top > bounds.height);
      input.setAttribute("aria-expanded", "true");
      button.setAttribute("aria-expanded", "true");
      openFolders = { input, close, wrapper: input.parentElement };
    };
    const choose = (index) => {
      input.value = values[index];
      close();
      input.focus();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };
    button.addEventListener("pointerdown", (event) => event.preventDefault());
    button.addEventListener("click", () => {
      const wasOpen = !list.hidden;
      input.focus();
      if (wasOpen) close(); else open();
    });
    options.forEach((option, index) => {
      option.addEventListener("pointerdown", (event) => event.preventDefault());
      option.addEventListener("click", () => choose(index));
    });
    input.addEventListener("input", () => highlight(-1));
    input.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        open();
        highlight(event.key === "ArrowDown" ? Math.min(active + 1, options.length - 1) : (active < 0 ? options.length - 1 : Math.max(active - 1, 0)));
      } else if (!list.hidden && event.key === "Enter" && active >= 0) {
        event.preventDefault();
        event.stopPropagation();
        choose(active);
      } else if (!list.hidden && event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
      } else if (!list.hidden && event.key === "Tab") close();
    });
    input.parentElement.addEventListener("focusout", (event) => {
      if (!input.parentElement.contains(event.relatedTarget)) close();
    });
  });
};
document.addEventListener("pointerdown", (event) => {
  if (openFolders && !openFolders.wrapper.contains(event.target)) openFolders.close();
});
document.addEventListener("reset", () => openFolders?.close());
initSaveFolders();

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
    initSaveFolders(panel);
    towTime.prepare(panel);
  } catch (error) {
    // Opening the row again tries again; the message offers the panel as a page of its own.
    delete slot.dataset.loading;
    slot.removeAttribute("aria-busy");
    showMessage({
      kind: "err",
      subject: subjectOf(details),
      text: t("js.edit.load_failed", { error: error.message || t("js.submit.unknown_error") }),
      href: slot.querySelector("a[href]")?.getAttribute("href") || "",
      link: t("js.messages.open_page"),
    });
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

// A "Needs attention" link lands on the problem: ?fix=<ref> names the control that fixes it
// (data-fix, fix_attrs; the server already focused it with autofocus) and ?spot=<id> the spot
// that shows it; a link without a fix names the spot as #<id> (the browser scrolls there, the CSS
// lights it up). The server opened what holds them. What still folds the control away opens, the
// spot comes into view below the sticky header and lights up (.landed), and the focus goes to the
// control; without one (nothing to do), to the spot itself. Runs after the rows have their order,
// so the spot stays where it is shown.
const landOnProblem = () => {
  const params = new URL(location.href).searchParams;
  const fix = params.get("fix") || "";
  const named = params.get("spot") || "";
  let anchor = named || String(location.hash || "").slice(1);
  if (!named) {
    try { anchor = decodeURIComponent(anchor); } catch { /* a malformed address: as written */ }
  }
  if (!fix && !anchor.startsWith("issue-")) return;
  const spot = anchor ? document.getElementById(anchor) : null;
  const control = fix ? [...document.querySelectorAll("[data-fix]")].find((el) => el.dataset.fix === fix) : null;
  const target = control || spot;
  if (!target || target.closest("[hidden]")) return;
  for (let fold = target.closest("details"); fold; fold = fold.parentElement?.closest("details")) fold.open = true;
  if (!control && !spot.hasAttribute("tabindex")) spot.setAttribute("tabindex", "-1");
  const shown = spot && !spot.closest("[hidden]") ? spot : target;
  if (spot && named) spot.classList.add("landed");
  shown.scrollIntoView({ block: "center" });
  target.focus({ preventScroll: true });
};
landOnProblem();
// The browser's own move to #anchor, when the page has been read, takes the focus away from a
// control that is not that spot (Chromium): once the page has loaded, the focus is put back.
window.addEventListener("load", () => {
  if (!document.activeElement || document.activeElement === document.body) landOnProblem();
}, { once: true });

// Last: the rows are in their order (sort, filter) before the focus comes back to one (A5).
restoreFocus();
