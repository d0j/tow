// The current-problems triangle shares the header/timer poll. Native details is closed
// initially even without scripts; it disappears only after a confirmed empty snapshot.
(() => {
  document.addEventListener("DOMContentLoaded", () => {
    const menu = document.querySelector("[data-attention]");
    const toggle = menu?.querySelector("[data-attention-toggle]");
    const counter = menu?.querySelector("[data-attention-count]");
    const list = menu?.querySelector("[data-attention-list]");
    if (!menu || !toggle || !counter || !list) return;
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
    if (typeof healthPoll === "undefined") return;
    let due = Date.now() + (menu.hidden ? 30000 : 15000);
    let failures = 0;
    const valid = (value) => value && typeof value === "object" && Number.isSafeInteger(value.count) && value.count >= 0
      && typeof value.label === "string" && typeof value.fingerprint === "string"
      && ["", "warn", "bad"].includes(value.tone) && Array.isArray(value.entries)
      && value.count === value.entries.length && value.entries.every((entry) =>
        entry && typeof entry === "object" && typeof entry.id === "string" && typeof entry.text === "string"
        && ["warn", "bad"].includes(entry.severity) && typeof entry.href === "string"
        && entry.href.startsWith("/") && !entry.href.startsWith("//") && !/[\\\x00-\x1f]/.test(entry.href));
    const update = (data) => {
      const value = data?.attention;
      if (!valid(value)) {
        failures += 1;
        due = Date.now() + Math.min(15000 * 2 ** Math.min(failures, 2), 60000);
        return; // Missing or malformed observations never pretend everything recovered.
      }
      failures = 0;
      due = Date.now() + (value.count ? 15000 : 30000);
      const focused = menu.contains(document.activeElement);
      if (menu.dataset.fingerprint !== value.fingerprint) {
        const focusedId = focused ? document.activeElement?.closest?.("[data-issue-id]")?.dataset.issueId : null;
        const rows = value.entries.map((entry) => {
          const row = document.createElement("li");
          row.dataset.issueId = entry.id;
          const link = document.createElement("a");
          link.className = `attention-link ${entry.severity}`;
          link.setAttribute("href", entry.href);
          const words = document.createElement("span");
          words.textContent = entry.text;
          link.append(words);
          const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          arrow.setAttribute("class", "i sm");
          arrow.setAttribute("aria-hidden", "true");
          arrow.setAttribute("focusable", "false");
          const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
          use.setAttribute("href", "#i-next");
          arrow.append(use);
          link.append(arrow);
          row.append(link);
          return row;
        });
        list.replaceChildren(...rows);
        menu.dataset.fingerprint = value.fingerprint;
        if (focusedId && value.count) {
          const retained = rows.find((row) => row.dataset.issueId === focusedId);
          (retained?.firstElementChild || toggle).focus();
        }
      }
      counter.textContent = value.count > 99 ? "99+" : String(value.count);
      toggle.title = value.label;
      toggle.setAttribute("aria-label", value.label);
      menu.classList.toggle("bad", value.tone === "bad");
      menu.classList.toggle("warn", value.tone === "warn");
      if (!value.count) {
        if (focused) document.getElementById("log-open")?.focus();
        menu.open = false;
      }
      menu.hidden = !value.count;
    };
    healthPoll.add({ due: () => due, shown: () => true, receive: update });
  });
})();
