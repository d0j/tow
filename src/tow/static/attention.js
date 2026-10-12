// Home's "Needs attention" folds into a warning button with the number of problems, and that
// button opens it again. The choice is this browser's (localStorage), kept with the fingerprint
// of the problems it was made for: another set of problems (a new one, or a different one) opens
// the banner by itself, so a new problem is never hidden.
//
// Home loads this file in <head>, not deferred (the CSP forbids an inline script): it marks
// <html> before the page is drawn, and the CSS shows the banner or the button from the start, so
// nothing jumps. Without the script both buttons stay hidden and the banner stays open.
(() => {
  const KEY = "tow.attention";
  const root = document.documentElement;
  const fingerprint = document.currentScript?.dataset?.fingerprint || "";

  const read = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "null");
      return saved && typeof saved === "object" ? saved : null;
    } catch {
      return null; // no storage, or a broken record: the banner stays open
    }
  };
  const write = (value) => {
    try {
      if (value) localStorage.setItem(KEY, JSON.stringify(value));
      else localStorage.removeItem(KEY);
    } catch {
      // no storage: the banner folds on this page only
    }
  };

  const saved = read();
  // Folded only for exactly the problems it was folded for.
  const folded = Boolean(fingerprint) && saved?.folded === true && saved.fp === fingerprint;
  if (saved && !folded) write(null); // the problems changed (or are gone): the old choice goes
  root.classList.add("attention-js");
  root.classList.toggle("attention-folded", folded);

  const setFolded = (on) => {
    root.classList.toggle("attention-folded", on);
    write(on ? { folded: true, fp: fingerprint } : null);
  };

  document.addEventListener("DOMContentLoaded", () => {
    const fold = document.querySelector("[data-attention-fold]");
    const unfold = document.querySelector("[data-attention-unfold]");
    if (!fold || !unfold) return;
    // The focus moves to the control that took the place of the one just pressed.
    fold.addEventListener("click", () => {
      setFolded(true);
      unfold.focus();
    });
    unfold.addEventListener("click", () => {
      setFolded(false);
      fold.focus();
    });
  });
})();
