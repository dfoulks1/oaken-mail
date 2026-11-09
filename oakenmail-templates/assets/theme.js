/* Palette and mode, applied from <head> so the page never flashes the wrong colours.
 * A palette is two colours (see oaken.css); a mode is light, dark or system. */
(function () {
  const root = document.documentElement;
  const PALETTES = ["oak", "birch", "maple", "water", "sky", "stone", "sakura"], MODES = ["light", "dark", "system"];
  const get = (k, d) => { try { return localStorage.getItem("oaken." + k) || d; } catch { return d; } };
  const put = (k, v) => { try { localStorage.setItem("oaken." + k, v); } catch { /* private window: it just will not stick */ } };
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const old = get("theme", ""); // the single choice earlier versions kept: oak-light, oak-dark, birch, system
  if (old && !get("mode", "")) { put("mode", old === "oak-dark" ? "dark" : old === "system" ? "system" : "light"); }
  const palette = () => (PALETTES.includes(get("palette", "")) ? get("palette", "") : "oak");
  const mode = () => (MODES.includes(get("mode", "")) ? get("mode", "") : "system");

  function apply() {
    const m = mode(), resolved = m === "system" ? (mq.matches ? "dark" : "light") : m;
    root.setAttribute("data-oaken-palette", palette());
    root.setAttribute("data-oaken-mode", resolved);
    root.setAttribute("data-bs-theme", resolved);
    document.querySelectorAll("[data-palette-set]").forEach((el) => el.classList.toggle("active", el.dataset.paletteSet === palette()));
    document.querySelectorAll("[data-mode-set]").forEach((el) => {
      el.classList.toggle("active", el.dataset.modeSet === m);
      if (el.type === "radio") el.checked = el.dataset.modeSet === m;
    });
    const icon = { light: "bi-sun", dark: "bi-moon-stars", system: "bi-circle-half" }[m];
    document.querySelectorAll("[data-theme-icon]").forEach((el) => (el.className = "bi " + icon));
  }
  window.OakenTheme = { setPalette: (p) => { put("palette", p); apply(); }, setMode: (m) => { put("mode", m); apply(); } };
  apply();
  mq.addEventListener("change", () => mode() === "system" && apply());
  document.addEventListener("DOMContentLoaded", apply);
  document.addEventListener("click", (e) => {
    const p = e.target.closest("[data-palette-set]"), m = e.target.closest("[data-mode-set]");
    if (p) window.OakenTheme.setPalette(p.dataset.paletteSet);
    if (m) window.OakenTheme.setMode(m.dataset.modeSet);
  });
})();
