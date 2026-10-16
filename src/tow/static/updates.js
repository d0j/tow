// Optional discovery: pages and health keep working when GitHub is unavailable.
(() => {
  const badge = document.querySelector("[data-release-badge]");
  if (!badge) return;
  const overlay = document.querySelector(".app-version");
  // Capture the loaded document's version, not the newer server's polling response.
  const pageVersion = document.querySelector("[data-page-version]")?.dataset?.pageVersion || "";
  let framePending = false;
  let settleTimer = 0;
  // What lies under the version badge: the browser's own hit test at a few points of it (the
  // middle first, then the corners; the first control found ends the look) - not every control
  // of the page measured on each scroll frame (a Home of 2000 rows has 12 000). Hidden controls
  // and closed accordions are not hit at all. Status pills count too: the badge covered "No
  // saved backups" and the edge of a card. One hit test costs ~14 ms on that Home, so the page
  // is looked at when a scroll, a resize or a burst of changes (typing a search hides and shows
  // rows, a sort moves them all) has settled, not on every frame of it (15 points per frame).
  const CONTROLS = "button,input,select,textarea,a,summary,td,.flash,.pill";
  const positionOverlay = () => {
    if (!overlay || framePending) return;
    framePending = true;
    window.requestAnimationFrame(() => {
      framePending = false;
      const box = overlay.getBoundingClientRect();
      const points = [
        [(box.left + box.right) / 2, (box.top + box.bottom) / 2], [box.left + 1, box.top + 1], [box.right - 1, box.top + 1],
        [box.left + 1, box.bottom - 1], [box.right - 1, box.bottom - 1],
      ];
      const obstructing = box.width > 2 && box.height > 2 && points.some(([x, y]) =>
        document.elementsFromPoint(x, y).some((element) => !overlay.contains(element) && element.closest?.(CONTROLS)));
      overlay.classList.toggle("is-obstructing", obstructing);
    });
  };
  const afterChanges = () => {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(positionOverlay, 150);
  };
  if (overlay) {
    window.addEventListener("scroll", afterChanges, { passive: true, capture: true });
    window.addEventListener("resize", afterChanges);
    document.addEventListener("toggle", afterChanges, true);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(afterChanges).observe(document.body);
    const main = document.querySelector("main");
    if (main && typeof MutationObserver !== "undefined") {
      // Countdowns rewrite their text every second; measuring every control for that would
      // force layout each second. Only added/removed elements and changed visibility count.
      const moved = (record) => record.type === "attributes" ?
        record.target.getAttribute(record.attributeName) !== record.oldValue :
        [...record.addedNodes, ...record.removedNodes].some((node) => node.nodeType === 1);
      new MutationObserver((records) => { if (records.some(moved)) afterChanges(); }).observe(main, {
        childList: true, subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ["hidden", "open", "class"],
      });
    }
    positionOverlay();
  }
  const status = document.querySelector("[data-release-status]");
  const check = document.querySelector("[data-release-check]");
  const notes = document.querySelector("[data-release-notes]");
  const command = document.querySelector("[data-release-command]");
  const history = document.querySelector("[data-update-history]");
  const install = document.querySelector("[data-release-install]");
  const progress = document.querySelector("[data-update-progress]");
  const support = document.querySelector("[data-update-support]");
  const unavailable = document.querySelector("[data-update-unavailable]");
  const rollback = document.querySelector("[data-update-rollback]");
  const input = document.querySelector("[data-update-version]");
  const apply = document.querySelector("[data-update-apply]");
  const previous = document.querySelector("[data-update-previous]");
  const reload = document.querySelector("[data-update-reload]");
  const logPanel = document.querySelector("[data-update-log]");
  const logText = document.querySelector("[data-update-log-text]");
  let latest = "";
  let supported = false;
  let updating = false;
  let operation = "";
  let seenJob = "";
  let pendingBaseline = "";
  let pendingTarget = "";
  let generation = 0;
  let pollRequest = null;
  let logRequest = null;
  let logRefreshPending = false;
  let pollTimer = null;
  let rollbackVersion = "";
  // The version installed now (the page's, then the update status's): an older target is a step back.
  let installed = /^\d+\.\d+\.\d+$/.test(pageVersion) ? pageVersion : "";
  let busy = false;
  const dateLabel = (value, key, label) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "";
    // The server's own writing of the time (<field>_label: the page's language and the zone,
    // as every other date of the page); the pattern below only for an older server.
    if (typeof label === "string" && label) return t(key, { when: label });
    const date = new Date(value * 1000);
    if (!Number.isFinite(date.getTime())) return "";
    // The language's own pattern (_meta.datetime), as the server writes dates in the page.
    const two = (number) => String(number).padStart(2, "0");
    const parts = {
      Y: String(date.getFullYear()), m: two(date.getMonth() + 1), d: two(date.getDate()),
      H: two(date.getHours()), M: two(date.getMinutes()), S: two(date.getSeconds()),
    };
    const pattern = (typeof I18N === "undefined" ? null : I18N._datetime) || "%Y-%m-%d %H:%M:%S";
    const when = String(pattern).replace(/%([YmdHMS%])/g, (whole, code) => (code === "%" ? "%" : parts[code]));
    return t(key, { when });
  };
  // A text with a time in it ("checked {when}") as pieces: the time a <time> by the page's one rule
  // (app.js towTime: "20:31", the full time on focus or a tap) from the server's words for it
  // (<field>_time); a server without them still gets its own written label.
  const stamp = (value, key, label, words) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return [];
    if (window.towTime && words && typeof words === "object") {
      const [before, after = ""] = t(key, { when: "\u0000" }).split("\u0000");
      return [before, window.towTime.element(words, typeof label === "string" ? label : ""), after].filter((piece) => piece !== "");
    }
    const said = dateLabel(value, key, label);
    return said ? [said] : [];
  };
  const stamps = (pairs) => pairs.flatMap((pieces) => (pieces.length ? [" · ", ...pieces] : []));
  const put = (target, pieces) => {
    if (typeof target.replaceChildren === "function") target.replaceChildren(...pieces);
    else target.textContent = pieces.map((piece) => (typeof piece === "string" ? piece : piece.textContent)).join("");
  };
  const render = (data, manual = false) => {
    const available = data.available === true && /^\d+\.\d+\.\d+$/.test(data.latest || "");
    latest = available ? data.latest : "";
    if (command && /^\d+\.\d+\.\d+$/.test(data.latest || "")) {
      command.textContent = command.textContent.replace(/--ref\s+\S+$/, "--ref v" + data.latest);
    }
    controls();
    // A newer version is the footer's badge on every page, Home too; Settings → Updates says more.
    badge.hidden = !available;
    badge.textContent = available ? t("js.releases.badge", { version: data.latest }) : "";
    badge.title = available ? t("js.releases.available", { version: data.latest }) : "";
    positionOverlay();
    if (notes && /^https:\/\/github\.com\/d0j\/tow\/releases(?:\/tag\/v\d+\.\d+\.\d+)?$/.test(data.url || "")) {
      notes.href = data.url;
    }
    if (status) {
      let said = available ? t("js.releases.available", { version: data.latest }) :
        (data.ok ? t(data.comparable === false ? "js.releases.unknown_build" : "js.releases.current") : t("js.releases.unavailable"));
      if (available && !data.ok) said += " · " + t("js.releases.stale");
      let pieces = [said, ...stamps([stamp(data.checked_at, "js.releases.checked", data.checked_at_label, data.checked_at_time)])];
      // U1: automatic checks off and nothing ever checked - the unticked box above says it all; a
      // check asked by hand, a newer version or a past check's result are still said.
      if (data.checks_enabled === false && !manual && !data.latest && !data.checked_at) pieces = [];
      put(status, pieces);
    }
  };
  const refresh = async (manual = false) => {
    if (busy) return;
    busy = true;
    if (check) { check.disabled = true; check.setAttribute("aria-busy", "true"); }
    if (status) status.textContent = t("js.releases.checking");
    try {
      const response = await fetch(manual ? "/updates/check" : "/updates.json", {
        method: manual ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
        signal: AbortSignal.timeout(15000), headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("release check unavailable");
      const data = await response.json();
      render(data, manual);
    } catch {
      if (status) status.textContent = t("js.releases.unavailable");
    } finally {
      busy = false;
      if (check) { check.disabled = false; check.removeAttribute("aria-busy"); }
    }
  };
  check?.addEventListener("click", () => { refresh(true); pollJob(); });
  const controls = () => {
    const disabled = updating || !supported;
    if (install) { install.hidden = !latest || !supported; install.disabled = disabled; }
    if (apply) apply.disabled = disabled;
    if (input) input.disabled = disabled;
    if (previous) previous.disabled = disabled;
  };
  const schedulePoll = () => {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(() => { pollTimer = null; pollJob(); }, 5000);
  };
  const pollJob = async () => {
    if (!progress) return;
    if (pollRequest?.generation === generation) return;
    const request = { generation };
    pollRequest = request;
    try {
      const response = await fetch("/updates/status", { cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error("update status unavailable");
      const job = await response.json();
      if (request.generation !== generation) return;
      // An old terminal result is not evidence that a new POST was accepted.
      if (pendingTarget) {
        if (!job.id || job.id === pendingBaseline || job.target !== pendingTarget) {
          progress.hidden = false;
          progress.textContent = t("js.releases.confirming");
          if (reload) reload.hidden = false;
          schedulePoll();
          return;
        }
        operation = job.id;
        pendingTarget = "";
      }
      // Another tab may start a newer job before this tab observes our terminal result.
      const replaced = Boolean(operation && job.id !== operation);
      seenJob = job.id || "";
      supported = job.supported === true;
      if (rollback) rollback.hidden = !supported;
      if (support) support.textContent = !supported && typeof job.message === "string" ? job.message :
        t(supported ? "js.releases.supported" : "js.releases.unsupported");
      if (unavailable) {
        unavailable.hidden = supported;
        unavailable.textContent = typeof job.message === "string" ? job.message : t("js.releases.unsupported");
      }
      rollbackVersion = job.rollback_version || "";
      if (previous) {
        previous.hidden = !rollbackVersion;
        previous.textContent = rollbackVersion ? t("js.releases.previous", { version: rollbackVersion }) : "";
      }
      updating = job.active === true;
      const installedVersion = typeof job.current === "string" && job.current ? job.current :
        (job.status === "ok" && typeof job.target === "string" ? job.target : "");
      if (/^\d+\.\d+\.\d+$/.test(installedVersion)) installed = installedVersion;
      const needsReload = !updating && Boolean(installedVersion) && pageVersion !== installedVersion;
      const phases = {
        queued: "js.releases.queued", preparing: "js.releases.preparing", stopping: "js.releases.stopping",
        backup: "js.releases.backup", installing: "js.releases.installing", checking: "js.releases.phase_checking",
        rolling_back: "js.releases.rolling_back", ok: "js.releases.ok", rolled_back: "js.releases.rolled_back",
        failed: "js.releases.failed", refused: "js.releases.refused", interrupted: "js.releases.interrupted",
        superseded: "js.releases.superseded",
        recovered: "js.releases.recovered",
      };
      if (Object.prototype.hasOwnProperty.call(phases, job.status)) {
        progress.hidden = false;
        const messageKey = job.status === "ok" && pageVersion && pageVersion === installedVersion ?
          "js.releases.ok_current" : phases[job.status];
        progress.textContent = t(messageKey, { version: job.target || installedVersion });
        if (replaced) progress.textContent = t("js.releases.operation_changed") + " · " + progress.textContent;
        if (typeof job.error_message === "string" && job.error_message) {
          progress.textContent = ["failed", "refused", "interrupted"].includes(job.status) ?
            job.error_message : progress.textContent + " · " + job.error_message;
        }
        if (job.backup_cleanup_pending === true) {
          progress.textContent += " · " + t("js.releases.backup_cleanup_pending");
        }
        // Its times, drawn for each place they show (an element lives in one place only).
        const said = progress.textContent;
        const times = () => stamps([
          stamp(job.started_at, "js.releases.started", job.started_at_label, job.started_at_time),
          stamp(job.finished_at, "js.releases.finished", job.finished_at_label, job.finished_at_time),
        ]);
        put(progress, [said, ...times()]);
        if (history) {
          put(history, [said, ...times()]);
          history.hidden = !job.id;
          if (job.status === "ok" && !updating && !needsReload && !replaced && !job.backup_cleanup_pending) progress.hidden = true;
        }
      }
      if (reload) reload.hidden = !needsReload;
      controls();
      if (updating) { operation = job.id; schedulePoll(); }
      else operation = "";
      refreshLog();
    } catch {
      if (request.generation !== generation) return;
      if (updating) {
        progress.hidden = false;
        progress.textContent = t(pendingTarget ? "js.releases.confirming" : "js.releases.reconnecting");
        if (pendingTarget && reload) reload.hidden = false;
      } else {
        supported = false;
        controls();
        if (support) support.textContent = t("js.releases.status_unavailable");
        if (unavailable) { unavailable.hidden = false; unavailable.textContent = t("js.releases.status_unavailable"); }
      }
      schedulePoll();
    } finally {
      if (pollRequest === request) pollRequest = null;
    }
  };
  const begin = async (version, source = null) => {
    if (updating || !supported) return;
    if (!/^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version || "")) {
      progress.hidden = false; progress.textContent = t("js.releases.invalid_version"); return;
    }
    // Going back to an older version says so: it may not know settings made since.
    const parts = (value) => String(value || "").replace(/^v/, "").split(".").map(Number);
    const target = parts(version), current = parts(installed);
    const order = target.map((part, index) => part - (current[index] ?? 0)).find((difference) => difference !== 0) ?? 0;
    const question = installed && current.length === 3 && order < 0 ?
      t("js.releases.confirm_older", { version: version.replace(/^v/, ""), current: installed }) : t("js.releases.confirm", { version });
    // The page's own dialog (app.js towConfirm), its button the action clicked; going back is
    // asked with Cancel first.
    const ask = { text: question, confirmLabel: source?.textContent?.trim() || "", danger: order < 0 };
    if (!(window.towConfirm ? await window.towConfirm(ask) : window.confirm(question))) return;
    if (updating || !supported) return;
    generation++;
    operation = "";
    pendingBaseline = seenJob;
    pendingTarget = version.replace(/^v/, "");
    if (pollTimer !== null) { window.clearTimeout(pollTimer); pollTimer = null; }
    if (reload) reload.hidden = true;
    if (logText) logText.textContent = t("js.releases.log_pending");
    updating = true; controls();
    progress.hidden = false; progress.textContent = t("js.releases.backup");
    let refused = false;
    try {
      const body = new URLSearchParams({ version });
      const response = await fetch("/updates/install", { method: "POST", body, credentials: "same-origin", signal: AbortSignal.timeout(60000) });
      const job = await response.json();
      if (!response.ok || !job.ok) { refused = true; throw new Error(job.error || t("js.releases.failed")); }
      if (typeof job.id !== "string" || !job.id) throw new Error("missing update identity");
      operation = job.id;
      pendingTarget = "";
      progress.textContent = t("js.releases.queued");
      schedulePoll();
    } catch (error) {
      // A lost POST response does not prove that the background updater did not start.
      if (refused) {
        pendingTarget = "";
        progress.textContent = error.message || t("js.releases.failed");
        updating = false; controls(); return;
      }
      progress.textContent = t("js.releases.confirming");
      await pollJob();
    }
  };
  install?.addEventListener("click", () => { begin(latest, install); });
  apply?.addEventListener("click", () => { begin(input?.value.trim() || "", apply); });
  previous?.addEventListener("click", () => { begin(rollbackVersion, previous); });
  reload?.addEventListener("click", () => { window.location.reload(); });
  const refreshLog = async () => {
    if (!logPanel?.open || !logText || pendingTarget) return;
    if (logRequest?.generation === generation && logRequest.job === seenJob) {
      logRefreshPending = true;
      return;
    }
    const request = { generation, job: seenJob };
    logRequest = request;
    try {
      const response = await fetch("/updates/log", { cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error("log unavailable");
      const data = await response.json();
      if (request.generation === generation && request.job === seenJob) logText.textContent = data.text || t("js.releases.log_empty");
    } catch {
      if (request.generation === generation && request.job === seenJob) logText.textContent = t("js.releases.log_unavailable");
    } finally {
      if (logRequest === request) {
        logRequest = null;
        if (logRefreshPending) { logRefreshPending = false; refreshLog(); }
      }
    }
  };
  logPanel?.addEventListener("toggle", refreshLog);
  refresh();
  pollJob();
  // Long-running dashboards discover releases too, not only after a reload.
  window.setInterval(() => { if (!document.hidden) { refresh(); pollJob(); } }, 60 * 60 * 1000);
})();
