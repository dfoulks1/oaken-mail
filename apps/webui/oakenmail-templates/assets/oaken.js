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

  // Add to the namespace api.js already created; replacing it would drop Oaken.api.
  window.Oaken = Object.assign(window.Oaken || {}, { setTheme, openFloatingCompose });
})();

/* ===========================================================================================
 * Data / render layer — populates the templates from the API (api.js), or from demo fixtures.
 *
 * The HTML pages ship with a static markup fallback inside each [data-oaken-list] /
 * [data-oaken-reader] container. If api.js is present this layer replaces that fallback with
 * live (or fixture) data; if the script or the core is unavailable, the static markup is what the
 * reader sees. That is deliberate progressive enhancement: the published demo looks the same with
 * or without JS, and the app degrades to "static shell" rather than a blank page.
 * =========================================================================================== */
(function () {
  if (!window.Oaken || typeof window.Oaken.api !== "function") return; // api.js not loaded
  const api = window.Oaken.api;

  /* ---- tiny helpers ---- */
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const qs = (sel, el) => (el || document).querySelector(sel);
  const qsa = (sel, el) => Array.from((el || document).querySelectorAll(sel));

  function toast(msg) {
    let host = qs(".ok-toast-host");
    if (!host) { host = document.createElement("div"); host.className = "ok-toast-host"; document.body.appendChild(host); }
    const t = document.createElement("div");
    t.className = "ok-toast";
    t.textContent = msg;
    host.appendChild(t);
    setTimeout(() => t.classList.add("show"), 10);
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, 3200);
  }
  window.Oaken.toast = toast;

  /* ---- mail text is UNTRUSTED: everything from the API is inserted as text, never as markup,
   *      except the one body field the core has already cleaned (reader html). ---- */
  function tag(text, color) {
    const dot = color ? `<span class="ok-label-dot" style="background:${color}"></span>` : "";
    return `<span class="ok-tag">${dot}${esc(text)}</span>`;
  }

  function rowHtml(m) {
    const starOn = m.starred ? " on" : "";
    const starIcon = m.starred ? "bi-star-fill" : "bi-star";
    const unread = m.is_read === false ? " unread" : "";
    const clip = m.has_attachments ? '<i class="bi bi-paperclip text-muted"></i> ' : "";
    const tags = []
      .concat((m.tags || []).map((t) => tag(t)))
      .concat((m.labels || []).map((l) => tag(l, window.Oaken.labelColor(l))))
      .join("");
    // Snippet markers (**word**) from keyword search become <mark>.
    const snippet = esc(m.snippet || "").replace(/\*\*(.+?)\*\*/g, "<mark>$1</mark>");
    return (
      `<li class="ok-msg${unread}" data-href="message.html?id=${encodeURIComponent(m.message_id)}" data-id="${esc(m.message_id)}" tabindex="0">` +
      `<input class="form-check-input m-0" type="checkbox" aria-label="Select">` +
      `<button class="btn btn-sm btn-ghost p-0 star${starOn}" data-action="star" aria-label="Star"><i class="bi ${starIcon}"></i></button>` +
      `<span class="from">${esc(m.from_name || m.from_addr)}</span>` +
      `<span class="date">${clip}${esc(fmtDate(m.date))}</span>` +
      `<span class="line2"><span class="subject">${esc(m.subject)}</span>` +
      `<span class="snippet">— ${snippet}…</span>${tags}</span>` +
      `</li>`
    );
  }

  /* ---- the message list: lazy infinite scroll, sort, and folds ----
   * Rows load PAGE at a time as the list nears its end. A fold groups the rows by the sort key
   * (a day for date, a sender for sender, a subject for subject, unread/read), so the order and
   * the folds always agree. Counts on a fold are of the rows loaded so far. Which folds are
   * closed is remembered per view in this browser. */
  const PAGE = 40;
  const REFRESH_PAGES = 5; // a refresh re-fetches what is loaded, up to this many pages
  const SORTS = [
    ["date_desc", "Newest first"], ["date_asc", "Oldest first"], ["sender_asc", "Sender A–Z"],
    ["sender_desc", "Sender Z–A"], ["subject_asc", "Subject"], ["unread_first", "Unread first"],
  ];
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* private window */ } },
  };
  const view = { key: "", items: [], total: null, exact: true, more: false, loading: false,
    token: 0, sort: "date_desc", folded: new Set(), last: null, counts: {} };
  let observer = null;

  const isSearch = (op) => op === "search" || op === "search_keyword";
  const sortFamily = (sort) => sort.split("_")[0]; // date | sender | subject | unread
  function chosenSort(op) {
    const stored = store.get(isSearch(op) ? "oaken.sort.search" : "oaken.sort");
    const ok = SORTS.some(([v]) => v === stored) || (isSearch(op) && stored === "relevance");
    return ok ? stored : (isSearch(op) ? "relevance" : "date_desc");
  }

  function dayLabel(d) {
    const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diff = Math.round((today - day) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    if (diff > 1 && diff < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric",
      year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
  }
  function fmtDate(iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return String(iso || "");
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
  }
  // The subject with one leading Re:/Fwd:/Fw: removed: the same rule the server sorts by.
  const bareSubject = (s) => String(s || "").replace(/^\s*(re|fwd|fw):/i, "").trim();

  // {key, label} of the fold a row belongs to under this sort, or null for no folds (best match).
  function foldOf(m, sort) {
    if (m.group) return { key: "g:" + m.group, label: m.group }; // the demo fixtures name groups
    if (sort === "date_desc" || sort === "date_asc") {
      const d = new Date(m.date);
      if (!m.date || isNaN(d)) return { key: "d:unknown", label: "No date" };
      return { key: "d:" + d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(), label: dayLabel(d) };
    }
    if (sort === "sender_asc" || sort === "sender_desc") {
      const name = m.from_name || m.from_addr || "(unknown)";
      return { key: "s:" + name.toLowerCase(), label: name };
    }
    if (sort === "subject_asc") {
      const s = bareSubject(m.subject) || "(no subject)";
      return { key: "t:" + s.toLowerCase(), label: s };
    }
    if (sort === "unread_first") {
      return m.is_read === false ? { key: "u:unread", label: "Unread" } : { key: "u:read", label: "Read" };
    }
    return null;
  }

  function foldsKey() { return "oaken.folds:" + view.key + ":" + sortFamily(view.sort); }
  function loadFolds() {
    try { return new Set(JSON.parse(store.get(foldsKey()) || "[]")); } catch (e) { return new Set(); }
  }
  function saveFolds() { store.set(foldsKey(), JSON.stringify(Array.from(view.folded).slice(-300))); }

  function rowsHtml(items) {
    let html = "";
    for (const m of items) {
      const fold = foldOf(m, view.sort);
      if (fold && (!view.last || view.last.key !== fold.key)) {
        const closed = view.folded.has(fold.key);
        html += `<div class="ok-list-group ok-fold${closed ? " closed" : ""}" role="button" tabindex="0" aria-expanded="${closed ? "false" : "true"}" data-fold="${esc(fold.key)}">` +
          `<i class="bi bi-chevron-down ok-fold-icon"></i><span class="ok-fold-label">${esc(fold.label)}</span><span class="ok-fold-count"></span></div>`;
      }
      if (fold) {
        view.last = fold;
        view.counts[fold.key] = (view.counts[fold.key] || 0) + 1;
      }
      let row = rowHtml(m);
      if (fold && view.folded.has(fold.key)) row = row.replace("<li ", `<li hidden data-fold-key="${esc(fold.key)}" `);
      else if (fold) row = row.replace("<li ", `<li data-fold-key="${esc(fold.key)}" `);
      html += row;
    }
    return html;
  }

  function updateFoldCounts(list) {
    qsa(".ok-fold", list).forEach((h) => {
      const n = view.counts[h.dataset.fold] || 0;
      const el = qs(".ok-fold-count", h);
      if (el) el.textContent = n ? String(n) : "";
    });
  }

  function renderStatus() {
    const text = qs("[data-oaken-pager-text]");
    if (!text) return;
    const n = view.items.length;
    if (view.total === null) { text.textContent = ""; return; }
    text.textContent = n ? `${n.toLocaleString()} of ${view.exact ? "" : "about "}${view.total.toLocaleString()}` : "0 messages";
  }

  function setSentinel(list) {
    let s = qs(".ok-sentinel", list);
    if (!s) { s = document.createElement("li"); s.className = "ok-sentinel"; list.appendChild(s); }
    s.textContent = view.loading ? "Loading…" : "";
    s.hidden = !view.more && !view.loading;
    return s;
  }

  function inputFor(container, op) {
    const input = {};
    const url = new URL(window.location.href);
    if (isSearch(op)) input.query = url.searchParams.get("q") || "";
    const label = url.searchParams.get("label");
    if (label) input.label = label;
    // Folders (ADR 0008): ?location=X filters to a filed folder; ?folder=inbox|filed|discarded
    // selects a disposition. The sidebar links set these.
    const location = url.searchParams.get("location");
    if (location) { input.location = location; input.disposition = "any"; }
    const folder = url.searchParams.get("folder");
    if (folder && ["inbox", "filed", "discarded"].includes(folder)) input.disposition = folder;
    input.sort = view.sort;
    return { input, key: op + "|" + view.sort + "|" + url.search };
  }

  function sortMenu(op) {
    const sel = qs("[data-oaken-sort]");
    if (!sel) return;
    const options = (isSearch(op) ? [["relevance", "Best match"]] : []).concat(SORTS);
    if (sel.dataset.for !== (isSearch(op) ? "search" : "list")) {
      sel.innerHTML = options.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("");
      sel.dataset.for = isSearch(op) ? "search" : "list";
    }
    sel.value = view.sort;
  }

  async function fetchPage(op, input, offset, limit) {
    const { output } = await api(op, { ...input, limit, offset });
    return output || {};
  }

  // (Re)load the list: a fresh view starts at the top; a refresh of the same view re-fetches the
  // rows already loaded (so a read-state change shows) and keeps the scroll position.
  async function loadList(container, keepChecked) {
    const list = qs(".ok-list", container) || container;
    const op = container.dataset.oakenList || "list_messages"; // "list_messages" | "search"
    view.sort = chosenSort(op);
    const { input, key } = inputFor(container, op);
    const sameView = key === view.key;
    const want = sameView ? Math.min(Math.max(view.items.length, PAGE), PAGE * REFRESH_PAGES) : PAGE;
    const token = ++view.token;
    sortMenu(op);
    container.classList.add("ok-loading");
    try {
      let items = [], out = {};
      while (items.length < want) {
        out = await fetchPage(op, input, items.length, Math.min(PAGE, want - items.length));
        if (token !== view.token) return; // a newer load replaced this one
        const got = out.items || [];
        items = items.concat(got);
        if (!got.length || !(out.truncated || (typeof out.total === "number" && items.length < out.total))) break;
      }
      const scroll = sameView ? container.scrollTop : 0;
      view.key = key;
      view.items = items;
      view.total = typeof out.total === "number" ? out.total : null;
      view.exact = out.total_exact !== false;
      view.more = !!(out.truncated || (view.total !== null && items.length < view.total));
      view.last = null; view.counts = {};
      view.folded = loadFolds();
      itemsById.clear();
      items.forEach((m) => itemsById.set(m.message_id, m));
      list.innerHTML = items.length ? rowsHtml(items) : '<li class="ok-empty-row text-muted p-4 text-center">No messages.</li>';
      setSentinel(list);
      updateFoldCounts(list);
      (keepChecked || []).forEach((id) => {
        const box = qs(`.ok-msg[data-id="${CSS.escape(id)}"] input[type=checkbox]`, list);
        if (box) box.checked = true;
      });
      if (out.degraded && out.degraded_reason) toast(out.degraded_reason.charAt(0).toUpperCase() + out.degraded_reason.slice(1) + ".");
      else if (out.degraded) toast("Search ran keyword-only (meaning search unavailable).");
      (out.warnings || []).forEach((w) => toast(w));
      renderStatus();
      bindRows(container);
      container.scrollTop = scroll;
      watchEnd(container, list, op, input);
    } catch (e) {
      toast(e.message || "Could not load messages.");
    } finally {
      container.classList.remove("ok-loading");
    }
  }

  // The next page, when the end of the list comes into view (a little early, so it is ready).
  async function loadMore(container, list, op, input, token) {
    if (view.loading || !view.more || token !== view.token) return;
    view.loading = true;
    setSentinel(list);
    try {
      const out = await fetchPage(op, input, view.items.length, PAGE);
      if (token !== view.token) return;
      // Only rows not already shown: a core that ignores offset would repeat the first page.
      const got = (out.items || []).filter((m) => !itemsById.has(m.message_id));
      got.forEach((m) => itemsById.set(m.message_id, m));
      view.items = view.items.concat(got);
      if (typeof out.total === "number") { view.total = out.total; view.exact = out.total_exact !== false; }
      view.more = !!got.length && !!(out.truncated || (view.total !== null && view.items.length < view.total));
      const html = rowsHtml(got);
      const sentinel = setSentinel(list);
      const before = list.children.length;
      sentinel.insertAdjacentHTML("beforebegin", html);
      bindRows(container, Array.from(list.children).slice(before - 1, -1));
      updateFoldCounts(list);
      renderStatus();
    } catch (e) {
      view.more = false;
      toast(e.message || "Could not load more messages.");
    } finally {
      view.loading = false;
      const s = setSentinel(list);
      if (view.more && observer) { observer.unobserve(s); observer.observe(s); } // still in view? go on
    }
  }

  function watchEnd(container, list, op, input) {
    if (observer) observer.disconnect();
    if (!("IntersectionObserver" in window)) return;
    const token = view.token;
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMore(container, list, op, input, token);
    }, { root: container, rootMargin: "0px 0px 400px 0px" });
    const s = qs(".ok-sentinel", list);
    if (s) observer.observe(s);
  }

  // Fold and unfold a group (click, or Enter/Space on the focused header).
  function toggleFold(h) {
    const key = h.dataset.fold;
    const closed = !view.folded.has(key);
    if (closed) view.folded.add(key); else view.folded.delete(key);
    h.classList.toggle("closed", closed);
    h.setAttribute("aria-expanded", closed ? "false" : "true");
    qsa(`.ok-msg[data-fold-key="${CSS.escape(key)}"]`, h.parentElement).forEach((r) => { r.hidden = closed; });
    saveFolds();
  }
  document.addEventListener("click", (e) => {
    const h = e.target.closest(".ok-fold");
    if (h) toggleFold(h);
  });
  document.addEventListener("keydown", (e) => {
    const h = e.target.closest && e.target.closest(".ok-fold");
    if (h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleFold(h); }
  });
  document.addEventListener("change", (e) => {
    const sel = e.target.closest && e.target.closest("[data-oaken-sort]");
    const container = qs("[data-oaken-list]");
    if (!sel || !container) return;
    const op = container.dataset.oakenList || "list_messages";
    store.set(isSearch(op) ? "oaken.sort.search" : "oaken.sort", sel.value);
    view.key = ""; // a different order is a fresh view: back to the top
    loadList(container);
  });

  /* Clicking a row: split layout loads the reader inline; otherwise follow the link. Mirrors the
   * presentation handler but also fetches the body when a reader pane is present. */
  function bindRows(container, rows) {
    (rows || qsa(".ok-msg[data-id]", container)).forEach((row) => {
      if (!row.matches || !row.matches(".ok-msg[data-id]") || row.dataset.bound) return;
      row.dataset.bound = "1";
      row.addEventListener("click", (e) => {
        if (e.target.closest("input,button,a")) return;
        const reader = qs("[data-oaken-reader]");
        const main = row.closest(".ok-main");
        if (reader && main && main.dataset.layout === "split" && window.innerWidth >= 992) {
          e.preventDefault();
          qsa(".ok-msg.selected", container).forEach((r) => r.classList.remove("selected"));
          row.classList.add("selected");
          loadReader(reader, row.dataset.id, { markPresented: true });
        }
      });
    });
  }

  const THREAD_MAX = 100;

  // The other messages of the conversation, oldest first. A failure here must not stop the opened
  // message from showing, so it answers an empty list.
  async function fetchThread(key) {
    if (!key) return [];
    try {
      const { output } = await api("list_messages", {
        thread_key: key, sort: "date_asc", disposition: "any", limit: THREAD_MAX,
      });
      return (output && output.items) || [];
    } catch (_) {
      return [];
    }
  }

  async function loadReader(container, messageId, opts) {
    container.classList.add("ok-loading");
    try {
      const { output } = await api("get_message", { message_id: messageId, include_text: true });
      const b = output || {};
      const thread = await fetchThread(b.thread_key);
      container.innerHTML = readerHtml(b, thread);
      // The reader starts hidden (d-none) beside the "Select a message" placeholder: swap them.
      container.classList.remove("d-none");
      qs(".ok-empty", container.parentElement)?.classList.add("d-none");
      qs(".ok-thread-msg.current", container)?.scrollIntoView({ block: "start" });
      // Opening a message is a user action: core marks it read after its configured delay (and
      // only if auto-marking is on). message_presented is a mutation, so it must be confirmed or
      // core answers with a proposal and arms nothing. The row and the badges change when core
      // announces MessageReadChanged (live updates below), not before.
      if (opts && opts.markPresented && b.message_id) {
        api("message_presented", { message_id: b.message_id }, { confirm: true }).catch(() => {});
      }
    } catch (e) {
      toast(e.message || "Could not open message.");
    } finally {
      container.classList.remove("ok-loading");
    }
  }

  // Expanding a collapsed card of the thread fetches its text the first time. Mail is untrusted:
  // the body goes in as text, never markup.
  async function openThreadCard(card) {
    if (!card || card.dataset.loaded || !card.dataset.id) return;
    card.dataset.loaded = "1";
    const body = qs(".ok-msg-body", card);
    if (body) body.textContent = "Loading…";
    try {
      const { output } = await api("get_message", { message_id: card.dataset.id, include_text: true });
      if (body) body.textContent = (output && output.text) || "(no text)";
      api("message_presented", { message_id: card.dataset.id }, { confirm: true }).catch(() => {});
    } catch (e) {
      delete card.dataset.loaded;
      if (body) body.textContent = "Could not load this message.";
      toast((e && e.message) || "Could not open message.");
    }
  }

  // "Alex <alex@x.org>, bob@y.org" from get_message's to_addrs (the demo fixture sends a string).
  function recipients(b) {
    if (typeof b.to === "string") return b.to;
    return (b.to_addrs || []).map((a) => (a.name ? `${a.name} <${a.address}>` : a.address)).join(", ");
  }

  const initialsOf = (name) =>
    (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

  // One card of the thread. The opened message arrives with its text; the others show a line of
  // snippet and fetch the rest when expanded.
  function cardHtml(m, { current, text, atts, to }) {
    const who = m.from_name || m.from_addr || "";
    return (
      `<article class="ok-thread-msg${current ? " current" : " collapsed"}" data-id="${esc(m.message_id || "")}"${current ? ' data-loaded="1"' : ""}>` +
      `<header data-action="toggle-thread"><span class="ok-avatar">${esc(initialsOf(who))}</span>` +
      `<div class="flex-grow-1"><div><span class="fw-semibold">${esc(m.from_name || "")}</span> <span class="small text-muted">&lt;${esc(m.from_addr || "")}&gt;</span>` +
      (m.is_read === false && !current ? ` <span class="badge text-bg-primary ms-1">Unread</span>` : "") + `</div>` +
      (to ? `<div class="small text-muted">to ${esc(to)}</div>` : "") +
      (current ? "" : `<div class="small text-muted ok-thread-snippet">${esc(m.snippet || "")}</div>`) + `</div>` +
      `<span class="small text-muted">${esc(current ? m.date || "" : fmtDate(m.date))}</span></header>` +
      `<div class="ok-msg-body" style="white-space:pre-wrap">${current ? (text ? esc(text) : "(no text)") : ""}${atts ? `<div class="mt-3">${atts}</div>` : ""}</div>` +
      `</article>`
    );
  }

  function readerHtml(b, thread) {
    const tags = []
      .concat((b.tags || []).map((t) => tag(t)))
      .concat((b.labels || []).map((l) => tag(l, window.Oaken.labelColor(l))))
      .join("");
    const atts = (b.attachments || [])
      .map((a) => `<a class="ok-attach" href="#"><i class="bi ${esc(a.icon || "bi-file-earmark")} fs-5"></i><span>${esc(a.filename)}<br><span class="small text-muted">${esc(a.size || "")}</span></span></a>`)
      .join("");
    // b.text is the cleaned plain text core produced. Mail is untrusted: it is escaped and shown
    // as text, never as markup.
    const current = cardHtml(b, { current: true, text: b.text, atts, to: recipients(b) });
    const others = (thread || []).filter((m) => m.message_id);
    const all = others.length > 1 || (others.length === 1 && others[0].message_id !== b.message_id);
    const cards = all
      ? others.map((m) => (m.message_id === b.message_id ? current : cardHtml(m, { current: false }))).join("")
      : current;
    // A thread that did not list the opened message (filtered, or past the cap) still shows it.
    const body = all && !others.some((m) => m.message_id === b.message_id) ? cards + current : cards;
    return (
      `<div class="ok-reader-inner">` +
      `<h1>${esc(b.subject)}</h1>` +
      (all ? `<div class="small text-muted mb-2">${others.length} messages in this conversation</div>` : "") +
      `<div class="d-flex gap-2 mb-4 flex-wrap">${tags}</div>` + body +
      (others.length >= THREAD_MAX ? `<div class="small text-muted">Showing the first ${THREAD_MAX} messages.</div>` : "") +
      `</div>`
    );
  }

  /* ---- mutating toolbar actions (mark read, star, sync, refresh) ----
   * All go through the registry's confirm gate. A human-driven single action is pre-confirmed
   * (confirm:true); a bulk action over the gate's threshold would come back as a proposal, which
   * we surface rather than auto-confirming. */
  function selectedIds(container) {
    return qsa(".ok-msg", container)
      .filter((r) => qs("input[type=checkbox]", r)?.checked)
      .map((r) => r.dataset.id)
      .filter(Boolean);
  }

  async function mutate(name, input, okMsg) {
    try {
      const res = await api(name, input, { confirm: true });
      if (res.proposal) { toast(`${res.proposal.total_changes} messages — confirm needed.`); return null; }
      if (okMsg) toast(okMsg);
      return res.output;
    } catch (e) {
      toast(e.message || "Action failed.");
      return null;
    }
  }

  function wireActions() {
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-oaken-action]");
      if (!t) return;
      const a = t.dataset.oakenAction;
      const list = qs("[data-oaken-list]");
      if (a === "refresh") { list && loadList(list); }
      else if (a === "sync") { mutate("sync_now", {}, "Sync started."); }
      else if (a === "mark-read" || a === "mark-unread") {
        const ids = list ? selectedIds(list) : [];
        if (!ids.length) { toast("Select messages first."); return; }
        const read = a === "mark-read";
        mutate(read ? "mark_read" : "mark_unread", { ids }).then((out) => {
          if (!out) return; // refused or failed: mutate() already said so; leave the rows alone
          const done = new Set([...(out.changed || []), ...(out.unchanged || [])]);
          ids.filter((id) => done.has(id)).forEach((id) => {
            qs(`.ok-msg[data-id="${CSS.escape(id)}"]`, list)?.classList.toggle("unread", !read);
          });
          renderSidebar();
          const n = (out.changed || []).length;
          toast(`${n} marked ${read ? "read" : "unread"}` +
            (out.local_only && out.local_only.length ? `; ${out.local_only.length} kept here only (not written back)` : "") +
            ((out.missing || []).length ? `; ${out.missing.length} not found` : "") + ".");
        });
      }
      else if (a === "send") {
        e.preventDefault();
        // There is no send operation in the registry yet (send is a later phase, and is never
        // exposed over MCP). Be honest rather than pretend.
        toast("Sending isn't wired up yet — this is a preview.");
      }
    });
  }

  /* ---- sidebar: folders (organization) and labels (search), from the backend ----
   * Folders are the default dispositions plus each distinct location (ADR 0008). Labels are the
   * search tags. Both come from list_labels, so nothing is assembled from mail in the client. */
  const FOLDER_ICON = { inbox: "bi-inbox", filed: "bi-folder2", discarded: "bi-trash3" };
  // A badge is the UNREAD count (like a mail client); nothing shows when there is none.
  function badge(n) { return n ? `<span class="count unread">${n}</span>` : ""; }
  // Put the unread count on one of the static top links (Inbox, Discarded).
  function topBadge(folder, n) {
    const a = qs(`.ok-nav > a[href="index.html?folder=${folder}"]`);
    if (!a) return;
    qs(".count", a)?.remove();
    a.insertAdjacentHTML("beforeend", badge(n));
  }

  // The structure core keeps (folders nested under Inbox, labels), cached for the menus below.
  let mailbox = { folders: [], labels: [] };
  const itemsById = new Map(); // message_id -> the row's data, for the label menu

  // A label's colour is a UI preference, kept in this browser; core stores no colours.
  const labelColors = {
    all() { try { return JSON.parse(localStorage.getItem("oaken.labelColors") || "{}"); } catch { return {}; } },
    set(name, color) {
      try { const m = this.all(); m[name] = color; localStorage.setItem("oaken.labelColors", JSON.stringify(m)); } catch { /* private mode: the default colour is used */ }
    },
  };
  const baseLabelColor = window.Oaken.labelColor;
  window.Oaken.labelColor = (name) => labelColors.all()[name] || baseLabelColor(name);

  const indent = (f) => "  ".repeat(f.depth);

  async function renderSidebar() {
    const folders = qs("[data-oaken-folders]");
    const labelsBox = qs("[data-oaken-labels]");
    if (!folders && !labelsBox) return;
    try {
      const { output } = await api("list_mailbox", {});
      mailbox = { folders: (output && output.folders) || [], labels: (output && output.labels) || [] };
      topBadge("inbox", (output && output.inbox && output.inbox.unread) || 0);
      topBadge("discarded", (output && output.discarded && output.discarded.unread) || 0);
      if (folders) {
        folders.innerHTML = mailbox.folders.length
          ? mailbox.folders.map((f) =>
              `<a href="index.html?location=${encodeURIComponent(f.path)}" title="Inbox/${esc(f.path)}" ` +
              `style="padding-left:${0.75 + f.depth * 1.1}rem"><i class="bi ${FOLDER_ICON.filed}"></i>` +
              `${esc(f.name)}${badge(f.tree.unread)}</a>`
            ).join("")
          : '<div class="small text-muted px-3 py-1">No folders yet.</div>';
      }
      if (labelsBox) {
        labelsBox.innerHTML = mailbox.labels.length
          ? mailbox.labels.map((l) =>
              `<a href="index.html?label=${encodeURIComponent(l.name)}">` +
              `<span class="ok-label-dot" style="background:${window.Oaken.labelColor(l.name)}"></span>` +
              `${esc(l.name)}${badge(l.count.unread)}</a>`
            ).join("")
          : '<div class="small text-muted px-3 py-1">No tags yet.</div>';
      }
    } catch (e) {
      // A sidebar failure should not break the page; leave the static fallback in place.
      toast(e.message || "Could not load folders.");
    }
  }

  /* ---- creating and using folders and labels ---- */
  const modalOf = (el) => (el && window.bootstrap ? window.bootstrap.Modal.getOrCreateInstance(el) : null);
  const folderOptions = (withInbox) =>
    (withInbox ? [{ path: "", label: "Inbox" }] : []).concat(
      mailbox.folders.map((f) => ({ path: f.path, label: indent(f) + f.name }))
    );

  function wireMailboxUi() {
    // New folder / new label forms.
    document.addEventListener("submit", async (e) => {
      const form = e.target.closest("form[data-oaken-new]");
      if (!form) return;
      e.preventDefault();
      const kind = form.dataset.oakenNew;
      const name = (form.elements.name.value || "").trim();
      if (!name) return;
      let out;
      if (kind === "folder") {
        const parent = form.elements.parent ? form.elements.parent.value : "";
        out = await mutate("create_folder", { path: parent ? `${parent}/${name}` : name });
      } else {
        out = await mutate("create_label", { name });
        if (out && form.elements.color) labelColors.set(out.name, form.elements.color.value);
      }
      if (!out) return; // mutate() already said why
      modalOf(form.closest(".modal"))?.hide();
      form.reset();
      toast(kind === "folder"
        ? (out.created.length ? `Folder “${out.path}” created.` : `Folder “${out.path}” already exists.`)
        : (out.created ? `Label “${out.name}” created.` : `Label “${out.name}” already exists.`));
      renderSidebar();
    });

    // "Inside" choices follow the folders that exist.
    document.addEventListener("show.bs.modal", (e) => {
      const sel = qs("[data-oaken-parent-select]", e.target);
      if (sel) {
        sel.innerHTML = folderOptions(true).map((o) =>
          `<option value="${esc(o.path)}">${o.path ? "" : "← "}${esc(o.label)}${o.path ? "" : " (top level)"}</option>`).join("");
      }
      const box = qs("[data-oaken-move-list]", e.target);
      if (box) {
        const list = qs("[data-oaken-list]");
        const ids = list ? selectedIds(list) : [];
        if (!ids.length) { e.preventDefault(); toast("Select messages first."); return; }
        const filter = qs("[data-oaken-move-filter]", e.target);
        const draw = () => {
          const q = (filter.value || "").toLowerCase();
          box.innerHTML = folderOptions(true)
            .filter((o) => !q || o.label.toLowerCase().includes(q))
            .map((o) => `<button type="button" class="list-group-item list-group-item-action" data-path="${esc(o.path)}">${esc(o.label)}</button>`)
            .join("") || '<div class="small text-muted p-2">No matching folder.</div>';
        };
        filter.value = ""; filter.oninput = draw; draw();
        box.onclick = async (ev) => {
          const b = ev.target.closest("[data-path]");
          if (!b) return;
          const out = await mutate("move_to_folder", { ids, path: b.dataset.path || null });
          if (!out) return;
          modalOf(e.target)?.hide();
          toast(`${out.moved.length} moved to ${out.path ? "Inbox/" + out.path : "Inbox"}.`);
          loadList(list); renderSidebar();
        };
      }
    });

    // Label menu: a checkbox per label for the selected messages, plus "New label…".
    document.addEventListener("show.bs.dropdown", (e) => {
      const menu = qs("[data-oaken-label-menu]", e.target);
      if (!menu) return;
      const list = qs("[data-oaken-list]");
      const ids = list ? selectedIds(list) : [];
      if (!ids.length) { e.preventDefault(); toast("Select messages first."); return; }
      const has = (name) => ids.every((id) => (itemsById.get(id)?.labels || []).includes(name));
      menu.innerHTML = (mailbox.labels.length ? mailbox.labels.map((l) =>
        `<li><label class="dropdown-item"><input class="form-check-input me-2" type="checkbox" data-label="${esc(l.name)}"${has(l.name) ? " checked" : ""}>` +
        `<span class="ok-label-dot me-2" style="background:${window.Oaken.labelColor(l.name)}"></span>${esc(l.name)}</label></li>`).join("")
        : '<li><span class="dropdown-item-text small text-muted">No labels yet.</span></li>') +
        '<li><hr class="dropdown-divider"></li>' +
        '<li><button type="button" class="dropdown-item" data-bs-toggle="modal" data-bs-target="#newLabelModal">New label…</button></li>';
      menu.onchange = async (ev) => {
        const box = ev.target.closest("input[data-label]");
        if (!box) return;
        const name = box.dataset.label;
        const out = await mutate("set_labels", box.checked ? { ids, add: [name] } : { ids, remove: [name] });
        if (!out) { box.checked = !box.checked; return; }
        ids.forEach((id) => {
          const m = itemsById.get(id);
          if (!m) return;
          const set = new Set(m.labels || []);
          box.checked ? set.add(name) : set.delete(name);
          m.labels = [...set];
        });
        toast(`${box.checked ? "Added" : "Removed"} “${name}” on ${out.changed.length || ids.length}.`);
        loadList(list, ids); renderSidebar();
      };
    });
  }

  /* ---- live updates: recompute the badges on every read-state change ----
   * Core publishes MessageReadChanged for every way read state changes (this UI, the CLI, MCP, a
   * Gmail sync that saw the mail read elsewhere) and MessageStored for new mail. We refresh the
   * sidebar counts (debounced) and flip the matching row, so nothing depends on which client acted.
   * EventSource reconnects by itself; on a gap or after a reconnect we just re-fetch. */
  let _sidebarTimer = null;
  function refreshSidebarSoon() {
    clearTimeout(_sidebarTimer);
    _sidebarTimer = setTimeout(renderSidebar, 250);
  }
  function watchEvents() {
    if (typeof EventSource === "undefined" || window.Oaken.isDemo) return;
    const es = new EventSource("/v1/events");
    es.addEventListener("MessageReadChanged", (e) => {
      try {
        const d = JSON.parse(e.data);
        const row = qs(`.ok-msg[data-id="${CSS.escape(d.message_id)}"]`);
        row && row.classList.toggle("unread", d.read === false);
      } catch { /* a malformed frame is ignored; the counts refresh below still runs */ }
      refreshSidebarSoon();
    });
    es.addEventListener("MessageStored", refreshSidebarSoon);
    es.addEventListener("gap", refreshSidebarSoon);
    es.onopen = refreshSidebarSoon; // also covers a reconnect after core restarted
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshSidebarSoon(); });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireActions();
    wireMailboxUi();
    renderSidebar();
    watchEvents();
    // Reflect the current ?q into the search box and any results heading.
    const q = new URL(window.location.href).searchParams.get("q") || "";
    qsa("[data-oaken-query]").forEach((el) => {
      if ("value" in el && el.tagName === "INPUT") el.value = q;
      else el.textContent = q ? `Results for “${q}”` : "All messages";
    });
    const list = qs("[data-oaken-list]");
    if (list) loadList(list);
    const reader = qs("[data-oaken-reader]");
    if (reader && reader.dataset.oakenReader) {
      // A standalone message page: id comes from the query string.
      const id = new URL(window.location.href).searchParams.get("id") || reader.dataset.oakenReader;
      loadReader(reader, id, { markPresented: true });
    }
    // A collapsed card of the thread loads its text the first time it is opened.
    document.addEventListener("click", (e) => {
      const h = e.target.closest(".ok-thread-msg > header");
      if (h) openThreadCard(h.parentElement);
    });
    // Source modal (View source) pulls the raw headers on demand.
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-oaken-source]");
      if (!b) return;
      const id = b.dataset.oakenSource;
      api("get_raw", { message_id: id }).then(({ output }) => {
        const pre = qs("#sourceModal pre");
        if (pre) pre.textContent = (output && output.headers) || "(unavailable)";
      }).catch((err) => toast(err.message || "Could not load source."));
    });
  });
})();

