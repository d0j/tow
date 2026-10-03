// Optional discovery: pages and health keep working when GitHub is unavailable.
(() => {
  const badge = document.querySelector("[data-release-badge]");
  if (!badge) return;
  const overlay = document.querySelector(".app-version");
  let framePending = false;
  const positionOverlay = () => {
    if (!overlay || framePending) return;
    framePending = true;
    window.requestAnimationFrame(() => {
      framePending = false;
      const box = overlay.getBoundingClientRect();
      const obstructing = [...document.querySelectorAll("button,input,select,textarea,a,summary,td,.flash")].some((control) => {
        if (overlay.contains(control) || !control.getClientRects().length) return false;
        if (control.checkVisibility && !control.checkVisibility({ visibilityProperty: true })) return false;
        // Closed accordions may still expose child rectangles in older browsers.
        for (let parent = control.parentElement; parent; parent = parent.parentElement) {
          if (parent.tagName === "DETAILS" && !parent.open && !parent.querySelector("summary")?.contains(control)) return false;
        }
        const rect = control.getBoundingClientRect();
        return Math.min(box.right, rect.right) - Math.max(box.left, rect.left) > .5 &&
          Math.min(box.bottom, rect.bottom) - Math.max(box.top, rect.top) > .5;
      });
      overlay.classList.toggle("is-obstructing", obstructing);
    });
  };
  if (overlay) {
    window.addEventListener("scroll", positionOverlay, { passive: true, capture: true });
    window.addEventListener("resize", positionOverlay);
    document.addEventListener("toggle", positionOverlay, true);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(positionOverlay).observe(document.body);
    const main = document.querySelector("main");
    if (main && typeof MutationObserver !== "undefined") {
      new MutationObserver(positionOverlay).observe(main, {
        childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "open", "class"],
      });
    }
    positionOverlay();
  }
  const status = document.querySelector("[data-release-status]");
  const check = document.querySelector("[data-release-check]");
  const notes = document.querySelector("[data-release-notes]");
  const command = document.querySelector("[data-release-command]");
  const install = document.querySelector("[data-release-install]");
  const progress = document.querySelector("[data-update-progress]");
  const support = document.querySelector("[data-update-support]");
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
  let pollTimer = null;
  let rollbackVersion = "";
  let busy = false;
  const dateLabel = (value, key) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "";
    const date = new Date(value * 1000);
    return Number.isFinite(date.getTime()) ? t(key, { when: date.toLocaleString(document.documentElement.lang) }) : "";
  };
  const render = (data) => {
    const available = data.available === true && /^\d+\.\d+\.\d+$/.test(data.latest || "");
    latest = available ? data.latest : "";
    if (command && /^\d+\.\d+\.\d+$/.test(data.latest || "")) {
      command.textContent = command.textContent.replace(/--ref\s+\S+$/, "--ref v" + data.latest);
    }
    controls();
    badge.hidden = !available;
    badge.textContent = available ? t("js.releases.badge", { version: data.latest }) : "";
    badge.title = available ? t("js.releases.available", { version: data.latest }) : "";
    positionOverlay();
    if (notes && /^https:\/\/github\.com\/d0j\/tow\/releases(?:\/tag\/v\d+\.\d+\.\d+)?$/.test(data.url || "")) {
      notes.href = data.url;
    }
    if (status) {
      status.textContent = available ? t("js.releases.available", { version: data.latest }) :
        (data.ok ? t(data.comparable === false ? "js.releases.unknown_build" : "js.releases.current") : t("js.releases.unavailable"));
      if (available && !data.ok) status.textContent += " · " + t("js.releases.stale");
      const checked = dateLabel(data.checked_at, "js.releases.checked");
      if (checked) status.textContent += " · " + checked;
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
      render(data);
    } catch {
      if (status) status.textContent = t("js.releases.unavailable");
    } finally {
      busy = false;
      if (check) { check.disabled = false; check.removeAttribute("aria-busy"); }
    }
  };
  check?.addEventListener("click", () => { refresh(true); });
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
      rollbackVersion = job.rollback_version || "";
      if (previous) {
        previous.hidden = !rollbackVersion;
        previous.textContent = rollbackVersion ? t("js.releases.previous", { version: rollbackVersion }) : "";
      }
      updating = job.active === true;
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
        progress.textContent = t(phases[job.status], { version: job.target || "" });
        if (replaced) progress.textContent = t("js.releases.operation_changed") + " · " + progress.textContent;
        if (typeof job.error_message === "string" && job.error_message) {
          progress.textContent = ["failed", "refused", "interrupted"].includes(job.status) ?
            job.error_message : progress.textContent + " · " + job.error_message;
        }
        for (const label of [dateLabel(job.started_at, "js.releases.started"), dateLabel(job.finished_at, "js.releases.finished")]) {
          if (label) progress.textContent += " · " + label;
        }
      }
      if (reload) reload.hidden = job.status !== "ok";
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
      }
      schedulePoll();
    } finally {
      if (pollRequest === request) pollRequest = null;
    }
  };
  const begin = async (version) => {
    if (updating || !supported) return;
    if (!/^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version || "")) {
      progress.hidden = false; progress.textContent = t("js.releases.invalid_version"); return;
    }
    if (!window.confirm(t("js.releases.confirm", { version }))) return;
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
  install?.addEventListener("click", () => { begin(latest); });
  apply?.addEventListener("click", () => { begin(input?.value.trim() || ""); });
  previous?.addEventListener("click", () => { begin(rollbackVersion); });
  reload?.addEventListener("click", () => { window.location.reload(); });
  const refreshLog = async () => {
    if (!logPanel?.open || !logText || pendingTarget ||
        (logRequest?.generation === generation && logRequest.job === seenJob)) return;
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
      if (logRequest === request) logRequest = null;
    }
  };
  logPanel?.addEventListener("toggle", refreshLog);
  refresh();
  pollJob();
  // Long-running dashboards discover releases too, not only after a reload.
  window.setInterval(() => { if (!document.hidden) refresh(); }, 60 * 60 * 1000);
})();
