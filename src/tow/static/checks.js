// The checks this tab started (D10). A Check pressed with the script gets an action id that this
// tab keeps (sessionStorage) before the request is sent; the server registers it before any work
// and answers at once. One scheduler then follows every such check, on every page of the tab,
// until its one result: no faster than every five seconds a check, at most two requests a second,
// one at a time, in turn; never while the tab is hidden; slower (up to a minute) while TOW does
// not answer. A check is never sent again by the script. Its messages go through messages.js.
(() => {
  const STATUS_MS = 5000; // one check is asked no more often than this
  const GAP_MS = 500; // two requests a second at most
  const BACKOFF_MS = 60000; // the longest wait while TOW does not answer
  const LIMIT = 64; // TOW keeps 64 checks: more cannot be followed (the page then opens as before)
  const UNKNOWN_GRACE_MS = 15000; // a request still on its way is not "unknown" yet
  const PREFIX = "tow.checks.";

  const newId = () => {
    const bytes = new Uint8Array(12);
    try {
      crypto.getRandomValues(bytes);
    } catch {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  };

  // The scheduler without the page: tests drive it with a fake clock and a fake status request.
  const createScheduler = ({
    now = () => Date.now(),
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id),
    request = async () => null, // entry -> { status: HTTP status, body } (or throws)
    storage = null,
    key = `${PREFIX}local`,
    hidden = () => false,
    progress = () => {}, // (entry, body): still waiting or running
    terminal = () => {}, // (entry, body | null): its one result (null: TOW cannot say any more)
    changed = () => {},
  } = {}) => {
    let memoryOnly = !storage;
    const load = () => {
      if (memoryOnly) return [];
      try {
        const data = JSON.parse(storage.get(key) || "null");
        return Array.isArray(data?.entries) ? data.entries.filter((entry) => entry && (entry.action || entry.job)) : [];
      } catch {
        memoryOnly = true;
        return [];
      }
    };
    let entries = load();
    let timer = 0;
    let inFlight = false;
    let last = -Infinity;
    let cursor = 0;
    let stopped = false;
    const save = () => {
      changed();
      if (memoryOnly) return;
      try {
        storage.set(key, JSON.stringify({ v: 1, entries }));
      } catch {
        memoryOnly = true;
      }
    };
    const plan = () => {
      clearTimer(timer);
      timer = 0;
      if (stopped || inFlight || hidden() || !entries.length) return;
      const soonest = Math.min(...entries.map((entry) => entry.due));
      timer = setTimer(poll, Math.max(0, Math.max(soonest, last + GAP_MS) - now()));
    };
    // In turn: the first due check after the one asked last.
    const pick = () => {
      const at = now();
      for (let step = 0; step < entries.length; step += 1) {
        const index = (cursor + step) % entries.length;
        if (entries[index].due <= at) {
          cursor = index + 1;
          return entries[index];
        }
      }
      return null;
    };
    const finish = (entry, body) => {
      // A job two clicks share (a waiting check of one topic) has one result.
      const same = entries.filter((other) => other === entry || (entry.job && other.job === entry.job));
      entries = entries.filter((other) => !same.includes(other));
      terminal(entry, body, same);
    };
    const handle = (entry, answer) => {
      if (!entries.includes(entry)) return; // forgotten meanwhile
      const at = now();
      if (answer?.status === 401) {
        // Signed out: nothing more can be asked; each check says its result is not confirmed.
        stopped = true;
        [...entries].forEach((other) => { if (entries.includes(other)) finish(other, null); });
        save();
        return;
      }
      const body = answer && answer.status >= 200 && answer.status < 300 ? answer.body : null;
      if (!body || typeof body.status !== "string") {
        entry.delay = Math.min(Math.max(entry.delay || STATUS_MS, STATUS_MS) * 2, BACKOFF_MS);
        entry.due = at + entry.delay;
        save();
        return;
      }
      if (typeof body.job === "string" && body.job) entry.job = body.job;
      entry.delay = STATUS_MS;
      if (body.status === "queued" || body.status === "running") {
        entry.due = at + STATUS_MS;
        progress(entry, body);
      } else if (body.status === "done" || body.status === "failed") {
        finish(entry, body);
      } else if (entry.job || at - entry.since > UNKNOWN_GRACE_MS) {
        finish(entry, body); // TOW restarted, or the record expired: unconfirmed, said once
      } else {
        entry.due = at + STATUS_MS; // the request may still be on its way
      }
      save();
    };
    const poll = async () => {
      timer = 0;
      if (stopped || inFlight || hidden()) return;
      const entry = pick();
      if (!entry) {
        plan();
        return;
      }
      inFlight = true;
      last = now();
      let answer = null;
      try {
        answer = await request(entry);
      } catch {
        answer = null;
      }
      inFlight = false;
      handle(entry, answer);
      plan();
    };
    return {
      // Before the request is sent: the action id the server registers (null: too many followed).
      begin: (meta = {}) => {
        if (stopped || entries.length >= LIMIT) return null;
        const at = now();
        const entry = { action: newId(), job: "", subject: String(meta.subject || ""), topic: String(meta.topic || ""), since: at, due: at + STATUS_MS, delay: STATUS_MS };
        entries.push(entry);
        save();
        plan();
        return entry.action;
      },
      // A check this tab follows from its page (no script started it: a native POST).
      adopt: (job, meta = {}) => {
        if (stopped || !job || entries.some((entry) => entry.job === job) || entries.length >= LIMIT) return false;
        const at = now();
        entries.push({ action: "", job: String(job), subject: String(meta.subject || ""), topic: String(meta.topic || ""), since: at, due: at + Math.min(STATUS_MS, meta.first ?? STATUS_MS), delay: STATUS_MS });
        save();
        plan();
        return true;
      },
      acknowledge: (action, ack) => {
        const entry = entries.find((item) => item.action === action);
        if (entry && ack?.job) {
          entry.job = String(ack.job);
          save();
        }
      },
      forget: (action) => {
        entries = entries.filter((item) => item.action !== action);
        save();
        plan();
      },
      wake: () => plan(),
      entries: () => entries.map((entry) => ({ ...entry })),
      count: () => entries.length,
      stored: () => !memoryOnly,
    };
  };
  window.TowCheckScheduler = createScheduler;
  if (typeof document === "undefined" || !document.getElementById?.("msg-host")) return;

  const I18N = (() => {
    try {
      return JSON.parse(document.getElementById("tow-i18n")?.textContent || "{}") || {};
    } catch {
      return {};
    }
  })();
  const t = (name) => String(I18N[name] ?? name);
  const host = document.getElementById("msg-host");
  const storage = (() => {
    try {
      const store = window.sessionStorage;
      return { get: (name) => store.getItem(name), set: (name, value) => store.setItem(name, value) };
    } catch {
      return null;
    }
  })();
  const show = (record, options) => window.towMessages?.showMessage(record, options);
  // A topic's row says that its check waits or runs, from what the scheduler learns.
  const markRow = (topic, status) => {
    const button = topic ? document.getElementById(`row-${topic}`)?.querySelector('form[action$="/check"] button') : null;
    if (!button) return;
    button.classList.toggle("spinning", status === "running");
    button.querySelector("use")?.setAttribute("href", status === "queued" ? "#i-clock" : "#i-refresh");
  };
  const scheduler = createScheduler({
    storage,
    key: `${PREFIX}${host.dataset.namespace || "local"}`,
    hidden: () => document.hidden,
    request: async (entry) => {
      const query = new URLSearchParams();
      if (entry.action) query.set("action", entry.action);
      if (entry.job) query.set("job", entry.job);
      const response = await fetch(`/check/status?${query}`, { cache: "no-store", credentials: "same-origin" });
      return { status: response.status, body: await response.json().catch(() => null) };
    },
    // Waiting or running: its one message, updated in place - no new reading time, not said again.
    progress: (entry, body) => {
      markRow(body.topic_id || entry.topic, body.status);
      if (body.message) show(body.message, { replace: true, quiet: true });
    },
    // Its result: the same message, now with the result, said once.
    terminal: (entry, body, same) => {
      markRow(body?.topic_id || entry.topic, "");
      const record = body?.message || { kind: "warn", subject: entry.subject, text: t("js.checks.unconfirmed"), href: "/history", link: t("js.checks.history") };
      same.forEach((item) => { if (item.action) window.towMessages?.remove?.(`check-${item.action}`); });
      show(record, { replace: true });
    },
    changed: () => window.towMessages?.refresh?.(),
  });
  document.addEventListener("visibilitychange", () => scheduler.wake());

  // A page that follows a check (no script started it: a native POST, or its result opened before
  // it ended): the scheduler takes it over, and the page's note becomes its message.
  const note = document.getElementById("check-activity");
  if (note?.dataset.checkJob && note.dataset.checkStatus !== "finished") {
    const job = note.dataset.checkJob;
    const words = note.querySelector("[data-check-status]")?.textContent.trim() || "";
    if (scheduler.adopt(job, { subject: note.dataset.checkSubject || "", topic: note.dataset.checkTopic || "", first: 2000 })) {
      show({ id: `check-${job}`, kind: "info", subject: note.dataset.checkSubject || "", text: words });
    }
    note.remove();
    try {
      const address = new URL(location.href);
      address.searchParams.delete("check_job");
      address.searchParams.delete("check_result");
      if (address.hash === "#check-result") address.hash = "";
      history.replaceState(history.state, "", address.pathname + address.search + address.hash);
    } catch {
      // the address stays as it was
    }
  }

  window.towChecks = {
    begin: (meta) => scheduler.begin(meta),
    acknowledge: (action, ack) => scheduler.acknowledge(action, ack),
    forget: (action) => scheduler.forget(action),
    count: () => scheduler.count(),
    scheduler,
  };
  scheduler.wake();
})();
