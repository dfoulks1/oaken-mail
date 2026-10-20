/* OakenMail UI behaviors — plain JS, needs Bootstrap 5.3 bundle.
 * Preferences are kept in localStorage; swap for server-side prefs later. */
(function () {
  const root = document.documentElement;
  const store = {
    get: (k, d) => { try { return localStorage.getItem("oaken." + k) || d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("oaken." + k, v); } catch {} },
  };

  /* ---- Theme: oak-light | oak-dark | birch | system ---- */
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  function applyTheme(choice) {
    const resolved = choice === "system" ? (mq.matches ? "oak-dark" : "oak-light") : choice;
    root.setAttribute("data-oaken-theme", resolved);
    root.setAttribute("data-bs-theme", resolved === "oak-dark" ? "dark" : "light");
    root.setAttribute("data-oaken-theme-choice", choice);
    document.querySelectorAll("[data-theme-set]").forEach((el) => {
      el.classList.toggle("active", el.dataset.themeSet === choice);
      if (el.type === "radio") el.checked = el.dataset.themeSet === choice;
    });
    const icon = { "oak-light": "bi-sun", "oak-dark": "bi-moon-stars", birch: "bi-tree", system: "bi-circle-half" }[choice];
    document.querySelectorAll("[data-theme-icon]").forEach((el) => (el.className = "bi " + icon));
  }
  function setTheme(choice) { store.set("theme", choice); applyTheme(choice); }
  applyTheme(store.get("theme", "system"));
  mq.addEventListener("change", () => store.get("theme", "system") === "system" && applyTheme("system"));

  /* ---- Density + layout ---- */
  function applyDensity(d) {
    root.setAttribute("data-oaken-density", d);
    document.querySelectorAll("[data-density-set]").forEach((el) => {
      el.classList.toggle("active", el.dataset.densitySet === d);
      if (el.type === "radio") el.checked = el.dataset.densitySet === d;
    });
  }
  function applyLayout(l) {
    document.querySelectorAll(".ok-main[data-layout]").forEach((m) => m.setAttribute("data-layout", l));
    document.querySelectorAll("[data-layout-set]").forEach((el) => {
      el.classList.toggle("active", el.dataset.layoutSet === l);
      if (el.type === "radio") el.checked = el.dataset.layoutSet === l;
    });
  }
  applyDensity(store.get("density", "comfortable"));

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(store.get("theme", "system"));
    applyDensity(store.get("density", "comfortable"));
    applyLayout(store.get("layout", "split"));

    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-theme-set],[data-density-set],[data-layout-set],[data-action]");
      if (!t) return;
      if (t.dataset.themeSet) setTheme(t.dataset.themeSet);
      if (t.dataset.densitySet) { store.set("density", t.dataset.densitySet); applyDensity(t.dataset.densitySet); }
      if (t.dataset.layoutSet) { store.set("layout", t.dataset.layoutSet); applyLayout(t.dataset.layoutSet); }
      const a = t.dataset.action;
      if (a === "toggle-layout") { const n = store.get("layout", "split") === "split" ? "list" : "split"; store.set("layout", n); applyLayout(n); }
      if (a === "toggle-sidebar") document.querySelector(".ok-sidebar")?.classList.toggle("show");
      if (a === "compose") { e.preventDefault(); openFloatingCompose(t.dataset); }
      if (a === "close-float") t.closest(".ok-float")?.remove();
      if (a === "minimize-float") { e.stopPropagation(); t.closest(".ok-float")?.classList.toggle("minimized"); }
      if (a === "expand-float") { e.stopPropagation(); expandFloat(t.closest(".ok-float")); }
      if (a === "popout-float") { e.stopPropagation(); popoutFloat(t.closest(".ok-float")); }
      if (a === "popout") { e.preventDefault(); window.open(t.getAttribute("href") || t.dataset.href, "_blank", "popup,width=900,height=760"); }
      if (a === "star") { e.stopPropagation(); e.preventDefault(); t.classList.toggle("on"); t.querySelector(".bi")?.classList.toggle("bi-star-fill"); t.querySelector(".bi")?.classList.toggle("bi-star"); }
      if (a === "toggle-thread") t.closest(".ok-thread-msg")?.classList.toggle("collapsed");
      if (a === "select-all") document.querySelectorAll(".ok-msg input[type=checkbox]").forEach((c) => (c.checked = t.checked));
    });

    /* Clicking a message: split layout shows it in reader; list layout navigates */
    document.querySelectorAll(".ok-msg[data-href]").forEach((row) => {
      row.addEventListener("click", (e) => {
        if (e.target.closest("input,button,a")) return;
        const main = row.closest(".ok-main");
        if (main && main.dataset.layout === "split" && window.innerWidth >= 992) {
          document.querySelectorAll(".ok-msg.selected").forEach((r) => r.classList.remove("selected"));
          row.classList.add("selected"); row.classList.remove("unread");
          document.querySelector(".ok-reader .ok-empty")?.classList.add("d-none");
          document.querySelector(".ok-reader .ok-reader-content")?.classList.remove("d-none");
        } else {
          window.location.href = row.dataset.href;
        }
      });
    });

    /* Recipient chips: Enter or comma turns input into a chip */
    document.querySelectorAll("[data-chips]").forEach(initChips);

    /* Keyboard shortcuts (c = compose, / = search, ? = shortcuts) */
    document.addEventListener("keydown", (e) => {
      if (e.target.closest("input,textarea,[contenteditable=true],select")) return;
      if (e.key === "c") { e.preventDefault(); openFloatingCompose({}); }
      if (e.key === "/") { e.preventDefault(); document.querySelector(".ok-search input")?.focus(); }
      if (e.key === "?") { const m = document.getElementById("shortcutsModal"); m && bootstrap.Modal.getOrCreateInstance(m).show(); }
    });

    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
  });

  function initChips(wrap) {
    const input = wrap.querySelector("input");
    input.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
        e.preventDefault();
        const chip = document.createElement("span");
        chip.className = "ok-chip";
        chip.innerHTML = '<span></span><button type="button" aria-label="Remove">&times;</button>';
        chip.firstChild.textContent = input.value.trim().replace(/,$/, "");
        chip.querySelector("button").onclick = () => chip.remove();
        wrap.insertBefore(chip, input);
        input.value = "";
      } else if (e.key === "Backspace" && !input.value) {
        input.previousElementSibling?.classList.contains("ok-chip") && input.previousElementSibling.remove();
      }
    });
    wrap.querySelectorAll(".ok-chip button").forEach((b) => (b.onclick = () => b.parentElement.remove()));
  }

  /* ---- Floating compose windows (bottom-right dock) ---- */
  function openFloatingCompose(opts) {
    const tpl = document.getElementById("tpl-float-compose");
    if (!tpl) { window.location.href = "compose.html"; return; }
    let dock = document.querySelector(".ok-dock");
    if (!dock) { dock = document.createElement("div"); dock.className = "ok-dock"; document.body.appendChild(dock); }
    if (dock.children.length >= 3) dock.firstElementChild.remove();
    const node = tpl.content.firstElementChild.cloneNode(true);
    if (opts.subject) node.querySelector("[name=subject]").value = opts.subject;
    if (opts.title) node.querySelector(".ok-float-title").textContent = opts.title;
    node.querySelector("header").addEventListener("click", () => node.classList.toggle("minimized"));
    dock.appendChild(node);
    node.querySelectorAll("[data-chips]").forEach(initChips);
    node.querySelector("[data-chips] input")?.focus();
  }
  function expandFloat(f) {
    const modalEl = document.getElementById("composeModal");
    if (!modalEl) { window.location.href = "compose.html"; return; }
    const subj = f.querySelector("[name=subject]").value;
    modalEl.querySelector("[name=subject]").value = subj;
    f.remove();
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
  function popoutFloat(f) {
    const subj = encodeURIComponent(f.querySelector("[name=subject]").value || "");
    window.open("compose.html?popout=1&subject=" + subj, "_blank", "popup,width=860,height=760");
    f.remove();
  }

  window.Oaken = { setTheme, openFloatingCompose };
})();
