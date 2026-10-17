// One state on every open page (tow.web.sync): the page asks GET /sync.json for the revision of
// every area it shows and gets, for the areas whose revision differs from the ones it has applied,
// what they are now. Readers of each area put it on the page: Home's rows, the checks waiting or
// running, the header, "Needs attention", the countdowns, the Undo, the theme, the language and
// the version. Without a script every page stays as the server drew it; a reload shows the truth.
//
// The schedule: one request every 5 s while the tab is visible (the first at a random moment of
// those 5 s, so that tabs opened together do not ask together); none while it is hidden; one at
// once when it is shown again, comes back from the back/forward cache or the network returns.
// One request at a time, 10 s at most; a wish to ask while one is on its way is remembered once.
// A failed or timed-out request waits 5, 10, 20, 40, then 60 s (a little more or less, at random),
// and never less than the server's Retry-After. 304 Not Modified is a success: the areas stay as
// they are - and an area this page could not apply yet (a reader failed, its rows or fragments did
// not come) is not lost behind it: it is kept and asked again after later successful polls, 5, 10,
// 20, 40, then every 60 s after the failure. Another tab of this browser that changed something
// says so on a BroadcastChannel of this install and sign-in (a hint without any data): this tab
// asks within 250 ms, never sooner than its own waiting after a failure. Signed out (401, 403, or the Sign out form): every area
// this page learned is forgotten, every request of the page (the poll, Home's rows, the page area's
// fragments) is aborted, and an answer still on its way - also a body still being read - is ignored.
(() => {
  const VISIBLE_MS = 5000;
  const BACKOFF_MS = [5000, 10000, 20000, 40000, 60000];
  const TIMEOUT_MS = 10000;
  const HINT_MS = 250;
  const MAX_IDS = 200; // rows per /topics/rows request (the server's limit too)
  const ROWS_AT_ONCE = 2; // /topics/rows requests on their way at the same time
  const ROWS_BODY_CHARS = 48 * 1024; // a request body stays below the server's 64 KiB
  const RELOAD_GUARD_MS = 10 * 60 * 1000; // one automatic reload per reason and target

  // ------------------------------------------------------------------ the poll, without the page
  // Tests drive it with a fake clock and a fake request: ``request({url, etag})`` answers
  // {status, etag, date, retryAfter, body} or throws; ``abort`` (if the request returns one in
  // ``onAbort``) ends a request that took longer than 10 s.
  const createSyncClient = ({
    now = () => Date.now(),
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id),
    random = Math.random,
    hidden = () => false,
    request = async () => null,
    query = () => "", // extra query parameters (the page area), "" for none
    signedOut = () => {}, // (status): the session ended; called once
  } = {}) => {
    const subscribers = new Map(); // area -> [{fn, manual}]
    const pollers = []; // after every answer: (client, ok)
    let observed = {}; // area -> the revision the server said last
    let applied = {}; // area -> the revision this page shows (what ``have`` sends)
    let areas = {}; // area -> its last payload
    let retries = {}; // area -> {rev, failures, at, running}: offered, not applied yet
    let epoch = 0; // a new one at sign-out: answers of an older one are ignored
    let stopped = false;
    let inFlight = false;
    let pending = false; // asked again while a request was on its way
    let timer = 0;
    let hintTimer = 0;
    let failures = 0;
    let waitUntil = 0; // no request before this (a failure's backoff)
    let serverUntil = 0; // nor before this (the server's Retry-After)
    let lastUrl = "";
    let lastEtag = "";
    let abortInFlight = null; // ends the request on its way (a sign-out)
    let offset = 0; // the server's clock minus this one (ms)
    let tzNow = null;
    let polls = 0;

    const haveParam = () => Object.entries(applied)
      .filter(([, rev]) => typeof rev === "string" && /^[0-9a-f]{32}$/.test(rev))
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([area, rev]) => `${area}:${rev}`)
      .join(",");
    const url = () => {
      const params = new URLSearchParams();
      const have = haveParam();
      if (have) params.set("have", have);
      const extra = query();
      if (extra) new URLSearchParams(extra).forEach((value, key) => params.set(key, value));
      const text = params.toString();
      return `/sync.json${text ? `?${text}` : ""}`;
    };
    const plan = (at) => {
      clearTimer(timer);
      timer = 0;
      if (stopped || hidden()) return;
      timer = setTimer(poll, Math.max(0, at - now()));
    };
    const succeeded = () => {
      failures = 0;
      waitUntil = 0;
      if (pending) {
        pending = false;
        plan(now());
      } else {
        plan(now() + VISIBLE_MS);
      }
    };
    const failed = (retryAfterMs = 0) => {
      failures += 1;
      const step = BACKOFF_MS[Math.min(failures, BACKOFF_MS.length) - 1];
      const wait = Math.max(Math.round(step * (0.8 + 0.4 * random())), retryAfterMs);
      waitUntil = now() + wait;
      if (retryAfterMs) serverUntil = now() + retryAfterMs;
      pending = false; // the wait is the answer to any wish to ask meanwhile
      plan(waitUntil);
    };
    // An area is offered to its readers; it is applied (named in ``have``) once every reader took
    // it: none threw, and a reader's promise did not settle false or fail. Otherwise it is kept and
    // offered again after a later successful poll - also a 304, which brings it no more - 5, 10, 20,
    // 40, then every 60 s, never sooner; a newer revision is offered at once. A ``manual`` area is
    // its owner's: applied with ack(), asked again by the owner (Home's rows, the page area).
    const offer = (area, payload, nowTs) => {
      const known = retries[area];
      const same = Boolean(known && known.rev === payload.rev);
      if (same && (known.running || now() < known.at)) return;
      const state = same ? known : { rev: payload.rev, failures: 0, at: 0, running: false };
      delete retries[area];
      const readers = subscribers.get(area) || [];
      const mine = epoch;
      let ok = true;
      const waits = [];
      for (const reader of readers) {
        try {
          const result = reader.fn(payload, { nowTs, client: api });
          if (!reader.manual && typeof result?.then === "function") waits.push(result);
        } catch {
          ok = false;
        }
      }
      if (readers.some((reader) => reader.manual)) return;
      const failed = () => {
        state.failures += 1;
        state.at = now() + BACKOFF_MS[Math.min(state.failures, BACKOFF_MS.length) - 1];
        state.running = false;
        retries[area] = state;
      };
      if (!ok) {
        failed();
        return;
      }
      if (!waits.length) {
        applied[area] = payload.rev;
        return;
      }
      state.running = true;
      retries[area] = state;
      Promise.all(waits).then((results) => results.every((result) => result !== false), () => false).then((done) => {
        if (mine !== epoch || retries[area] !== state) return; // signed out, or a newer revision meanwhile
        if (!done) {
          failed();
          return;
        }
        delete retries[area];
        applied[area] = payload.rev;
      });
    };
    const offerAgain = () => {
      for (const [area, state] of Object.entries(retries)) {
        const payload = areas[area];
        if (!payload || payload.rev !== state.rev || applied[area] === state.rev) delete retries[area];
        else if (!state.running && now() >= state.at) offer(area, payload, (now() + offset) / 1000);
      }
    };
    const deliver = (body) => {
      const changed = body?.areas && typeof body.areas === "object" ? body.areas : {};
      for (const [area, payload] of Object.entries(changed)) {
        if (!payload || typeof payload.rev !== "string") continue;
        observed[area] = payload.rev;
        areas[area] = payload;
      }
      for (const [area, payload] of Object.entries(changed)) {
        if (payload && typeof payload.rev === "string") offer(area, payload, body.now_ts);
      }
    };
    const told = (ok) => pollers.forEach((fn) => {
      try {
        fn(api, ok);
      } catch {
        // one broken view never stops the others
      }
    });
    const end = (status) => {
      if (stopped) return;
      stopped = true;
      epoch += 1;
      clearTimer(timer);
      clearTimer(hintTimer);
      timer = 0;
      hintTimer = 0;
      observed = {};
      applied = {};
      areas = {};
      retries = {};
      lastUrl = "";
      lastEtag = "";
      const aborting = abortInFlight;
      abortInFlight = null;
      aborting?.(); // the request on its way: its answer is not used anyway
      signedOut(status);
    };
    const poll = async () => {
      timer = 0;
      if (stopped || hidden()) return;
      if (inFlight) {
        pending = true;
        return;
      }
      inFlight = true;
      pending = false;
      polls += 1;
      const mine = epoch;
      const address = url();
      const etag = address === lastUrl ? lastEtag : "";
      let abort = null;
      let timeout = 0;
      let answer = null;
      try {
        answer = await Promise.race([
          request({ url: address, etag, onAbort: (fn) => { abort = fn; abortInFlight = fn; } }),
          new Promise((resolve) => { timeout = setTimer(() => resolve({ status: 0, timedOut: true }), TIMEOUT_MS); }),
        ]);
      } catch {
        answer = null;
      }
      clearTimer(timeout);
      if (answer?.timedOut) abort?.();
      if (mine !== epoch) return; // signed out meanwhile: a late answer is not used
      abortInFlight = null;
      inFlight = false;
      if (stopped) return;
      const status = answer?.status || 0;
      if (status === 401 || status === 403) {
        end(status);
        return;
      }
      if (status === 304) {
        // HTTP Date has whole seconds: it corrects the clock only when it moved (set, slept).
        if (answer.date && Math.abs(answer.date - now() - offset) > 2000) offset = answer.date - now();
        offerAgain();
        succeeded();
        told(true);
        return;
      }
      if (status === 200 && answer.body?.ok === true) {
        lastUrl = address;
        lastEtag = answer.etag || "";
        if (Number.isFinite(answer.body.now_ts)) offset = answer.body.now_ts * 1000 - now();
        if (Number.isFinite(answer.body.tz_now)) tzNow = answer.body.tz_now;
        deliver(answer.body);
        offerAgain();
        succeeded();
        told(true);
        return;
      }
      failed(status === 429 || status === 503 ? Math.max(0, Number(answer?.retryAfter) || 0) * 1000 : 0);
      told(false);
    };
    // Ask now: shown again, back from the cache, online again (``kind`` "show"), or another tab's
    // hint ("hint", coalesced within 250 ms). A hint never cuts a failure's wait short, and nothing
    // cuts the server's Retry-After short.
    const nudge = (kind = "show") => {
      if (stopped || hidden()) return;
      if (inFlight) {
        pending = true;
        return;
      }
      const at = now();
      if (at < serverUntil) return;
      if (kind === "hint") {
        if (at < waitUntil || hintTimer) return;
        hintTimer = setTimer(() => {
          hintTimer = 0;
          if (!stopped && !hidden() && !inFlight && now() >= waitUntil) plan(now());
        }, HINT_MS);
        return;
      }
      plan(at);
    };
    const api = {
      // A reader of ``area``: fn(payload, {nowTs, client}). ``manual``: the page applies the area
      // later (Home's rows, fetched first) and says so with ack(area, rev).
      on: (area, fn, { manual = false } = {}) => {
        if (!subscribers.has(area)) subscribers.set(area, []);
        subscribers.get(area).push({ fn, manual });
        if (areas[area]) {
          try {
            fn(areas[area], { nowTs: (now() + offset) / 1000, client: api });
            if (!manual) applied[area] = areas[area].rev;
          } catch {
            // the next answer brings it again
          }
        }
      },
      // After every answer (ok: true for 200 and 304, false for a failure).
      onPoll: (fn) => { pollers.push(fn); },
      ack: (area, rev) => {
        if (!stopped && typeof rev === "string") applied[area] = rev;
      },
      // Start: the first request at a random moment of the first 5 s.
      start: () => {
        if (!stopped && !timer && !inFlight) plan(now() + Math.floor(random() * VISIBLE_MS));
      },
      nudge,
      now: () => nudge("show"),
      hidden: () => {
        clearTimer(timer);
        timer = 0;
      },
      signOut: (status = 0) => end(status),
      area: (name) => areas[name] || null,
      serverNow: () => now() + offset,
      tzNow: () => tzNow,
      observed: (name) => observed[name] || "",
      applied: (name) => applied[name] || "",
      stopped: () => stopped,
      backingOff: () => now() < waitUntil,
      polls: () => polls,
      url,
    };
    return api;
  };

  // ------------------------------------------------------------------ Home's rows, without the page
  // What the topics area says, put on the page: rows the server no longer has go, new ones come in
  // their place, changed ones are drawn again from /topics/rows (at most 200 a request, two
  // requests at a time). A row is fetched for the revision the area named; an answer drawn from
  // another revision is not used (the next poll asks again), and a failed or too slow request is
  // asked again by ``settle``: the area counts as applied only once every row of it is on the
  // page. A row under the owner's hands (``isProtected``) is never replaced or removed: it gets a
  // note and is drawn again (or removed) once the work ends (``settle``).
  const createRowSync = ({
    rows, // () => the row elements in the page's order
    idOf = (row) => String(row.id || "").replace(/^row-/, ""),
    hashOf = (row) => row.dataset?.rowHash || "",
    fetchRows, // (ids, rev) => Promise<{status, body}>
    isProtected = () => false,
    place, // (row | null, fragment {id, html, hash}, beforeId) => the row now on the page, or null
    removeRow, // (row) => void
    note = () => {}, // (row, "changed" | "removed" | "")
    applied = () => {}, // ({order, placed, removed}) after a batch: the list's tools follow
    ack = () => {}, // (rev): every row of this revision is on the page
    retry = () => {}, // an answer from a newer revision: ask the server again soon
    signedOut = () => {},
    emptied = () => {}, // (hasRows): Home turned empty (false) or got its first topic (true); null: neither
    now = () => Date.now(),
  } = {}) => {
    let generation = 0;
    let current = null; // {rev, order, acked}
    const later = new Set(); // ids to draw again once free (protected when their change came, a job ended)
    const kept = new Set(); // ids the server removed, kept under the owner's hands
    // Rows of the current revision whose request failed: asked again by ``settle`` 5, 10, 20, 40,
    // then every 60 s after the failure; the revision is applied once they are all on the page.
    let failed = new Set();
    let failures = 0;
    let retryAt = 0;
    const missed = (ids) => {
      failed = new Set(ids);
      failures += 1;
      retryAt = now() + BACKOFF_MS[Math.min(failures, BACKOFF_MS.length) - 1];
    };
    const find = (id) => rows().find((row) => idOf(row) === id) || null;
    const nextOnPage = (order, id) => {
      const index = order.indexOf(id);
      const present = new Set(rows().map(idOf));
      for (let i = index + 1; i < order.length; i += 1) if (present.has(order[i])) return order[i];
      return "";
    };
    const removeOrKeep = (row) => {
      const id = idOf(row);
      if (isProtected(row)) {
        kept.add(id);
        note(row, "removed");
        return false;
      }
      kept.delete(id);
      later.delete(id);
      removeRow(row);
      return true;
    };
    const batches = (ids) => {
      const out = [];
      let batch = [];
      let size = 0;
      for (const id of ids) {
        if (batch.length >= MAX_IDS || size + id.length + 4 > ROWS_BODY_CHARS) {
          out.push(batch);
          batch = [];
          size = 0;
        }
        batch.push(id);
        size += id.length + 4;
      }
      if (batch.length) out.push(batch);
      return out;
    };
    // Fetch ``ids`` for revision ``rev`` and put them in; true when every one of them arrived.
    const draw = async (ids, rev, order, mine) => {
      const queue = batches(ids);
      const result = { complete: true, placed: [], removed: [], failed: [], stale: false };
      const worker = async () => {
        while (queue.length && mine === generation) {
          const batch = queue.shift();
          let answer = null;
          try {
            answer = await fetchRows(batch, rev);
          } catch {
            answer = null;
          }
          if (mine !== generation) return;
          if (answer?.status === 401 || answer?.status === 403) {
            result.complete = false;
            generation += 1;
            signedOut(answer.status);
            return;
          }
          const body = answer?.status === 200 ? answer.body : null;
          if (!body || body.ok !== true || !Array.isArray(body.rows)) {
            result.complete = false; // asked again by ``settle``
            result.failed.push(...batch);
            continue;
          }
          if (body.rev !== rev || body.stale) {
            result.complete = false; // another revision: never mixed in
            result.stale = true;
            retry();
            continue;
          }
          for (const fragment of body.rows) {
            if (!fragment || typeof fragment.id !== "string" || typeof fragment.html !== "string") continue;
            const row = find(fragment.id);
            // Checked again right before: the owner may have started working in it meanwhile.
            if (row && isProtected(row)) {
              if (hashOf(row) !== fragment.hash) {
                later.add(fragment.id);
                note(row, "changed");
              }
              continue;
            }
            const now = place(row, fragment, row ? "" : nextOnPage(order, fragment.id));
            if (now) {
              later.delete(fragment.id);
              kept.delete(fragment.id);
              note(now, "");
              result.placed.push(fragment.id);
            }
          }
          const listed = new Set(Array.isArray(body.order) ? body.order : order);
          for (const id of Array.isArray(body.missing) ? body.missing : []) {
            const row = find(id);
            if (row && !listed.has(id) && removeOrKeep(row)) result.removed.push(id);
          }
          if (Array.isArray(body.deferred) && body.deferred.length) queue.push(...batches(body.deferred.filter((id) => typeof id === "string")));
        }
      };
      await Promise.all(Array.from({ length: ROWS_AT_ONCE }, worker));
      if (mine !== generation) result.complete = false;
      return result;
    };
    // The topics area: {rev, full, order, changed, removed} or, after a restart or a long sleep,
    // the whole manifest {rev, full: true, order, rows: {id: hash}}.
    const topics = async (payload) => {
      if (!payload || typeof payload.rev !== "string" || !Array.isArray(payload.order)) return false;
      generation += 1;
      const mine = generation;
      const order = payload.order.filter((id) => typeof id === "string");
      if (current?.rev !== payload.rev) failures = 0;
      failed = new Set();
      current = { rev: payload.rev, order, acked: false };
      const listed = new Set(order);
      const onPage = new Map(rows().map((row) => [idOf(row), row]));
      if (!onPage.size && order.length) {
        emptied(true); // the first topic: the page with its list (reloaded when idle)
        return false;
      }
      const removed = [];
      for (const [id, row] of onPage) {
        if (!listed.has(id) && removeOrKeep(row)) removed.push(id);
      }
      if (!order.length) {
        // The last topic went: the page with its first steps (reloaded when idle).
        emptied(onPage.size ? false : null);
        applied({ order, placed: [], removed });
        current.acked = true;
        ack(payload.rev);
        return true;
      }
      emptied(null);
      const wanted = new Set();
      if (payload.full) {
        const hashes = payload.rows && typeof payload.rows === "object" ? payload.rows : {};
        order.forEach((id) => {
          const row = onPage.get(id);
          if (!row || hashOf(row) !== hashes[id]) wanted.add(id);
        });
      } else {
        (Array.isArray(payload.changed) ? payload.changed : []).forEach((id) => { if (listed.has(id)) wanted.add(id); });
        order.forEach((id) => { if (!onPage.has(id)) wanted.add(id); });
      }
      // Rows kept for the owner come back into the list once the server has them again (an Undo).
      for (const id of [...kept]) {
        if (listed.has(id)) {
          kept.delete(id);
          wanted.add(id);
        }
      }
      later.forEach((id) => { if (listed.has(id)) wanted.add(id); });
      const result = await draw([...wanted], payload.rev, order, mine);
      if (mine !== generation) return false;
      result.removed.unshift(...removed);
      applied({ order, ...result });
      if (result.complete) {
        failures = 0;
        current.acked = true;
        ack(payload.rev);
      } else if (!result.stale && result.failed.length) {
        missed(result.failed);
      }
      return result.complete;
    };
    // Rows waiting for the owner, rows a check started or ended in, and rows whose request failed:
    // drawn again once free (the failed ones once their wait is over).
    let settling = null;
    let again = false;
    const settle = async () => {
      if (settling) {
        again = true; // asked for more meanwhile: one more pass right after
        return settling;
      }
      if (!current) return undefined;
      settling = (async () => {
        const mine = generation;
        const { rev, order } = current;
        for (const id of [...kept]) {
          const row = find(id);
          if (!row) kept.delete(id);
          else removeOrKeep(row);
        }
        const free = [...later].filter((id) => {
          const row = find(id);
          if (!row) {
            later.delete(id);
            return false;
          }
          return !isProtected(row) && order.includes(id);
        });
        const asking = failed.size && now() >= retryAt ? [...failed] : [];
        const ids = [...new Set([...free, ...asking])];
        if (!ids.length) return;
        const result = await draw(ids, rev, order, mine);
        if (mine !== generation) return;
        applied({ order, ...result });
        if (!asking.length || result.stale) return;
        const still = result.failed.filter((id) => failed.has(id));
        if (still.length) {
          missed(still);
          return;
        }
        failed = new Set();
        failures = 0;
        if (!current.acked) {
          current.acked = true;
          ack(rev);
        }
      })();
      try {
        await settling;
      } finally {
        settling = null;
      }
      if (again) {
        again = false;
        return settle();
      }
      return undefined;
    };
    return {
      topics,
      settle,
      redraw: (ids) => { ids.forEach((id) => later.add(String(id))); return settle(); },
      cancel: () => { generation += 1; current = null; later.clear(); kept.clear(); failed = new Set(); failures = 0; },
      waiting: () => [...later],
      kept: () => [...kept],
    };
  };

  // ------------------------------------------------------------------ reloads, without the page
  // A page reloads by itself only when nothing would be lost (``idle``): for another language once,
  // for another version after 5 s that any activity cancels. A page that is not idle says why it
  // should be reloaded instead (``notice``) and reloads once it is idle again. The same reason and
  // target reload at most once in ten minutes (a server that keeps saying "another version" never
  // loops the page); without storage there is no automatic reload at all, only the notice.
  const createReloader = ({
    idle = () => true,
    reload = () => {},
    notice = () => {}, // (reason, target | ""): "" removes it
    storage = null,
    now = () => Date.now(),
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id),
    key = "tow.sync.reload",
  } = {}) => {
    const wanted = new Map(); // reason -> {target, delay, noticed}
    let timer = 0;
    let timerReason = "";
    const recent = (reason, target) => {
      if (!storage) return true;
      try {
        const last = JSON.parse(storage.getItem(key) || "null");
        return Boolean(last && last.reason === reason && last.target === target && now() - Number(last.at) < RELOAD_GUARD_MS);
      } catch {
        return true;
      }
    };
    const mark = (reason, target) => {
      try {
        storage.setItem(key, JSON.stringify({ reason, target, at: now() }));
        return true;
      } catch {
        return false;
      }
    };
    const say = (reason) => {
      const wish = wanted.get(reason);
      if (!wish || wish.noticed) return;
      wish.noticed = true;
      notice(reason, wish.target);
    };
    const fire = () => {
      timer = 0;
      const reason = timerReason;
      const wish = wanted.get(reason);
      if (!wish) return;
      if (!idle()) {
        say(reason); // the next idle moment starts it again
        return;
      }
      if (recent(reason, wish.target) || !mark(reason, wish.target)) {
        say(reason);
        return;
      }
      reload();
    };
    const check = () => {
      if (timer) return;
      for (const [reason, wish] of wanted) {
        if (!recent(reason, wish.target) && idle()) {
          timerReason = reason;
          timer = setTimer(fire, wish.delay);
          return;
        }
        say(reason);
      }
    };
    return {
      // ``announce``: said at once, also by an idle page (a new version: why it reloads in 5 s).
      want: (reason, target, { delay = 5000, announce = false } = {}) => {
        const known = wanted.get(reason);
        if (known && known.target === target) {
          check();
          return;
        }
        if (known?.noticed) notice(reason, "");
        if (known && timerReason === reason && timer) {
          clearTimer(timer);
          timer = 0;
        }
        wanted.set(reason, { target, delay, noticed: false });
        if (announce) say(reason);
        check();
      },
      drop: (reason) => {
        const wish = wanted.get(reason);
        if (!wish) return;
        wanted.delete(reason);
        if (wish.noticed) notice(reason, "");
        if (timerReason === reason && timer) {
          clearTimer(timer);
          timer = 0;
        }
      },
      // Something happened on the page: a reload about to happen waits for the next idle moment,
      // and the page says why it should be reloaded meanwhile.
      activity: () => {
        if (!timer) return;
        clearTimer(timer);
        timer = 0;
        say(timerReason);
      },
      check,
      pending: () => Boolean(timer),
      wanted: (reason) => wanted.get(reason)?.target || "",
    };
  };

  window.TowSync = { createSyncClient, createRowSync, createReloader };
  if (typeof document === "undefined" || !document.getElementById?.("msg-host")) return;

  // ------------------------------------------------------------------ the page
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
  const host = document.getElementById("msg-host");
  const namespace = host.dataset.namespace || "local";
  const session = (() => {
    try {
      return window.sessionStorage;
    } catch {
      return null;
    }
  })();
  const randomId = () => {
    const bytes = new Uint8Array(12);
    try {
      crypto.getRandomValues(bytes);
    } catch {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  };
  // This tab's name for the server's rate (a bucket per tab on this computer), kept by the tab.
  const tab = (() => {
    try {
      const known = session?.getItem("tow.sync.tab");
      if (known && /^[0-9a-f]{24}$/.test(known)) return known;
      const made = randomId();
      session?.setItem("tow.sync.tab", made);
      return made;
    } catch {
      return randomId();
    }
  })();
  const show = (record, options) => window.towMessages?.showMessage?.(record, options);
  // One session for every request and settler of this page. Signing out (the form, or a 401/403
  // to any of its requests) ends it: every request on its way is aborted, and its answer - also a
  // body still being read - is not used; nothing is asked any more; what keeps work of its own
  // (pages.js) is told once (``onSignOut``).
  const newController = () => (typeof AbortController === "function" ? new AbortController() : null);
  const signIn = { epoch: 0, ended: false, controller: newController(), listeners: [] };
  const endSession = () => {
    if (signIn.ended) return;
    signIn.ended = true;
    signIn.epoch += 1;
    try {
      signIn.controller?.abort();
    } catch {
      // aborted already
    }
    signIn.listeners.splice(0).forEach((fn) => {
      try {
        fn();
      } catch {
        // one broken listener never stops the others
      }
    });
  };
  const onSignOut = (fn) => {
    if (signIn.ended) fn();
    else signIn.listeners.push(fn);
  };
  // A fragment request (Home's rows, the page area of pages.js): 10 s at most, its body included;
  // one that takes longer is aborted and answers {status: 0} (failed: asked again later), as does
  // one the session ended under (``cancelled``).
  const CANCELLED = { status: 0, body: null, cancelled: true };
  const request = async (url, init = {}) => {
    const mine = signIn.epoch;
    if (signIn.ended) return { ...CANCELLED };
    const controller = newController();
    const stop = () => controller?.abort();
    signIn.controller?.signal?.addEventListener?.("abort", stop);
    let timer = 0;
    const late = new Promise((resolve) => {
      timer = window.setTimeout(() => {
        stop();
        resolve({ status: 0, body: null, timedOut: true });
      }, TIMEOUT_MS);
    });
    const asked = (async () => {
      const response = await fetch(url, { ...init, signal: controller?.signal });
      const body = response.status === 200 ? await response.json().catch(() => null) : null;
      return { status: response.status, body };
    })().catch(() => ({ status: 0, body: null }));
    try {
      const answer = await Promise.race([asked, late]);
      return mine === signIn.epoch ? answer : { ...CANCELLED }; // the session ended meanwhile
    } finally {
      window.clearTimeout(timer);
      signIn.controller?.signal?.removeEventListener?.("abort", stop);
    }
  };
  let rowSync = null; // Home's rows (below)
  let pageQuery = ""; // page=/sites… once pages.js follows this page's area

  // What the page is doing that a reload or a row drawn again would destroy.
  const FIELDS = "input, select, textarea, [contenteditable]";
  const changedField = (field) => {
    if (field.type === "hidden" || field.type === "submit" || field.type === "button" || field.disabled) return false;
    if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
    if (field.type === "file") return Boolean(field.files?.length);
    if (field.tagName === "SELECT") return [...(field.options || [])].some((option) => option.selected !== option.defaultSelected);
    return typeof field.defaultValue === "string" && field.value !== field.defaultValue;
  };
  const drafts = () => [...document.querySelectorAll("form")].some((form) =>
    (form.getAttribute("method") || "").toLowerCase() === "post"
    && !form.matches("[data-theme-form], [data-auto-submit]")
    && [...form.querySelectorAll("input, select, textarea")].some(changedField));
  const idle = () => {
    if (document.hidden) return false;
    if (document.querySelector("dialog[open], details.row-edit[open], details#new[open]")) return false;
    if (document.querySelector('form[data-dirty="1"], form[data-submitting="1"], [aria-busy="true"]')) return false;
    if (document.activeElement?.matches?.(FIELDS)) return false;
    return !drafts();
  };
  let pressed = null; // the row the pointer is pressed in
  let touched = { row: null, at: 0 }; // the row the owner last typed, clicked or moved the focus in
  const TOUCH_MS = 10000;
  // A row under the owner's hands: its edit panel open, unsaved or sent words, a panel still
  // loading, a field with the focus, the pointer pressed in it or on its buttons, or a button or
  // its summary with the focus while the owner used the row in the last 10 s (a focus left on a
  // row's summary after a save does not hold it for ever: it is drawn again, the focus kept).
  const protectedRow = (row) => {
    if (!row) return false;
    const active = document.activeElement;
    const focused = Boolean(active && active !== document.body && row.contains(active));
    return Boolean(
      row.querySelector("details.row-edit[open]")
      || row.querySelector('form[data-dirty="1"], form[data-submitting="1"], [aria-busy="true"], [data-loading]')
      || (focused && active.matches?.(FIELDS))
      || (focused && touched.row === row && Date.now() - touched.at < TOUCH_MS)
      || pressed === row
      || row.querySelector(".row-ops:hover, summary:hover"),
    );
  };

  const timers = { setTimer: (fn, ms) => window.setTimeout(fn, ms), clearTimer: (id) => window.clearTimeout(id) };
  const client = createSyncClient({
    ...timers,
    hidden: () => document.hidden,
    request: async ({ url, etag, onAbort }) => {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      onAbort(() => controller?.abort());
      const headers = { Accept: "application/json", "X-TOW-Tab": tab };
      if (etag) headers["If-None-Match"] = etag;
      const response = await fetch(url, { cache: "no-store", credentials: "same-origin", headers, signal: controller?.signal });
      const date = Date.parse(response.headers.get("Date") || "");
      const answer = {
        status: response.status,
        etag: response.headers.get("ETag") || "",
        date: Number.isFinite(date) ? date : 0,
        retryAfter: Number(response.headers.get("Retry-After")) || 0,
        body: null,
      };
      if (response.status === 200) answer.body = await response.json().catch(() => null);
      return answer;
    },
    // The page area (Sites, Settings, History: pages.js) once a page asks for it: its path and
    // History's filters go with every poll (the server's request variant).
    query: () => pageQuery,
    signedOut: (status) => {
      endSession();
      rowSync?.cancel();
      window.towChecks?.scheduler?.stop?.();
      window.towMessages?.signOut?.();
      if (status === 401) show({ id: "checks-signed-out", kind: "warn", text: t("js.checks.signed_out"), href: "/login", link: t("js.checks.sign_in") });
    },
  });

  // Same-browser tabs of this install and sign-in: a hint, never data.
  let channel = null;
  try {
    if (typeof BroadcastChannel === "function") {
      channel = new BroadcastChannel(`tow-sync.${namespace}`);
      channel.onmessage = (event) => { if (event.data === "changed") client.nudge("hint"); };
    }
  } catch {
    channel = null;
  }
  // Said at once (the page may be about to open the next one), then at most once per 250 ms.
  let hinting = 0;
  let hinted = 0;
  const post = () => {
    hinted = Date.now();
    try {
      channel?.postMessage("changed");
    } catch {
      // a closed channel: the other tabs see it within their 5 s
    }
  };
  const hint = () => {
    if (!channel || client.stopped()) return;
    const wait = hinted + HINT_MS - Date.now();
    if (wait <= 0) post();
    else if (!hinting) hinting = window.setTimeout(() => { hinting = 0; post(); }, wait);
  };

  document.addEventListener("visibilitychange", () => (document.hidden ? client.hidden() : client.nudge("show")));
  window.addEventListener("pageshow", (event) => { if (event.persisted) client.nudge("show"); });
  window.addEventListener("online", () => client.nudge("show"));
  document.addEventListener("submit", (event) => {
    if (event.target?.getAttribute?.("action") === "/logout") {
      client.signOut(0);
      try {
        channel?.close();
      } catch {
        // closed already
      }
      channel = null;
    }
  });

  // ---- reloads (language, version, an empty Home) and their notices
  const reloader = createReloader({
    ...timers,
    idle,
    reload: () => window.location.reload(),
    storage: session,
    notice: (reason, target) => {
      if (reason === "rows") return; // an empty Home says nothing: it reloads when idle
      const id = `sync-${reason}`;
      if (!target) {
        window.towMessages?.remove?.(id);
        return;
      }
      const text = reason === "version" ? t("js.sync.updated", { version: target.split("|")[0] }) : t("js.sync.reload_language");
      show({ id, kind: "info", text, href: location.pathname + location.search, link: t("js.messages.reload") }, { replace: true });
    },
  });
  let activityTimer = 0;
  const activity = () => {
    reloader.activity();
    window.clearTimeout(activityTimer);
    activityTimer = window.setTimeout(() => {
      reloader.check();
      // Rows that waited for the owner, and an order or a filter that waited: now, if free.
      if (!rowSync || !(rowSync.waiting().length || rowSync.kept().length || window.towList?.pending?.())) return;
      rowSync.settle().then(() => window.towList?.refresh?.({ keep: true }), () => {});
    }, 300);
  };
  // The owner's own gestures make a row "used" (a key - its keyup lands where Tab took the focus -,
  // the pointer, typing, a change, a submit); a focus the page put back after a save (app.js) is
  // no gesture: it never holds a row by itself.
  ["input", "keydown", "keyup", "pointerdown", "submit", "change"].forEach((name) => document.addEventListener(name, (event) => {
    const row = event.target?.closest?.(".row-wrap[id^='row-']") || null;
    if (name === "pointerdown") pressed = row;
    if (row) touched = { row, at: Date.now() };
    activity();
  }, true));
  document.addEventListener("focusin", activity, true);
  ["pointerup", "pointercancel", "focusout", "reset", "pointerout"].forEach((name) => document.addEventListener(name, () => {
    if (name !== "pointerout") pressed = null;
    activity();
  }, true));
  document.addEventListener("toggle", activity, true);

  // ---- the header: site icons, the torrent client, the messenger bell (classes, tooltips, words)
  // in the words base.html writes them with (base.header.*, web.header.*).
  const TONES = ["ok", "warn", "bad", "mut", "new"];
  const toneOf = (value) => (TONES.includes(value) ? value : "mut");
  const SITE_WORDS = { ok: "base.header.site_ok", warn: "base.header.site_warn" };
  const CLIENT_WORDS = { ok: "web.header.connected", bad: "web.header.disconnected" };
  const ownText = (node) => {
    const said = node.querySelector(".sr-only")?.textContent || "";
    const all = node.textContent || "";
    return said && all.endsWith(said) ? all.slice(0, all.length - said.length) : all;
  };
  const label = (node, text, said) => {
    if (node.textContent === `${text}${said}`) return;
    const sr = node.querySelector(".sr-only") || Object.assign(document.createElement("span"), { className: "sr-only" });
    sr.textContent = said;
    node.replaceChildren(text, sr);
  };
  const patchHeader = (h) => {
    const strip = document.querySelector(".hdr-sites");
    if (strip && h.sites && typeof h.sites === "object") {
      const titles = h.site_titles && typeof h.site_titles === "object" ? h.site_titles : {};
      const present = [...strip.querySelectorAll(".trk")];
      // A site's icon by its name once this script has drawn it, else by the title it shows.
      const byName = new Map(present.filter((node) => node.dataset.hdrSite).map((node) => [node.dataset.hdrSite, node]));
      const byTitle = new Map(present.filter((node) => !node.dataset.hdrSite).map((node) => [ownText(node), node]));
      const busy = present.some((node) => node.classList.contains("busy"));
      const wanted = Object.entries(h.sites).map(([name, tone]) => {
        const title = String(titles[name] || name);
        let node = byName.get(name) || byTitle.get(title);
        byTitle.delete(title);
        if (!node) node = document.createElement("span");
        node.dataset.hdrSite = name;
        const shade = toneOf(tone);
        const state = t(SITE_WORDS[shade] || "base.header.not_checked");
        node.className = `trk ${shade}${busy ? " busy" : ""}`;
        node.title = `${title}: ${state}`;
        label(node, title, `: ${state}`);
        return node;
      });
      const now = [...strip.querySelectorAll(".trk")];
      if (now.length !== wanted.length || now.some((node, index) => node !== wanted[index])) strip.replaceChildren(...wanted);
    }
    const services = document.querySelector(".hdr-svc");
    const clientNode = services ? [...services.querySelectorAll(".trk")].find((node) => !node.classList.contains("trk-ico")) : null;
    if (clientNode && typeof h.client_label === "string") {
      const shade = toneOf(h.qbit_tone);
      clientNode.className = `trk ${shade}`;
      clientNode.title = `${h.client_label}: ${h.qbit ?? ""}`;
      label(clientNode, h.client_label, `: ${t(CLIENT_WORDS[shade] || "base.header.not_checked")}`);
    }
    const bell = services?.querySelector(".trk-ico");
    if (bell && typeof h.bot === "string") {
      const shade = h.bot_ok === null || h.bot_ok === undefined ? "mut" : h.bot_ok ? "ok" : "bad";
      bell.setAttribute("class", `trk trk-ico ${shade}`);
      bell.title = h.bot;
      bell.setAttribute("aria-label", h.bot);
    }
  };
  client.on("header", patchHeader);

  // ---- the checks waiting or running anywhere (another tab, another device, Check all)
  let jobsKnown = null; // topic -> "queued" | "running"
  const rowOf = (id) => (id ? document.getElementById(`row-${id}`) : null);
  const markRow = (id, status) => {
    const button = rowOf(id)?.querySelector('form[action$="/check"] button');
    if (!button) return;
    button.classList.toggle("spinning", status === "running");
    button.querySelector("use")?.setAttribute("href", status === "queued" ? "#i-clock" : "#i-refresh");
  };
  const shownStatus = (row) => {
    const button = row.querySelector('form[action$="/check"] button');
    if (!button) return "";
    if (button.classList.contains("spinning")) return "running";
    return button.querySelector("use")?.getAttribute("href") === "#i-clock" ? "queued" : "";
  };
  client.on("jobs", (payload) => {
    const waiting = Array.isArray(payload.waiting) ? payload.waiting : [];
    const now = new Map();
    let everything = false;
    for (const job of waiting) {
      if (!job || typeof job !== "object") continue;
      const topic = String(job.topic_id || "");
      if (!topic) {
        everything = true;
        continue;
      }
      if (now.get(topic) !== "running") now.set(topic, job.status === "running" ? "running" : "queued");
    }
    const before = jobsKnown || new Map(
      [...document.querySelectorAll(".row-wrap[id^='row-']")].map((row) => [row.id.slice(4), shownStatus(row)]).filter(([, status]) => status),
    );
    const changed = new Set([...before.keys(), ...now.keys()].filter((id) => (before.get(id) || "") !== (now.get(id) || "")));
    jobsKnown = now;
    changed.forEach((id) => markRow(id, now.get(id) || ""));
    // The row's own words ("Checking…", its result) come with the row drawn again.
    if (changed.size) rowSync?.redraw([...changed]).then(() => window.towList?.refresh?.({ keep: true }), () => {});
    // Check all: its button turns and the header's sites wait, as in the tab that asked for it.
    const checkAll = document.querySelector('header.app form[action="/check"]');
    if (checkAll && checkAll.dataset.submitting !== "1") {
      checkAll.querySelector("button")?.classList.toggle("spinning", everything);
      document.querySelectorAll(".hdr-sites .trk, .hdr-clock").forEach((node) => node.classList.toggle("busy", everything));
    }
  });

  // ---- Undo: the one install-wide Undo, live in every tab, its time counted from the server's clock
  let undoStamp = host.dataset.undoStamp || "";
  client.on("undo", (payload, { nowTs }) => {
    const center = window.towMessages?.center;
    if (!center) return;
    const undo = payload.undo && typeof payload.undo === "object" ? payload.undo : null;
    const usable = undo && !undo.local_only && typeof undo.stamp === "string" && Number.isFinite(undo.until_ts);
    const fresh = usable && undo.stamp !== undoStamp;
    if (undoStamp && (!usable || undo.stamp !== undoStamp)) window.towMessages?.remove?.(`undo-${undoStamp}`);
    if (!usable) {
      undoStamp = "";
      center.setUndo({});
    } else {
      const serverNow = Number.isFinite(nowTs) ? nowTs * 1000 : client.serverNow();
      undoStamp = undo.stamp;
      center.setUndo({
        stamp: undo.stamp,
        until: Date.now() + (undo.until_ts * 1000 - serverNow),
        text: t("js.messages.undo_available", { what: String(undo.label || "") }),
      });
    }
    // A new Undo: its change's message is in the list (one request with the messages area's, if both
    // came in this answer); its time running on changes nothing there.
    if (fresh) window.towMessages?.refresh?.();
  });

  // ---- preferences: the theme (Auto follows each device), the language (another one reloads)
  client.on("config", (payload) => {
    const saving = document.querySelector('[data-theme-form][data-saving="1"]');
    if (!saving && ["light", "dark", "auto"].includes(payload.theme)) {
      const root = document.documentElement;
      if (payload.theme === "auto") delete root.dataset.theme;
      else if (root.dataset.theme !== payload.theme) root.dataset.theme = payload.theme;
    }
    const page = document.documentElement.lang || "";
    const resolved = String(payload.language_resolved || "");
    if (resolved && page && resolved !== page) reloader.want("language", resolved, { delay: 1000 });
    else {
      reloader.drop("language");
      window.towMessages?.remove?.("sync-language"); // said by this page before it was reloaded
    }
  });

  // ---- the version: another app or static version on the server than this page's
  const footer = document.querySelector("[data-page-version]");
  client.on("version", (payload) => {
    const app = String(payload.app || "");
    const statics = String(payload.static || "");
    const mine = footer?.dataset.pageVersion || "";
    const mineStatic = footer?.dataset.staticVersion || "";
    if (!app || !mine) return;
    if (app !== mine || (statics && mineStatic && statics !== mineStatic)) {
      reloader.want("version", `${app}|${statics}`, { delay: 5000, announce: true });
    } else {
      reloader.drop("version");
      window.towMessages?.remove?.("sync-version"); // said by this page before it was reloaded
    }
  });

  // ---- the messages area: their list is messages.js's (it reads its own feed). The area is applied
  // only once messages.js has taken that list (its refresh answers true); otherwise it is offered
  // again later, also behind 304s (the client's offer).
  client.on("messages", () => {
    const asked = window.towMessages?.refresh?.();
    return typeof asked?.then === "function" ? asked.then((taken) => taken === true, () => false) : true;
  });

  // ---- Home's rows
  const list = document.getElementById("topics");
  const listRows = () => (list ? [...list.querySelectorAll(".row-wrap[id^='row-']")] : []);
  // One visible row keeps its place on the screen while rows above it come and go.
  const anchorRow = () => {
    const top = Number.parseFloat(window.getComputedStyle?.(document.documentElement)?.getPropertyValue("--msg-top")) || 0;
    const row = listRows().find((each) => !each.hidden && each.getBoundingClientRect().bottom > top);
    return row ? { row, top: row.getBoundingClientRect().top } : null;
  };
  // The focus by what it is (the same button of the same row, the same field), when a change took it.
  const focusIdentity = () => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    return {
      element: active,
      row: active.closest?.(".row-wrap[id]")?.id || "",
      action: active.closest?.("form[action]")?.getAttribute("action") || "",
      name: active.getAttribute?.("name") || "",
      id: active.id || "",
      summary: Boolean(active.closest?.("summary")),
    };
  };
  const refocus = (was) => {
    if (!was || was.element.isConnected) return;
    if (document.activeElement && document.activeElement !== document.body) return; // the owner moved on
    let target = was.id ? document.getElementById(was.id) : null;
    const row = !target && was.row ? document.getElementById(was.row) : null;
    if (row && was.action) target = [...row.querySelectorAll("form[action]")].find((form) => form.getAttribute("action") === was.action)?.querySelector(was.name ? `[name="${was.name}"]` : "button");
    if (row && !target && was.summary) target = row.querySelector("summary"); // the row's one summary
    target?.focus?.({ preventScroll: true });
  };
  let batch = null; // {anchor, focus} while rows are put in
  const begin = () => {
    if (!batch) batch = { anchor: anchorRow(), focus: focusIdentity() };
  };
  const finish = () => {
    if (!batch) return;
    const { anchor, focus } = batch;
    batch = null;
    if (anchor?.row.isConnected) {
      const shift = anchor.row.getBoundingClientRect().top - anchor.top;
      if (shift) window.scrollBy(0, shift);
    }
    refocus(focus);
  };
  rowSync = list ? createRowSync({
    rows: listRows,
    fetchRows: (ids, rev) => request("/topics/rows", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-TOW-Tab": tab },
      body: JSON.stringify({ ids, rev }),
    }),
    isProtected: protectedRow,
    place: (row, fragment, beforeId) => {
      const template = document.createElement("template");
      template.innerHTML = fragment.html.trim();
      const fresh = template.content.querySelector(".row-wrap");
      if (!fresh || fresh.id !== `row-${fragment.id}`) return null;
      begin();
      if (fragment.hash) fresh.dataset.rowHash = fragment.hash;
      if (row) {
        // The running personal timer goes on in its own element.
        const timer = row.querySelector("[data-topic-timer]");
        const slot = fresh.querySelector("[data-topic-timer]");
        if (timer && slot) slot.replaceWith(timer);
        if (row.hidden) fresh.hidden = true;
        row.replaceWith(fresh);
      } else {
        const before = beforeId ? rowOf(beforeId) : null;
        const end = document.getElementById("list-empty");
        if (before?.parentNode === list) list.insertBefore(fresh, before);
        else if (end?.parentNode === list) list.insertBefore(fresh, end);
        else list.append(fresh);
      }
      window.towRows?.prepare?.(fresh);
      return fresh;
    },
    removeRow: (row) => {
      begin();
      row.remove();
    },
    note: (row, kind) => {
      let node = row.querySelector("[data-sync-note]");
      if (!kind) {
        node?.remove();
        delete row.dataset.syncState;
        return;
      }
      row.dataset.syncState = kind;
      if (!node) {
        node = document.createElement("p");
        node.className = "field-hint sync-note";
        node.setAttribute("role", "status");
        node.setAttribute("data-sync-note", "");
        const panel = row.querySelector("details.row-edit > .edit-panel");
        if (panel) panel.prepend(node);
        else row.querySelector("details.row-edit")?.append(node);
      }
      node.textContent = t(kind === "removed" ? "js.sync.row_removed" : "js.sync.row_changed");
    },
    applied: ({ order }) => {
      window.towList?.refresh?.({ order, keep: true });
      window.towRows?.timers?.();
      finish();
    },
    ack: (rev) => client.ack("topics", rev),
    retry: () => window.setTimeout(() => client.nudge("hint"), 1000),
    signedOut: (status) => client.signOut(status),
    emptied: (hasRows) => {
      if (hasRows === null) reloader.drop("rows");
      else reloader.want("rows", hasRows ? "some" : "none", { delay: 1000 });
    },
  }) : null;
  if (rowSync) {
    // A failure leaves the revision unapplied: the next answer brings it again.
    client.on("topics", (payload) => { rowSync.topics(payload).catch(() => {}); }, { manual: true });
  }

  // After every answer: a reload waiting for an idle page, rows waiting for the owner.
  client.onPoll((_, ok) => {
    if (!ok) return;
    reloader.check();
    rowSync?.settle();
  });

  // What /health.json said, from the areas this page has: app.js's readers (the countdown, the
  // personal timers, "Needs attention", TOW's zone) read the same fields as before.
  const health = () => ({
    ...(client.area("header") || {}),
    ok: true,
    now_ts: client.serverNow() / 1000,
    tz_now: client.tzNow(),
    attention: client.area("attention"),
    topic_timers: client.area("timers")?.timers || {},
  });

  window.towSync = {
    on: (area, fn, options) => client.on(area, fn, options),
    // The page area: ``{page, group, q}`` names it (pages.js); nothing: no page area.
    page: (params) => {
      const query = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => { if (value) query.set(key, String(value)); });
      pageQuery = query.toString();
    },
    onPoll: (fn) => client.onPoll(fn),
    request,
    onSignOut,
    health,
    now: () => client.now(),
    hint,
    idle,
    protectedRow,
    area: (name) => client.area(name),
    serverNow: () => client.serverNow(),
    tzNow: () => client.tzNow(),
    signOut: () => client.signOut(0),
    client,
    rows: rowSync,
    reloader,
  };
  // A page that opened after an action (its message came with it) tells the other tabs.
  if (host.querySelector("#flash")) hint();
  // Every reader has been added once the page's scripts have run.
  document.addEventListener("DOMContentLoaded", () => window.setTimeout(() => client.start(), 0));
  if (document.readyState === "complete") client.start();
})();
