/* OakenMail UI behaviors — plain JS, needs Bootstrap 5.3 bundle.
 * Preferences are kept in localStorage; swap for server-side prefs later. */
(function () {
  const root = document.documentElement;
  const store = {
    get: (k, d) => { try { return localStorage.getItem("oaken." + k) || d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("oaken." + k, v); } catch {} },
  };

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
    applyDensity(store.get("density", "comfortable"));
    applyLayout(store.get("layout", "split"));

    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-density-set],[data-layout-set],[data-action]");
      if (!t) return;
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
      if (a === "star") { e.stopPropagation(); e.preventDefault(); const on = !t.classList.contains("on"); const row = t.closest(".ok-msg");
        if (row && window.Oaken && window.Oaken.api) window.Oaken.api("set_starred", { ids: [row.dataset.id], starred: on }, { confirm: true }).catch((err) => { toast((err && err.message) || "Could not star."); t.classList.toggle("on"); t.querySelector(".bi")?.classList.toggle("bi-star-fill"); t.querySelector(".bi")?.classList.toggle("bi-star"); });
        t.classList.toggle("on"); t.querySelector(".bi")?.classList.toggle("bi-star-fill"); t.querySelector(".bi")?.classList.toggle("bi-star"); }
      if (a === "toggle-thread") t.closest(".ok-thread-msg")?.classList.toggle("collapsed");
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
    window.Oaken.fillSenders?.(document); // defined by the second IIFE below

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
    window.Oaken.fillSenders?.(node);
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
  window.Oaken = Object.assign(window.Oaken || {}, { openFloatingCompose });
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

  function toast(msg, kind) {
    kind = kind || (/(could not|couldn.t|cannot|can.t|failed|refused|error|not valid|not found|select messages first|nothing changed)/i.test(msg) ? "error" : "");
    let host = qs(".ok-toast-host");
    if (!host) { host = document.createElement("div"); host.className = "ok-toast-host"; document.body.appendChild(host); }
    const t = document.createElement("div");
    t.className = "ok-toast" + (kind ? " " + kind : "");
    t.setAttribute("role", kind === "error" ? "alert" : "status");
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

  // What each stage did to a message, from the labels its plugins wrote: icon, name, and how many results.
  // A plugin either asks a model ("ai") or follows fixed rules ("deterministic"); "" when that cannot be told.
  const NATURE = { ai: ["bi-robot", "Asks a model"], deterministic: ["bi-diagram-2", "Fixed rules, no model"] };
  const natureIcon = (n) => NATURE[n] ? `<i class="bi ${NATURE[n][0]} text-muted me-1" title="${NATURE[n][1]}" aria-label="${NATURE[n][1]}"></i>` : "";
  window.Oaken = window.Oaken || {};
  window.Oaken.natureIcon = natureIcon;
  const STAGE_BADGES = [["triage", "bi-bandaid", "Triage"], ["enrich", "bi-stars", "Enrich"], ["act", "bi-clipboard-check", "Act"]];
  function stageBadges(m) {
    return STAGE_BADGES.map(([k, icon, name]) => {
      const n = ((m.stages || {})[k] || []).length;
      return n ? `<span class="ok-stg" title="${name}: ${n} result${n === 1 ? "" : "s"}"><i class="bi ${icon}"></i>${n}</span>` : "";
    }).join("");
  }
  // The reading pane shows the same icons as the list. Pick one to load that stage's results below it.
  const STAGE_CACHE = {}; // message_id -> the message the panel last drew, so a pick needs no fetch
  // One vertical pill per message, drawn beside its own card and shown only while that card is expanded.
  function stagePillHtml(id, m = {}) {
    // A pip marks a stage that has already run: it has stored results.
    const ran = (k) => ((m.stages || {})[k] || []).length > 0 || (k === "enrich" && (m.enrichments || []).length > 0);
    return `<aside class="ok-stage-pill" data-stage-panel data-id="${esc(id)}" aria-label="Stage results for this message">` +
      STAGE_BADGES.map(([k, icon, name]) => `<button type="button" class="ok-stg-icon${ran(k) ? " ran" : ""}" data-stage-pick="${k}" title="${name}${ran(k) ? ": has run" : ""}" aria-pressed="false"><i class="bi ${icon}"></i></button>`).join("") + `</aside>`;
  }

  // The open stage's results, inside the card it belongs to. Run now sends the message back through the stage.
  // How long a step took, for what it cost: 850 ms, 12.4 s, 2 min 3 s.
  const took = (ms) => ms == null ? "" : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;

  function stageResultsHtml(m, open, pick) {
    const st = m.stages || {};
    const r = st[open] || [];
    const name = (STAGE_BADGES.find(([k]) => k === open) || [])[2] || open;
    const nice = (t) => { const w = String(t).replace(/[_-]+/g, " "); return w.charAt(0).toUpperCase() + w.slice(1); };
    // Stored enrichment data draws as a block per enricher: a bold field name, then bullets (a list stays a list).
    const blocks = open === "enrich" ? (m.enrichments || []) : [];
    // Several enrichers: a dropdown beside the title picks which one this pane shows (the first by default).
    const chosen = blocks.find((b) => b.name === pick) || blocks[0];
    const picker = blocks.length > 1
      ? `<select class="form-select form-select-sm d-inline-block w-auto ms-1 py-0" data-enrich-pick aria-label="Which enrichment">${blocks.map((b) => `<option value="${esc(b.name)}"${b === chosen ? " selected" : ""}>${esc(nice(b.name))}</option>`).join("")}</select>`
      : chosen ? " " + esc(nice(chosen.name)) : "";
    const drawn = chosen ? `<div class="fw-semibold small mt-2 mb-1">Enrich:${picker}${chosen.duration_ms != null ? ` <span class="fw-normal text-muted">· took ${took(chosen.duration_ms)}</span>` : ""}</div>` + chosen.fields.map((f) =>
      `<div class="fw-semibold small">${esc(nice(f.name))}</div><ul class="small mb-2 ps-3">${f.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`).join("") : "";
    const labeled = r.map((x) => `<div class="small"><span class="text-muted">${esc(x.key)}</span> ${esc(x.value)}</div>`).join("");
    const rows = drawn || labeled ? drawn + labeled : `<div class="small text-muted">No ${esc(name.toLowerCase())} results yet.</div>`;
    const folder = open === "triage" && m.location ? `<div class="small mb-1"><i class="bi bi-folder2 me-1"></i>Filed in <strong>${esc(m.location.split(".").join(" › "))}</strong>${m.location_by ? ` <span class="text-muted">set by ${esc(m.location_by)}</span>` : ""}</div>` : "";
    // shortcut: core's item_rerun takes enrich and act only, and has no priority; triage re-run and priority wait on core
    const rerun = open === "enrich" ? `<button class="btn btn-sm btn-ghost" data-stage-rerun="enrich" title="Send this message back through the enrichers now"><i class="bi bi-arrow-repeat"></i> Run now</button>` : "";
    return `<div class="d-flex align-items-start gap-2"><div class="flex-grow-1">${drawn ? "" : `<div class="fw-semibold small mb-1">${esc(name)}</div>`}${folder}${rows}</div>` +
      (rerun ? `<div class="d-flex flex-column gap-1">${rerun}</div>` : "") + `</div>`;
  }

  function rowHtml(m) {
    const starOn = m.starred ? " on" : "";
    const starIcon = m.starred ? "bi-star-fill" : "bi-star";
    const unread = m.is_read === false ? " unread" : "";
    const clip = m.has_attachments ? '<i class="bi bi-paperclip text-muted"></i> ' : "";
    // What's new spans every folder, so each row says where it is filed, then its labels.
    const where = m.location && new URL(location.href).searchParams.get("view") === "new" ? `<span class="ok-tag"><i class="bi bi-folder2"></i>${esc(m.location.split(".").join(" › "))}</span>` : "";
    const tags = [where]
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
      `<span class="snippet">— ${snippet}…</span>${tags}${stageBadges(m)}</span>` +
      `</li>`
    );
  }

  /* ---- the message list: lazy infinite scroll, sort, and folds ----
   * Rows load PAGE at a time as the list nears its end. A fold groups the rows by the sort key
   * (a day for date, a sender for sender, a subject for subject, unread/read), so the order and
   * the folds always agree. Counts on a fold are of the rows loaded so far. Which folds are
   * closed is remembered per view in this browser. */
  const PAGE = 40;
  const AUTO_PAGES = 3; // pages that load on their own while nobody scrolls (folded groups keep the end in view); after that, scroll or press Load more
  const REFRESH_PAGES = 5; // a refresh re-fetches what is loaded, up to this many pages
  const SORTS = [
    ["date_desc", "Newest first"], ["date_asc", "Oldest first"], ["sender_asc", "Sender A–Z"],
    ["sender_desc", "Sender Z–A"], ["subject_asc", "Subject"], ["unread_first", "Unread first"],
  ];
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* private window */ } },
  };
  const view = { key: "", items: [], total: null, exact: true, more: false, loading: false, auto: 0,
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
  function fullDate(iso) { // the open message: day, date and time in this machine's zone, not the header's raw ISO text
    const d = new Date(iso);
    return !iso || isNaN(d) ? String(iso || "") : d.toLocaleString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
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

  // Select-all ticks what is loaded; if more exists, offer to load and tick the rest (Gmail's "select all N").
  function selectLoaded(on) {
    view.selTest = null;
    qsa(".ok-msg input[type=checkbox]").forEach((c) => (c.checked = on));
    const text = qs("[data-oaken-pager-text]");
    if (!on || !view.more || !view.next || view.total === null || !text) { renderStatus(); return; }
    text.innerHTML = `${view.items.length.toLocaleString()} selected. <a href="#" data-select-everything>Select all ${view.total.toLocaleString()}</a>`;
  }

  // Keep the header box and the count in step with the row boxes (a row ticked or cleared, rows loaded, a refresh).
  function syncSelection() {
    const boxes = qsa(".ok-msg input[type=checkbox]");
    const n = boxes.filter((c) => c.checked).length;
    const head = qs("[data-action=select-all]");
    if (head) { head.checked = n > 0 && n === boxes.length; head.indeterminate = n > 0 && n < boxes.length; }
    const text = qs("[data-oaken-pager-text]");
    if (!text) return;
    if (!n) renderStatus();
    else if (head && head.checked && view.more && view.next && view.total !== null) selectLoaded(true);
    else text.textContent = `${n.toLocaleString()} selected`;
  }

  // These sit here, not with the first wrapper's click handler: they need view, qsa and the selection helpers above.
  document.addEventListener("click", async (e) => {
    const head = e.target.closest("[data-action=select-all]");
    if (head) { selectLoaded(head.checked); return; }
    const all = e.target.closest("[data-select-everything]");
    if (!all) return;
    e.preventDefault();
    all.textContent = "Loading every message…";
    while (view.more && view.next) { const n = view.items.length; await view.next(); if (view.items.length === n) break; } // shortcut: pages the rest in; a very large view loads slowly
    const test = view.selTest;
    qsa(".ok-msg").forEach((row) => { const c = qs("input[type=checkbox]", row); if (c) c.checked = test ? test(itemsById.get(row.dataset.id) || {}) : true; });
    syncSelection();
    const text = qs("[data-oaken-pager-text]");
    if (text) text.textContent = test ? `${qsa(".ok-msg input:checked").length.toLocaleString()} of ${view.items.length.toLocaleString()} selected` : `All ${view.items.length.toLocaleString()} selected`;
  });
  document.addEventListener("change", (e) => { if (e.target.matches && e.target.matches(".ok-msg input[type=checkbox]")) syncSelection(); });

  // Shift-click a row's box: every row between it and the last box you clicked takes the same state.
  let lastBox = null;
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".ok-msg input[type=checkbox]");
    if (!b) return;
    if (e.shiftKey && lastBox && lastBox !== b && lastBox.isConnected) {
      const boxes = qsa(".ok-msg input[type=checkbox]"), [i, k] = [boxes.indexOf(lastBox), boxes.indexOf(b)].sort((x, y) => x - y);
      boxes.slice(i, k + 1).forEach((c) => (c.checked = b.checked));
      syncSelection();
    }
    lastBox = b;
  });

  // Tick the loaded rows that pass `test`; if more rows exist unloaded, offer to load them all and apply the same test.
  function selectWhere(test) {
    view.selTest = test;
    qsa(".ok-msg").forEach((row) => { const c = qs("input[type=checkbox]", row); if (c) c.checked = test(itemsById.get(row.dataset.id) || {}); });
    syncSelection();
    const text = qs("[data-oaken-pager-text]"), n = qsa(".ok-msg input:checked").length;
    if (text && n && view.more && view.next && view.total !== null) text.innerHTML = `${n.toLocaleString()} selected. <a href="#" data-select-everything>Load all ${view.total.toLocaleString()} and select all matching</a>`;
  }

  // The caret beside the header box ticks the loaded rows that fit a test, replacing the selection.
  const SELECT_TESTS = {
    all: () => true, none: () => false,
    unread: (m) => m.is_read === false, read: (m) => m.is_read !== false,
    starred: (m) => !!m.starred, unstarred: (m) => !m.starred,
    attachments: (m) => !!m.has_attachments,
    sharp: (m) => { const s = ((m.stages || {}).enrich || []).find((x) => x.key === "sentiment.score"); return !!s && Number(s.value) <= 1; },
  };
  // "By label" lists the labels on the loaded rows, with how many carry each, built each time the menu opens.
  document.addEventListener("show.bs.dropdown", (e) => {
    const menu = qs("[data-oaken-select-menu]", e.target);
    if (!menu) return;
    const counts = new Map();
    qsa(".ok-msg").forEach((row) => (itemsById.get(row.dataset.id)?.labels || []).forEach((l) => counts.set(l, (counts.get(l) || 0) + 1)));
    qsa("[data-select-label-item]", menu).forEach((li) => li.remove());
    if (!counts.size) return;
    menu.insertAdjacentHTML("beforeend", '<li data-select-label-item><hr class="dropdown-divider"></li><li data-select-label-item><h6 class="dropdown-header">By label</h6></li>' +
      [...counts].sort((a, b) => a[0].localeCompare(b[0])).map(([l, n]) =>
        `<li data-select-label-item><button class="dropdown-item d-flex justify-content-between gap-3" data-select-label="${esc(l)}"><span><span class="ok-label-dot me-2" style="background:${window.Oaken.labelColor(l)}"></span>${esc(l)}</span><span class="text-muted">${n}</span></button></li>`).join(""));
  });
  document.addEventListener("click", (e) => {
    const lb = e.target.closest("[data-select-label]");
    if (!lb) return;
    const name = lb.dataset.selectLabel;
    selectWhere((m) => (m.labels || []).includes(name));
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-select]");
    const test = b && SELECT_TESTS[b.dataset.select];
    if (!test) return;
    if (b.dataset.select === "all" || b.dataset.select === "none") { selectLoaded(b.dataset.select === "all"); syncSelection(); return; }
    selectWhere(test);
  });

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
    s.textContent = view.loading ? "Loading…" : (view.more ? "Load more" : "");
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
    const v = url.searchParams.get("view");
    if (v) input.view = v;
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

  /* The search box offers the kinds of search the server lists (a magnifier for key:value search; a plugin may add more, such as an AI
   * sparkle). One kind: the box alone. Two or more: a small menu picks which one the box submits to. */
  async function wireSearchModes() {
    const forms = qsa(".ok-search");
    if (!forms.length) return;
    let modes = [];
    try { modes = ((await api("list_search_modes", {})).output || {}).modes || []; } catch (_) { return; }
    if (!modes.length) return;
    forms.forEach((form) => {
      const input = qs("input", form), icon = qs(":scope > .bi", form);
      const apply = (m) => { form.action = m.action; input.placeholder = m.placeholder; if (icon) icon.className = `bi ${m.icon}`; };
      apply(modes[0]);
      if (modes.length < 2) return;
      const menu = document.createElement("select");
      menu.className = "ok-search-mode";
      menu.setAttribute("aria-label", "Kind of search");
      modes.forEach((m) => menu.add(new Option(m.label, m.id)));
      menu.addEventListener("change", () => apply(modes.find((m) => m.id === menu.value)));
      form.append(menu);
      input.style.paddingRight = "9rem";
    });
  }

  async function fetchPage(op, input, offset, limit) {
    const { output } = await api(op, { ...input, limit, offset });
    return output || {};
  }

  // (Re)load the list: a fresh view starts at the top; a refresh of the same view re-fetches the
  // rows already loaded (so a read-state change shows) and keeps the scroll position.
  async function loadList(container, keepChecked) {
    const empty = qs('[data-oaken-action="empty-discarded"]');
    if (empty) empty.hidden = new URL(location.href).searchParams.get("view") !== "discarded";
    const list = qs(".ok-list", container) || container;
    const op = container.dataset.oakenList || "list_messages"; // "list_messages" | "search"
    view.sort = chosenSort(op);
    const { input, key } = inputFor(container, op);
    const sameView = key === view.key;
    const want = sameView ? Math.min(Math.max(view.items.length, PAGE), PAGE * REFRESH_PAGES) : PAGE;
    const token = ++view.token;
    view.auto = 0;
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
      syncSelection();
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
      const fresh = Array.from(list.children).slice(before - 1, -1);
      bindRows(container, fresh);
      if (qs("[data-action=select-all]")?.checked) fresh.forEach((r) => { const b = qs("input[type=checkbox]", r); if (b) b.checked = true; }); // select-all covers rows that arrive later
      updateFoldCounts(list);
      syncSelection();
    } catch (e) {
      view.more = false;
      toast(e.message || "Could not load more messages.");
    } finally {
      view.loading = false;
      const s = setSentinel(list);
      if (view.more && observer && view.auto < AUTO_PAGES) { observer.unobserve(s); observer.observe(s); } // still in view? go on, a few pages at most
    }
  }

  function watchEnd(container, list, op, input) {
    if (observer) observer.disconnect();
    if (!("IntersectionObserver" in window)) return;
    const token = view.token;
    view.next = () => loadMore(container, list, op, input, token);
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && view.auto < AUTO_PAGES) { view.auto++; loadMore(container, list, op, input, token); }
    }, { root: container, rootMargin: "0px 0px 400px 0px" });
    if (!container._okScroll) { // a real scroll lets it load again
      container._okScroll = true;
      container.addEventListener("scroll", () => { if (view.auto >= AUTO_PAGES) { view.auto = 0; const e = qs(".ok-sentinel", list); if (e && observer) { observer.unobserve(e); observer.observe(e); } } }, { passive: true });
    }
    const s = qs(".ok-sentinel", list);
    if (s) observer.observe(s);
  }

  document.addEventListener("click", (e) => { if (e.target.closest(".ok-sentinel") && view.more && !view.loading && view.next) { view.auto = 0; view.next(); } });

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
        } else if (row.dataset.href) {
          window.location.href = row.dataset.href; // list layout (search results, narrow windows): open the message page
        }
      });
    });
  }

  // The conversation as core's get_thread returns it: reading order, each with parent_id and
  // depth. A failure here must not stop the opened message from showing, so it answers [].
  async function fetchThread(messageId) {
    try {
      const { output } = await api("get_thread", { message_id: messageId });
      return { items: (output && output.items) || [], cut: !!(output && output.truncated), total: output && output.total };
    } catch (_) {
      return { items: [], cut: false, total: 0 };
    }
  }

  async function loadReader(container, messageId, opts) {
    container.classList.add("ok-loading");
    try {
      const { output } = await api("get_message", { message_id: messageId, include_text: true });
      const b = output || {};
      const thread = await fetchThread(b.message_id);
      container.innerHTML = readerHtml(b, thread);
      container.dataset.id = b.message_id;
      threadProgress(container);
      if (b.html) { const cur = qs(".ok-thread-msg.current .ok-msg-body", container); cur && showBody(cur, { text: b.text, html: b.html }); }
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

  // A message body. Mail is untrusted: text goes in as text; HTML goes in a frame with no scripts, no forms,
  // no navigation, and a policy that loads nothing remote (remote images are how senders track opens).
  const HTML_POLICY = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`;
  function showBody(el, { text, html }) {
    const keep = qs(":scope > .mt-3", el); // the attachment links ride in the same box
    if (!html) { el.style.whiteSpace = "pre-wrap"; el.textContent = text || "(no text)"; keep && el.append(keep); return; }
    el.style.whiteSpace = "normal";
    const f = document.createElement("iframe");
    f.setAttribute("sandbox", "allow-same-origin"); // same-origin only so we can read its height; scripts stay off
    f.setAttribute("referrerpolicy", "no-referrer");
    f.style.cssText = "width:100%;border:0;min-height:12rem;background:#fff";
    f.srcdoc = HTML_POLICY + html;
    f.onload = () => { f.style.height = (f.contentDocument?.documentElement.scrollHeight || 400) + "px"; };
    el.replaceChildren(f, ...(keep ? [keep] : []));
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
      if (body) showBody(body, output || {});
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

  const MAX_INDENT = 6; // deeper replies stop indenting so a long chain stays readable
  const initialsOf = (name) =>
    (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

  // One card of the thread. The opened message arrives with its text; the others show a line of
  // snippet and fetch the rest when expanded.
  function cardHtml(m, { current, text, atts, to, replyTo }) {
    const who = m.from_name || m.from_addr || "";
    const depth = Math.min(Number(m.depth) || 0, MAX_INDENT);
    return (
      `<article class="ok-thread-msg${current ? " current" : " collapsed"}${depth ? " nested" : ""}" data-id="${esc(m.message_id || "")}"${current ? ' data-loaded="1"' : ""} style="--ok-depth:${depth}">` +
      `<header data-action="toggle-thread"><span class="ok-avatar">${esc(initialsOf(who))}</span>` +
      `<div class="flex-grow-1 ok-thread-who"><div><span class="fw-semibold">${esc(m.from_name || "")}</span> <span class="small text-muted">&lt;${esc(m.from_addr || "")}&gt;</span>` +
      (m.is_read === false && !current ? ` <span class="badge text-bg-primary ms-1">Unread</span>` : "") + `</div>` +
      (to ? `<div class="small text-muted">to ${esc(to)}</div>` : "") +
      (replyTo ? `<div class="small text-muted">in reply to ${esc(replyTo)}</div>` : "") +
      (current ? "" : `<div class="small text-muted ok-thread-snippet">${esc(m.snippet || "")}</div>`) + `</div>` +
      `<span class="small text-muted ok-thread-date">${esc(current ? fullDate(m.date) : fmtDate(m.date))}</span></header>` +
      `<div class="ok-stage-results" data-stage-results hidden></div>` +
      `<div class="ok-msg-body" style="white-space:pre-wrap">${current ? (text ? esc(text) : "(no text)") : ""}${atts ? `<div class="mt-3">${atts}</div>` : ""}</div>` +
      stagePillHtml(m.message_id || "", m) +
      `</article>`
    );
  }

  // The whole-thread bar: the same three marks as a message's pill, in a row. Only enrich works on a thread today.
  // The tone of each message as a face, in thread order, from the sentiment enricher's labels. A sharp or hostile one (0-1) is marked; a click opens it.
  function heatStripHtml(nodes) {
    const pick = (m, k) => ((m.stages || {}).enrich || []).find((x) => x.key === k)?.value;
    const cells = nodes.map((m) => ({ m, score: pick(m, "sentiment.score"), face: pick(m, "sentiment.face") })).filter((c) => c.face && c.score !== undefined);
    if (!cells.length) return "";
    return `<div class="ok-heat-strip mt-2" aria-label="Tone of each message">` + cells.map(({ m, score, face }) =>
      `<button type="button" class="ok-heat${score <= 1 ? " ok-heat-hot" : ""}" data-heat="${esc(m.message_id)}" title="${esc(nameOfNode(m))}, tone ${esc(score)} of 5">${esc(face)}</button>`).join("") + `</div>`;
  }
  const nameOfNode = (m) => (m && (m.from_name || m.from_addr)) || "";

  const THREAD_NODES = {}; // thread bar id -> the thread's messages as drawn (names, depth, order), so the analysis pane needs no second look
  function threadBarHtml(id, nodes = []) {
    THREAD_NODES[id] = nodes;
    const icon = (k) => STAGE_BADGES.find(([x]) => x === k)[1];
    return `<div class="ok-thread-bar mb-3" data-thread-bar data-id="${esc(id)}"><aside class="ok-stage-pill ok-stage-pill-row" aria-label="The whole thread">` +
      `<button type="button" class="ok-stg-icon" disabled title="Triage: not available for a whole thread yet"><i class="bi ${icon("triage")}"></i></button>` +
      `<button type="button" class="ok-stg-icon" data-thread-enrich title="Show what is stored for each message in this thread, and enrich the rest"><i class="bi ${icon("enrich")}"></i></button>` +
      `<button type="button" class="ok-stg-icon" disabled title="Act: not available for a whole thread yet"><i class="bi ${icon("act")}"></i></button></aside>` +
      `<span class="small text-muted ms-2" data-thread-status></span><div class="ok-stage-results mx-0 mt-2" data-thread-results hidden></div>${heatStripHtml(nodes)}</div>`;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // On open, say how far the thread is: what is enriched is stored, so a reload shows it too.
  async function threadProgress(root) {
    const bar = qs("[data-thread-bar]", root), status = bar && qs("[data-thread-status]", bar);
    if (!status) return;
    try {
      const { done, total, enriched } = (await api("thread_progress", { message_id: bar.dataset.id })).output;
      if (!bar.dataset.busy && done) status.textContent = `${done} of ${total} enriched`;
      // The card's own data has no enrichments (only the full message does), so the pips come from here.
      for (const id of enriched) qsa(`[data-stage-panel][data-id="${CSS.escape(id)}"] [data-stage-pick="enrich"]`, root).forEach((b) => b.classList.add("ran"));
    } catch (err) { /* the bar works without it */ }
  }
  // One question with a check (yes) and an x (no). Resolves true, false, or null when it is dismissed.
  function askYesNo(text) {
    return new Promise((resolve) => {
      const el = document.createElement("div");
      el.className = "modal fade"; el.tabIndex = -1;
      el.innerHTML = `<div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content"><div class="modal-body text-center">` +
        `<p class="mb-3">${esc(text)}</p>` +
        `<button type="button" class="btn btn-oaken me-2" data-pick="yes" aria-label="Yes"><i class="bi bi-check-lg"></i></button>` +
        `<button type="button" class="btn btn-outline-oaken" data-pick="no" aria-label="No"><i class="bi bi-x-lg"></i></button></div></div></div>`;
      let pick = null;
      el.addEventListener("click", (e) => { const b = e.target.closest("[data-pick]"); if (b) { pick = b.dataset.pick === "yes"; window.bootstrap.Modal.getInstance(el).hide(); } });
      el.addEventListener("hidden.bs.modal", () => { el.remove(); resolve(pick); });
      document.body.appendChild(el);
      window.bootstrap.Modal.getOrCreateInstance(el).show();
    });
  }
  // Part of the thread already has a summary: re-enrich those too? "redo" (yes), "missing" (no: only what lacks one) or null (dismissed).
  async function askEnrich(done) {
    const yes = await askYesNo(`Do you want to re-enrich ${done} message${done === 1 ? "" : "s"}?`);
    return yes === null ? null : (yes ? "redo" : "missing");
  }

  const niceName = (t) => { const w = String(t).replace(/[_-]+/g, " "); return w.charAt(0).toUpperCase() + w.slice(1); };
  function threadSummaryHtml(out) {
    const b = out.blocks && out.blocks[0];
    return `<div class="fw-semibold small mb-1">Thread summary${b && b.duration_ms != null ? ` <span class="fw-normal text-muted">· took ${took(b.duration_ms)}</span>` : ""}</div>` +
      (b ? b.fields.map((f) => `<div class="fw-semibold small">${esc(niceName(f.name))}</div><ul class="small mb-2 ps-3">${f.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`).join("") : '<div class="small text-muted">Nothing came back.</div>');
  }
  // The rollup of a thread is written by a worker from the stored summaries. It is a job: queued, then taken, then done.
  function rollupHtml(roll, have) {
    const r = roll.result;
    const stale = r.summaries != null && have != null && r.summaries !== have;
    const head = (t) => `<div class="fw-semibold small mt-2">${t}</div>`;
    const issues = (r.issues || []).length ? head("Issues: for (+1) and against (−1), each person once") +
      `<table class="table table-sm small mb-1"><tbody>${r.issues.map((i) => `<tr><td>${esc(i.issue)}${i.note ? `<div class="text-muted">${esc(i.note)}</div>` : ""}</td>` +
        `<td class="text-end text-nowrap">+${Number(i.plus) || 0}</td><td class="text-end text-nowrap">−${Number(i.minus) || 0}</td></tr>`).join("")}</tbody></table>` : "";
    const hot = (r.hot_spots || []).length ? head("Hot spots") + `<ul class="small ps-3 mb-1">${r.hot_spots.map((h) => `<li><span class="fw-semibold">${esc(h.where)}</span>${h.tone != null ? ` <span class="text-muted">tone ${esc(h.tone)}/5</span>` : ""}: ${esc(h.why)}</li>`).join("")}</ul>` : "";
    const points = (r.talking_points || []).length ? head("Talking points") + `<ul class="small ps-3 mb-1">${r.talking_points.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
    return `<div class="fw-semibold small mb-1">Thread summary <span class="fw-normal text-muted">· from ${r.summaries ?? "?"} summaries${r.by ? ` · ${esc(String(r.by).replace(/^agent:.*$/, "a worker"))}` : ""}</span></div>` +
      (r.about ? `<p class="small mb-1"><span class="fw-semibold">About:</span> ${esc(r.about)}</p>` : "") + `<p class="small mb-1">${esc(r.summary)}</p>` + issues + hot + points +
      `<button type="button" class="btn btn-sm btn-ghost mt-1" data-thread-summarise>${stale ? `Refresh (${have} summaries now)` : "Refresh"}</button>`;
  }
  function rollupSlot(bar, roll, have) {
    const slot = qs("[data-thread-sum]", bar);
    if (!slot) return;
    if (roll.result) slot.innerHTML = rollupHtml(roll, have);
    else if (roll.status === "queued" || roll.status === "running") slot.innerHTML = `<div class="small text-muted"><span class="spinner-border spinner-border-sm me-1"></span>${roll.status === "running" ? "A worker is writing it…" : "Queued for a worker…"}</div>`;
    else slot.innerHTML = `${roll.status === "failed" || roll.status === "cancelled" ? `<div class="small text-danger mb-1">The last try ${esc(roll.status)}.</div>` : ""}<button type="button" class="btn btn-sm btn-outline-oaken" data-thread-summarise>Summarise the thread</button>`;
  }
  const rollupTimers = new WeakMap();
  function pollRollup(bar, have) {
    clearInterval(rollupTimers.get(bar));
    const started = Date.now();
    rollupTimers.set(bar, setInterval(async () => {
      if (!document.body.contains(bar) || Date.now() - started > 15 * 60 * 1000) { clearInterval(rollupTimers.get(bar)); return; }
      try {
        const roll = (await api("get_thread_rollup", { message_id: bar.dataset.id })).output || { status: "none" };
        if (roll.status !== "queued" && roll.status !== "running") clearInterval(rollupTimers.get(bar));
        rollupSlot(bar, roll, have);
      } catch { /* the next tick tries again */ }
    }, 6000));
  }
  const faceOf = (m) => (((m.stages || {}).enrich || []).find((x) => x.key === "sentiment.face") || {}).value || "";

  // Who wrote it, as a Member would say it: the name (a list's "X via members" is X), then the Apache id when the address is an apache.org one.
  // A list's own address is never the sender: if there is no name, the address stays as is.
  function senderLabel(m) {
    const name = String(m.from_name || "").replace(/\s+via\s+\S+$/i, "").trim();
    const addr = String(m.from_addr || m.from || "");
    const id = /@apache\.org$/i.test(addr) ? addr.split("@")[0] : "";
    if (name) return id && id.toLowerCase() !== name.toLowerCase() ? `${name} (${id})` : name;
    return addr;
  }

  // The thread pill's Enrich icon opens this: what is already stored for each message, in thread order and indented under what it answers,
  // with the queueing as a button inside it.
  async function openThreadPane(bar) {
    const box = qs("[data-thread-results]", bar), status = qs("[data-thread-status]", bar);
    if (bar.dataset.busy) return;
    if (!box.hidden && box.dataset.pane === "analysis") { box.hidden = true; return; } // a second click closes it
    status.textContent = "Loading the analysis…";
    try {
      const { items = [], total = 0 } = (await api("thread_enrichments", { message_id: bar.dataset.id })).output || {};
      const text = new Map(items.map((i) => [i.message_id, i.summary]));
      const nodes = (THREAD_NODES[bar.dataset.id] || []).length ? THREAD_NODES[bar.dataset.id] : items; // the nodes carry names and depth; the op's rows are the fallback
      const have = items.filter((i) => i.summary).length;
      const faces = new Map();
      nodes.forEach((m) => { const f = faceOf(m); if (f) faces.set(f, (faces.get(f) || 0) + 1); });
      const roll = (await api("get_thread_rollup", { message_id: bar.dataset.id })).output || { status: "none" };
      box.dataset.pane = "analysis";
      box.innerHTML = `<div class="mb-3" data-thread-sum></div>` +
        (faces.size ? `<div class="small mb-2" aria-label="Tone of the thread">Tone ${[...faces].map(([f, n]) => `<span class="ms-2">${esc(f)} ${n}</span>`).join("")}</div>` : "") +
        `<div class="fw-semibold small mb-1">Analysis <span class="fw-normal text-muted">· ${have} of ${total} messages enriched</span></div>` +
        (have < total ? `<div class="mb-2"><button type="button" class="btn btn-sm btn-outline-oaken" data-thread-run data-n="${total - have}">Enrich the other ${total - have}</button></div>` : "") +
        (nodes.length ? `<ul class="small list-unstyled mb-0">${nodes.map((m) => {
          const id = m.message_id, depth = Math.min(Number(m.depth) || 0, MAX_INDENT), s = text.get(id);
          return `<li class="mb-2" style="margin-left:${depth * 0.9}rem;${depth ? "border-left:2px solid var(--ok-border);padding-left:.6rem" : ""}">` +
            `${faceOf(m) ? `<span class="me-1">${esc(faceOf(m))}</span>` : ""}<span class="fw-semibold">${esc(senderLabel(m))}</span> <span class="text-muted">${esc(String(m.date || "").slice(0, 10))}</span>` +
            `<div>${s ? esc(s) : '<span class="text-muted">not enriched</span>'}</div></li>`;
        }).join("")}</ul>` : '<div class="small text-muted">No messages.</div>');
      box.hidden = false;
      status.textContent = "";
      rollupSlot(bar, roll, have);
      if (roll.status === "queued" || roll.status === "running") pollRollup(bar, have);
    } catch (err) {
      status.textContent = "";
      toast((err && err.message) || "Could not load the analysis.");
    }
  }
  async function runThread(bar, opts) {
    if (bar.dataset.busy) return;
    bar.dataset.busy = "1";
    const id = bar.dataset.id, status = qs("[data-thread-status]", bar), box = qs("[data-thread-results]", bar), btn = qs("[data-thread-enrich]", bar);
    btn.classList.add("active");
    try {
      const have = (await api("thread_progress", { message_id: id })).output;
      let redo = false;
      if (have.done && !(opts && opts.asked)) { const pick = await askEnrich(have.done); if (!pick) return; redo = pick === "redo"; }
      const q = await mutate("enrich_thread", { message_id: id, redo });  // shortcut: after a redo the wait below counts old summaries as done, so it can end early
      if (!q) return;
      const total = q.queued.length + (q.kept || 0);  // the kept ones already have a summary and count as done
      let done = 0;
      status.textContent = q.kept ? `Queued ${q.queued.length}, kept ${q.kept} already summarised…` : `Queued ${total} messages…`;
      for (let waited = 0; done < total && waited < 300000; waited += 3000) { // shortcut: polls every 3s for up to 5 minutes; a longer thread summarises what is ready
        await sleep(3000);
        done = (await api("thread_progress", { message_id: id })).output.done;
        status.textContent = done ? `Enriching ${done} of ${total}…` : `Queued ${total} messages…`;
      }
      status.textContent = "Summarising the thread…";
      const out = (await api("summarize_thread", { message_id: id })).output;
      status.textContent = `${out.messages} messages` + (out.without_summary ? `, ${out.without_summary} not enriched yet` : "");
      box.innerHTML = threadSummaryHtml(out);
      box.dataset.pane = "summary";
      box.hidden = false;
    } catch (err) {
      status.textContent = "";
      toast((err && err.message) || "Could not summarise the thread.");
    } finally { delete bar.dataset.busy; btn.classList.remove("active"); }
  }

  function readerHtml(b, thread) {
    STAGE_CACHE[b.message_id] = b;
    const tags = []
      .concat((b.tags || []).map((t) => tag(t)))
      .concat((b.labels || []).map((l) => tag(l, window.Oaken.labelColor(l))))
      .join("");
    const atts = (b.attachments || [])
      .map((a) => `<a class="ok-attach" href="#"><i class="bi ${esc(a.icon || "bi-file-earmark")} fs-5"></i><span>${esc(a.filename)}<br><span class="small text-muted">${esc(a.size || "")}</span></span></a>`)
      .join("");
    // b.text is the cleaned plain text core produced. Mail is untrusted: it is escaped and shown
    // as text, never as markup.
    const nodes = (thread && thread.items) || [];
    const byId = new Map(nodes.map((m) => [m.message_id, m]));
    const nameOf = (m) => (m && (m.from_name || m.from_addr)) || "";
    const card = (m) => {
      const isCur = m.message_id === b.message_id;
      const replyTo = m.parent_id ? nameOf(byId.get(m.parent_id)) : "";
      return isCur
        ? cardHtml({ ...b, depth: m.depth }, { current: true, text: b.text, atts, to: recipients(b), replyTo })
        : cardHtml(m, { current: false, replyTo });
    };
    const many = nodes.length > 1;
    const inList = nodes.some((m) => m.message_id === b.message_id);
    // A thread that did not list the opened message (filtered, or past the cap) still shows it.
    const body = many
      ? nodes.map(card).join("") + (inList ? "" : cardHtml(b, { current: true, text: b.text, atts, to: recipients(b) }))
      : cardHtml(b, { current: true, text: b.text, atts, to: recipients(b) });
    return (
      `<div class="ok-reader-inner">` +
      `<h1>${esc(b.subject)}</h1>` +
      (many ? `<div class="small text-muted mb-2">${thread.total || nodes.length} messages in this conversation</div>` + threadBarHtml(b.message_id, nodes) : "") +
      `<div class="d-flex gap-2 mb-3 flex-wrap" data-reader-tags>${tags}</div>` +
      body +
      (thread && thread.cut ? `<div class="small text-muted">Showing ${nodes.length} of ${thread.total} messages.</div>` : "") +
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

  /* ---- compose: the From box lists every account that can send (list_senders), whatever its provider ---- */
  let sendersP = null;
  function fillSenders(root) {
    sendersP = sendersP || api("list_senders", {}).then((r) => r.output.senders || []).catch(() => { sendersP = null; return []; });
    sendersP.then((senders) => {
      if (!senders.length) return; // keep the page's placeholder options when core offers none
      let def = ""; // shortcut: the default sender is remembered by this browser only; move it to core's settings if a second browser should share it
      try { def = localStorage.getItem("oaken.defaultSender") || ""; } catch (e) { /* private window: no default */ }
      root.querySelectorAll("select[name=from]").forEach((sel) => {
        sel.innerHTML = senders.map((s) => `<option value="${esc(s.account)}"${s.account === def ? " selected" : ""}>${esc(s.address || s.account)}${s.provider ? ` (${esc(s.provider)})` : ""}</option>`).join("");
      });
    });
  }

  window.Oaken.fillSenders = fillSenders;

  async function sendFrom(box, btn) {
    if (!box) return;
    const chips = (n) => {
      const wrap = box.querySelector(`[name=${n}]`)?.closest("[data-chips]");
      if (!wrap) return [];
      const typed = wrap.querySelector("input").value.trim(); // an address typed but not yet turned into a chip still counts
      return [...qsa(".ok-chip span:first-child", wrap).map((c) => c.textContent), ...(typed ? [typed] : [])];
    };
    const input = {
      account: box.querySelector("[name=from]")?.value || "",
      to: chips("to"), cc: chips("cc"), bcc: chips("bcc"),
      subject: box.querySelector("[name=subject]")?.value || "",
      body: box.querySelector(".ok-editor")?.innerText || "",
    };
    btn.disabled = true;
    try {
      await api("send", input, { confirm: true });
      toast("Sent.");
      box.querySelector("[data-action=close-float]")?.click();
      bootstrap.Modal.getInstance(box.closest(".modal"))?.hide();
    } catch (e) {
      toast(e.message || "Send failed.");
    } finally {
      btn.disabled = false;
    }
  }

  function wireActions() {
    document.addEventListener("change", (e) => {
      const sel = e.target.closest("[data-enrich-pick]");
      if (!sel) return;
      const card = sel.closest(".ok-thread-msg"), box = qs("[data-stage-results]", card);
      box.dataset.pick = sel.value; // kept per message pane, so a redraw keeps the same one
      box.innerHTML = stageResultsHtml(STAGE_CACHE[card.dataset.id], "enrich", sel.value);
    });
    document.addEventListener("click", async (e) => {
      const te = e.target.closest("[data-thread-enrich]");
      if (te) { openThreadPane(te.closest("[data-thread-bar]")); return; }
      const ts = e.target.closest("[data-thread-summarise]");
      if (ts) {
        const bar = ts.closest("[data-thread-bar]");
        if (!(await askYesNo("Summarise this thread? The stored summaries are sent to a worker, which writes one rollup. Nothing is re-enriched."))) return;
        ts.disabled = true;
        api("request_thread_rollup", { message_id: bar.dataset.id }).then(({ output }) => {
          rollupSlot(bar, { status: "queued" });
          pollRollup(bar, output.summaries);
        }).catch((err) => { ts.disabled = false; toast((err && err.message) || "Could not queue the thread summary."); });
        return;
      }
      const tr = e.target.closest("[data-thread-run]");
      if (tr) {
        const n = Number(tr.dataset.n) || 0;
        if (await askYesNo(`Enrich the other ${n} message${n === 1 ? "" : "s"}? They are queued for the model. The ones already done are left alone.`)) runThread(tr.closest("[data-thread-bar]"), { asked: true });
        return;
      }
      const again = e.target.closest("[data-stage-rerun]");
      if (again) { // run a later stage again for this message: the stages before it are skipped
        const id = again.closest(".ok-thread-msg").dataset.id;
        await mutate("rerun_stage", { message_id: id, stage: again.dataset.stageRerun }, "Queued. Reopen in a moment for the new results.");
        return;
      }
      const pick = e.target.closest("[data-stage-pick]");
      if (pick) { // open one stage's results in this card, collapse the rest of the thread; a second click closes it
        const card = pick.closest(".ok-thread-msg"), pill = pick.closest("[data-stage-panel]"), box = qs("[data-stage-results]", card);
        pill.dataset.open = pill.dataset.open === pick.dataset.stagePick ? "" : pick.dataset.stagePick;
        qsa("[data-stage-pick]", pill).forEach((b) => { const on = b.dataset.stagePick === pill.dataset.open; b.classList.toggle("active", on); b.setAttribute("aria-pressed", String(on)); });
        card.classList.toggle("has-stage", !!pill.dataset.open);
        if (!pill.dataset.open) { box.hidden = true; return; }
        qsa(".ok-thread-msg", card.parentElement).forEach((c) => { if (c !== card) c.classList.add("collapsed"); });
        box.hidden = false;
        if (!STAGE_CACHE[card.dataset.id]) {
          box.textContent = "Loading…";
          try { STAGE_CACHE[card.dataset.id] = (await api("get_message", { message_id: card.dataset.id })).output; }
          catch (err) { box.textContent = "Could not load results."; return; }
        }
        box.innerHTML = stageResultsHtml(STAGE_CACHE[card.dataset.id], pill.dataset.open, box.dataset.pick);
        return;
      }
      const t = e.target.closest("[data-oaken-action]");
      if (!t) return;
      const a = t.dataset.oakenAction;
      const list = qs("[data-oaken-list]");
      if (a === "refresh") { list && loadList(list); }
      else if (a === "sync") { mutate("sync_now", {}).then((o) => { if (o) { toast(o.started.length ? `Sync started (${o.started.length}).` : "Nothing to sync."); setTimeout(() => list && loadList(list), 4000); } }); }
      else if (a === "discard" || a === "empty-discarded") {
        const inDiscarded = new URL(location.href).searchParams.get("view") === "discarded";
        if (a === "empty-discarded") {
          if (!confirm("Delete every discarded message for good? This cannot be undone.")) return;
          mutate("empty_discarded", {}).then((out) => { if (out) { toast(`${out.purged} deleted.`); list && loadList(list); renderSidebar(); } });
          return;
        }
        const ids = list ? selectedIds(list) : [];
        if (!ids.length) { toast("Select messages first."); return; }
        mutate("set_discarded", { ids, discarded: !inDiscarded }).then((out) => {
          if (!out) return;
          ids.forEach((id) => qs(`.ok-msg[data-id="${CSS.escape(id)}"]`, list)?.remove());
          renderSidebar();
          toast(`${ids.length} ${inDiscarded ? "restored" : "discarded"}.`);
        });
      }
      else if (a === "archive") {
        const ids = list ? selectedIds(list) : [];
        if (!ids.length) { toast("Select messages first."); return; }
        const inArchive = new URL(location.href).searchParams.get("view") === "archive";
        mutate("set_archived", { ids, archived: inArchive }).then((out) => {
          if (!out) return;
          ids.forEach((id) => qs(`.ok-msg[data-id="${CSS.escape(id)}"]`, list)?.remove()); // it leaves this view either way
          renderSidebar();
          toast(`${ids.length} ${inArchive ? "moved back to the inbox" : "archived"}.`);
        });
      }
      else if (a === "mark-view-read") {
        const n = view.total;
        askYesNo(`Mark ${n === null ? "everything" : n.toLocaleString() + " message" + (n === 1 ? "" : "s")} in this view as read?`).then(async (yes) => {
          if (!yes) return;
          const out = await mutate("mark_view_read", inputFor(list.closest("[data-oaken-list]") || list, "list_messages").input);
          if (!out) return;
          toast(`${out.marked.toLocaleString()} marked read.`);
          loadList(list.closest("[data-oaken-list]") || list); renderSidebar();
        });
      }
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
        sendFrom(t.closest(".ok-float, .modal-content"), t);
      }
    });
  }

  /* ---- sidebar: folders (organization) and labels (search), from the backend ----
   * Folders are the default dispositions plus each distinct location (ADR 0008). Labels are the
   * search tags. Both come from list_labels, so nothing is assembled from mail in the client. */
  const FOLDER_ICON = { inbox: "bi-inbox", filed: "bi-folder2", discarded: "bi-trash3" };
  // A badge is the UNREAD count (like a mail client); nothing shows when there is none.
  function badge(n) { return n ? `<span class="count unread">${n}</span>` : ""; }
  // Put the unread count on one of the fixed top links (keyed by data-view: inbox, starred, ...).
  function topBadge(view, n) {
    const a = qs(`.ok-nav a[data-view="${view}"]`);
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

  /* ---- the one sidebar, used on every page ----
   * Every template carries an empty <aside class="ok-sidebar" data-oaken-sidebar></aside>; this
   * fills it with the canonical chrome (21b C4): Compose, the fixed views (Inbox, Starred,
   * Discarded), the Folders section, the Labels section, and More. The fixed views link to
   * index.html with the ?view/?folder/?label the list reads; the active one is marked from the
   * current URL. Folders and labels are then filled from the backend by renderSidebar(). Keeping
   * the markup here means one definition instead of a copy per page. */
  // The four locked stages: id, full name, icon. Used by the masthead, the sidebar and the stage page.
  const STAGE_META = [["intake", "Intake & Store", "bi-database"], ["triage", "Triage", "bi-bandaid"], ["enrich", "Enrich", "bi-stars"], ["act", "Act", "bi-clipboard-check"]];
  const stageLink = (id, name, icon, cls, label) =>
    `<a ${cls} href="stage.html?s=${id}" data-stage-link="${id}" data-stage="${id}" title="${esc(name)}"><i class="bi ${icon}"></i>${label ? esc(name) : ""}<span class="ok-pip" hidden></span></a>`;

  const SIDEBAR_HTML = `
    <button class="btn btn-oaken btn-compose" data-action="compose"><i class="bi bi-pencil-square"></i>Compose</button>
    <nav class="ok-nav" data-oaken-nav>
      <a href="index.html?view=new" data-view="new"><i class="bi bi-envelope-exclamation"></i>What's new</a>
      <a href="index.html?view=inbox" data-view="inbox"><i class="bi bi-inbox"></i>Inbox</a>
      <a href="index.html?view=starred" data-view="starred"><i class="bi bi-star"></i>Starred</a>
      <a href="index.html?view=sent" data-view="sent"><i class="bi bi-send"></i>Sent</a>
      <a href="index.html?view=drafts" data-view="drafts"><i class="bi bi-file-earmark"></i>Drafts</a>
      <a href="index.html?view=archive" data-view="archive"><i class="bi bi-archive"></i>Archive</a>
      <div class="ok-nav-heading" data-fold-folders role="button" tabindex="0" aria-expanded="true" style="cursor:pointer"><span>Folders</span><span class="d-flex align-items-center gap-1"><button type="button" class="btn btn-ghost ok-nav-plus" data-oaken-plus="folder" title="New folder" aria-label="New folder"><i class="bi bi-plus-lg"></i></button><i class="bi bi-chevron-down"></i></span></div>
      <div data-oaken-folders><div class="small text-muted px-3 py-1">No folders yet.</div></div>
      <a href="index.html?view=discarded" data-view="discarded"><i class="bi bi-trash3"></i>Discarded</a>
      <div class="ok-nav-heading"><span>Labels</span><button type="button" class="btn btn-ghost ok-nav-plus" data-oaken-plus="label" title="New label" aria-label="New label"><i class="bi bi-plus-lg"></i></button></div>
      <div data-oaken-labels><div class="small text-muted px-3 py-1">No tags yet.</div></div>
      <div class="ok-nav-heading">More</div>
${STAGE_META.map(([id, name, icon]) => stageLink(id, name, icon, "", true)).join("")}
      <a href="accounts.html" data-page="accounts.html"><i class="bi bi-person-badge"></i>Accounts</a>
      <a href="sieve.html" data-page="sieve.html" data-feature="sieve filters" hidden><i class="bi bi-funnel"></i>Filters</a>
      <a href="settings.html" data-page="settings.html"><i class="bi bi-gear"></i>Settings</a>
    </nav>`;

  // Folders heading folds the tree; the choice is remembered in this browser only.
  function foldFolders(open) {
    const h = qs("[data-fold-folders]"), box = qs("[data-oaken-folders]");
    if (!h || !box) return;
    box.hidden = !open;
    h.setAttribute("aria-expanded", String(open));
    qs(".bi-chevron-down, .bi-chevron-right", h).className = "bi " + (open ? "bi-chevron-down" : "bi-chevron-right");
    try { localStorage.setItem("oaken.foldersOpen", open ? "1" : "0"); } catch { /* private mode: it just resets */ }
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-oaken-plus]")) return; const h = e.target.closest("[data-fold-folders]"); if (h) foldFolders(h.getAttribute("aria-expanded") !== "true"); });
  document.addEventListener("keydown", (e) => { const h = e.target.closest && e.target.closest("[data-fold-folders]"); if (h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); foldFolders(h.getAttribute("aria-expanded") !== "true"); } });

  // Folders the person folded, by path; kept in this browser only.
  function foldedPaths() { try { return new Set(JSON.parse(localStorage.getItem("oaken.foldedPaths") || "[]")); } catch { return new Set(); } }
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-fold-path]");
    if (!c) return;
    e.preventDefault(); e.stopPropagation(); // the caret folds; the row still opens the folder
    const set = foldedPaths(), p = c.dataset.foldPath;
    set.has(p) ? set.delete(p) : set.add(p);
    try { localStorage.setItem("oaken.foldedPaths", JSON.stringify([...set])); } catch { /* private mode: it just resets */ }
    renderSidebar({ lazy: true });
  });

  function markActive(nav) {
    const url = new URL(window.location.href);
    const page = (url.pathname.split("/").pop() || "index.html");
    const view = url.searchParams.get("view") || (url.searchParams.get("folder") ? "" : "inbox");
    const folder = url.searchParams.get("location") || url.searchParams.get("folder") || "";
    const label = url.searchParams.get("label") || "";
    nav.querySelectorAll("a").forEach((a) => a.classList.remove("active"));
    const hit =
      (page === "index.html" && folder && nav.querySelector(`a[href="index.html?location=${encodeURIComponent(folder)}"]`)) ||
      (page === "index.html" && label && nav.querySelector(`a[href="index.html?label=${encodeURIComponent(label)}"]`)) ||
      (page === "index.html" && view && nav.querySelector(`a[data-view="${view}"]`)) ||
      (page === "stage.html" && nav.querySelector(`a[data-stage="${url.searchParams.get("s") || "intake"}"]`)) ||
      nav.querySelector(`a[data-page="${page}"]`);
    if (hit) hit.classList.add("active");
  }

  /* ---- masthead: the four stages and settings, far right. A red pip says something in that stage failed. ---- */
  function buildMasthead() {
    qsa("[data-oaken-masthead]").forEach((m) => {
      m.innerHTML = STAGE_META.map(([id, name, icon]) => stageLink(id, name, icon, 'class="btn btn-ghost position-relative"', false)).join("") +
        `<a class="btn btn-ghost" href="settings.html" title="Settings"><i class="bi bi-gear"></i></a>`;
    });
  }

  async function refreshPips() {
    try {
      const { output } = await api("stage_health", {});
      for (const st of (output && output.stages) || []) {
        qsa(`[data-stage-link="${st.stage}"]`).forEach((a) => {
          const pip = qs(".ok-pip", a);
          if (pip) pip.hidden = !st.failed;
          a.title = st.failed ? `${st.title}: ${st.failed} failed` : st.title;
        });
      }
    } catch { /* core is away: no pips rather than a wrong one */ }
  }

  async function initStagePage() {
    const card = qs("[data-stage-card]");
    if (!card) return;
    const id = new URLSearchParams(location.search).get("s") || "intake";
    let templates = [];

    const show = (st) => {
      qs("[data-stage-title]").textContent = st.title;
      qs("[data-stage-queue]").innerHTML = (st.queues || []).map((q) => esc(q.title || q.queue) + (q.default ? ' <span class="text-muted fw-normal small">(default)</span>' : "")).join(", ") || "No queue";
      qs("[data-stage-counts]").textContent = `${st.queued} waiting · ${st.running} running · ${st.done} done (1h)`;
      qs("[data-stage-run]").checked = !st.paused;
      qs("[data-stage-workers]").value = st.workers;
      qs("[data-stage-jobs-tab] .ok-pip").hidden = !st.failed; // the look-at-me pip rides on the tab name
      card.hidden = false;
      refreshPips();
    };
    const reload = () => api("get_stage", { stage: id }).then((r) => show(r.output)).catch(() => {});
    const act = async (op, input, ok) => { const out = await mutate(op, { stage: id, ...input }, ok); if (out) { show(out); loadJobs(); } else reload(); };

    /* -- General: the queue card -- */
    try { show((await api("get_stage", { stage: id })).output); } catch (e) { toast(e.message || "Could not load this stage."); return; }
    qs("[data-stage-run]").addEventListener("change", (e) => act(e.target.checked ? "stage_resume" : "stage_pause", {}, e.target.checked ? "Started." : "Paused."));
    qs("[data-stage-save]").addEventListener("click", () => act("stage_set_workers", { workers: Number(qs("[data-stage-workers]").value) }, "Saved. The service is reloading."));
    qs("[data-stage-requeue]").addEventListener("click", () => act("stage_requeue_failed", {}, "Failures requeued."));

    /* -- General: the stage's plugins -- */
    const pluginDetail = (p) =>
      (p.doc ? `<pre class="small text-wrap mb-2" style="white-space:pre-wrap">${esc(p.doc.trim())}</pre>` : "") +
      (p.setup_help ? `<p class="mb-2"><span class="text-muted">Setup</span> ${esc(p.setup_help)}</p>` : "") +
      ((p.settings || []).length ? `<div class="text-muted">Settings (defaults)</div>` + p.settings.map((x) =>
        `<div><code>${esc(x.name)}</code> <span class="text-muted">${esc(x.type)}${x.required ? ", required" : ""}${x.default != null && x.default !== "" ? `, default ${esc(JSON.stringify(x.default))}` : ""}</span> ${esc(x.description || "")}</div>`).join("") : "") +
      ((p.commands || []).length ? `<div class="text-muted mt-2">Commands</div>` + p.commands.map((c) => `<div><code>${esc(c.name)}</code> ${esc(c.summary || "")}</div>`).join("") : "") +
      `<div class="text-muted mt-2">v${esc(p.version || "?")}</div>`;
    // The settings editor: the instance's own values, one field each. Only what you changed is sent.
    const editField = (f) => {
      const id = `pc-${esc(f.name)}`, val = f.value, lbl = `<label class="form-label small mb-1" for="${id}">${esc(f.name)}${f.required ? " *" : ""}</label>`;
      const hint = f.description ? `<div class="form-text">${esc(f.description)}</div>` : "";
      if (f.type === "boolean") return `<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="${id}" data-cfg="${esc(f.name)}" data-type="boolean" ${val ? "checked" : ""}><label class="form-check-label small" for="${id}">${esc(f.name)}</label>${hint}</div>`;
      if (f.type === "choice" && f.choices) return `<div class="mb-2">${lbl}<select class="form-select form-select-sm" id="${id}" data-cfg="${esc(f.name)}" data-type="choice">` +
        (f.choices || []).concat(f.choices && f.choices.includes(val) || val == null || val === "" ? [] : [val]).map((c) => `<option value="${esc(c)}"${c === (val == null ? "" : val) ? " selected" : ""}>${c === "" ? "(default)" : esc(c)}</option>`).join("") + `</select>${hint}</div>`;
      const num = f.type === "integer" || f.type === "number";
      const shown = val == null ? "" : typeof val === "object" ? JSON.stringify(val) : val;
      const area = f.type !== "string" && !num;
      return `<div class="mb-2">${lbl}` + (area ? `<textarea class="form-control form-control-sm font-monospace" rows="2" id="${id}" data-cfg="${esc(f.name)}" data-type="json">${esc(shown)}</textarea>`
        : `<input class="form-control form-control-sm" id="${id}" type="${num ? "number" : "text"}" data-cfg="${esc(f.name)}" data-type="${esc(f.type)}" value="${esc(shown)}">`) + `${hint}</div>`;
    };
    // Settings open in a modal, not inline. The modal lives in the page; this fills it for one plugin.
    const sm = qs("#pluginSettingsModal");
    async function openSettings(row) {
      const body = qs("[data-settings-body]", sm), save = qs("[data-plugin-save]", sm);
      sm.dataset.plugin = row.dataset.plugin;
      qs("[data-settings-title]", sm).textContent = row.dataset.plugin;
      body.innerHTML = '<div class="text-muted">Loading…</div>'; save.hidden = true;
      modalOf(sm)?.show();
      try {
        const out = (await api("get_plugin_config", { plugin: row.dataset.plugin })).output;
        const settings = out.settings || [], wiring = out.wiring || [];
        body._orig = Object.fromEntries(settings.concat(wiring).map((x) => [x.name, x.value == null ? (x.type === "choice" || x.type === "string" ? "" : x.value) : x.value]));
        body.innerHTML = (settings.length ? settings.map(editField).join("") : '<div class="text-muted mb-2">This plugin declares no settings of its own.</div>') +
          (wiring.length ? '<details class="mt-3"><summary class="text-muted small text-uppercase" style="letter-spacing:.08em;cursor:pointer">Advanced: stage, model server, filters</summary><div class="mt-2">' + wiring.map(editField).join("") + "</div></details>" : "");
        save.hidden = false;
      } catch (err) { body.textContent = (err && err.message) || "Could not load its settings."; }
    }
    async function saveSettings() {
      const d = qs("[data-settings-body]", sm), config = {};
      try {
        qsa("[data-cfg]", d).forEach((el) => {
          const name = el.dataset.cfg, t = el.dataset.type;
          let v = t === "boolean" ? el.checked : el.value;
          if (t === "integer") v = v === "" ? null : parseInt(v, 10);
          else if (t === "number") v = v === "" ? null : Number(v);
          else if (t === "json") v = v.trim() === "" ? null : JSON.parse(v);
          if (JSON.stringify(v) !== JSON.stringify(d._orig[name]) && v !== null) config[name] = v;
        });
      } catch (err) { toast("One value is not valid JSON."); return; }
      if (!Object.keys(config).length) { toast("Nothing changed."); return; }
      const out = await mutate("set_plugin_config", { plugin: sm.dataset.plugin, config }, "Saved. Core is reloading.");
      if (out) { modalOf(sm)?.hide(); setTimeout(loadPlugins, 3000); }
    }
    qs("[data-plugin-save]", sm)?.addEventListener("click", saveSettings);
    async function loadPlugins() {
      const box = qs("[data-stage-plugins]");
      try {
        const { output } = await api("get_stage_plugins", { stage: id });
        templates = output.templates || [];
        qs("[data-stage-add]").hidden = !(templates.length || (output.plugins || []).some((p) => !p.enabled));
        box.innerHTML = (output.plugins || []).map((p) =>
          `<div class="d-flex justify-content-between align-items-center py-2 border-top" data-plugin="${esc(p.id)}">` +
          `<div class="flex-grow-1"><a href="#" class="fw-semibold text-reset text-decoration-none" data-plugin-look title="Show what it is and how it is set up">${natureIcon(p.nature)}${esc(p.name)}</a> ${p.started ? (p.healthy ? "" : '<span class="badge text-bg-warning">Unhealthy</span>') : '<span class="badge text-bg-secondary">Off</span>'}` +
          `<div class="text-muted small">${esc(p.summary || "")}${p.warning ? ` <span class="text-danger">${esc(p.warning)}</span>` : ""}</div>` +
          `<div class="small mt-2" data-plugin-detail hidden>${pluginDetail(p)}</div></div>` +
          `<div class="d-flex gap-2 align-items-center"><button class="btn btn-sm btn-ghost text-muted" data-plugin-settings title="Settings"><i class="bi bi-gear"></i></button><button class="btn btn-sm btn-ghost text-muted" data-plugin-remove title="Remove this instance"><i class="bi bi-trash3"></i></button>` +
          `<div class="form-check form-switch m-0"><input class="form-check-input" type="checkbox" role="switch" data-plugin-toggle ${p.enabled ? "checked" : ""} aria-label="Switch ${esc(p.name)}"></div></div></div>`
        ).join("") || '<div class="text-muted small">Nothing here yet.</div>';
      } catch (e) { box.textContent = e.message || "Could not load plugins."; }
    }
    qs("[data-stage-plugins]").addEventListener("change", async (e) => {
      const t = e.target.closest("[data-plugin-toggle]"); if (!t) return;
      const plugin = t.closest("[data-plugin]").dataset.plugin;
      const out = await mutate("stage_plugin_toggle", { plugin, enabled: t.checked }, t.checked ? "Switched on." : "Switched off.");
      if (!out) t.checked = !t.checked; // refused: put the switch back
      loadPlugins();
    });
    qs("[data-stage-plugins]").addEventListener("click", async (e) => {
      const gear = e.target.closest("[data-plugin-settings]");
      if (gear) { openSettings(gear.closest("[data-plugin]")); return; }
      const look = e.target.closest("[data-plugin-look]");
      if (look) { e.preventDefault(); const d = look.closest("[data-plugin]").querySelector("[data-plugin-detail]"); d.hidden = !d.hidden; return; }
      const t = e.target.closest("[data-plugin-remove]"); if (!t) return;
      const plugin = t.closest("[data-plugin]").dataset.plugin;
      if (!confirm(`Remove ${plugin}? Its settings are deleted.`)) return;
      if (await mutate("stage_plugin_remove", { plugin }, "Removed.")) loadPlugins();
    });

    /* -- General: add an instance from a template or plugin -- */
    const modalEl = qs("#addPluginModal"), body = qs("[data-add-body]", modalEl);
    const fieldFor = (f) => {
      const label = `<label class="form-label small mb-1" for="af-${esc(f.name)}">${esc(f.name)}${f.required ? " *" : ""}</label>`;
      const hint = f.description ? `<div class="form-text">${esc(f.description)}</div>` : "";
      if (f.type === "boolean") return `<div class="form-check form-switch mb-3"><input class="form-check-input" type="checkbox" id="af-${esc(f.name)}" data-cfg="${esc(f.name)}" data-type="boolean" ${f.default ? "checked" : ""}><label class="form-check-label" for="af-${esc(f.name)}">${esc(f.name)}</label>${hint}</div>`;
      const type = f.type === "integer" || f.type === "number" ? "number" : "text";
      const list = f.type === "array", dflt = f.default == null ? "" : list && Array.isArray(f.default) ? f.default.join(", ") : esc(f.default);
      return `<div class="mb-3">${label}<input class="form-control" id="af-${esc(f.name)}" type="${type}" data-cfg="${esc(f.name)}" data-type="${esc(f.type)}" value="${dflt}"${list ? ' placeholder="separate with commas"' : ""}>${hint}</div>`;
    };
    qs("[data-stage-add]").addEventListener("click", () => {
      body.innerHTML = `<div class="mb-3"><label class="form-label small mb-1" for="af-pick">What to add</label><select class="form-select" id="af-pick">` +
        templates.map((t, i) => `<option value="${i}">${esc(t.name)} — ${esc(t.summary || "")}</option>`).join("") + `</select><div class="form-text" data-add-nature></div></div><div data-add-fields></div>`;
      const render = () => {
        const t = templates[Number(qs("#af-pick", body).value)]; if (!t) return;
        qs("[data-add-nature]", body).innerHTML = NATURE[t.nature] ? natureIcon(t.nature) + NATURE[t.nature][1] : "";
        qs("[data-add-fields]", body).innerHTML =
          `<div class="mb-3"><label class="form-label small mb-1" for="af-name">Name this one</label><input class="form-control" id="af-name" placeholder="for example work" autocomplete="off"></div>` +
          (t.setup_help ? `<p class="small text-muted">${esc(t.setup_help)}</p>` : "") + (t.settings || []).map(fieldFor).join("");
      };
      qs("#af-pick", body).addEventListener("change", render); render();
      bootstrap.Modal.getOrCreateInstance(modalEl).show();
    });
    qs("[data-add-go]").addEventListener("click", async () => {
      const t = templates[Number(qs("#af-pick", body)?.value)]; if (!t) return;
      const config = {};
      qsa("[data-cfg]", body).forEach((el) => {
        const type = el.dataset.type;
        if (type === "boolean") { config[el.dataset.cfg] = el.checked; return; }
        if (el.value === "") return; // blank means the plugin's own default
        config[el.dataset.cfg] = type === "integer" || type === "number" ? Number(el.value) : type === "array" ? el.value.split(",").map((x) => x.trim()).filter(Boolean) : el.value;
      });
      const out = await mutate("stage_plugin_add", { template: t.id, name: qs("#af-name", body)?.value || "", config }, "Added.");
      if (out) { bootstrap.Modal.getInstance(modalEl)?.hide(); loadPlugins(); }
    });

    /* -- Jobs -- */
    async function loadJobs() {
      const box = qs("[data-stage-jobs]"), status = qs("[data-jobs-status]").value;
      try {
        const { output } = await api("list_stage_jobs", { stage: id, status });
        const jobs = output.jobs || [];
        qs("[data-jobs-count]").textContent = jobs.length ? `${jobs.length} ${status === "queued" ? "waiting" : status}` : "";
        box.innerHTML = jobs.length ? `<div class="table-responsive"><table class="table table-sm align-middle mb-0 bg-transparent"><tbody>` + jobs.map((j) =>
          `<tr data-job="${esc(j.id)}"><td class="text-nowrap"><div class="fw-semibold">${esc(j.kind)}</div><div class="small text-muted">${esc(j.subject || "")}</div></td>` +
          `<td class="small text-muted">${j.attempts}/${j.max_attempts}</td><td class="small ${j.last_error ? "text-danger" : "text-muted"}">${esc(j.last_error || "")}</td>` +
          `<td class="small text-muted text-nowrap" title="${status === "failed" ? "Failed" : "Queued"}: ${esc(new Date(status === "failed" ? j.updated_at : j.created_at).toLocaleString())}">${esc(new Date(status === "failed" ? j.updated_at : j.created_at).toLocaleString([], { dateStyle: "short", timeStyle: "medium" }))}</td>` +
          `<td class="text-end text-nowrap">${status === "failed" ? '<button class="btn btn-sm btn-outline-oaken me-1" data-job-retry>Retry</button>' : ""}${status !== "running" || true ? '<button class="btn btn-sm btn-ghost text-muted" data-job-cancel title="Cancel"><i class="bi bi-x-lg"></i></button>' : ""}</td></tr>`
        ).join("") + `</tbody></table></div>` : `<div class="text-muted small">No ${status === "queued" ? "waiting" : status} jobs.</div>`;
      } catch (e) { box.textContent = e.message || "Could not load jobs."; }
    }
    qs("[data-jobs-status]").addEventListener("change", loadJobs);
    qs("[data-stage-jobs]").addEventListener("click", async (e) => {
      const retry = e.target.closest("[data-job-retry]"), cancel = e.target.closest("[data-job-cancel]");
      if (!retry && !cancel) return;
      const job_id = e.target.closest("[data-job]").dataset.job;
      if (await mutate(retry ? "job_retry" : "job_cancel", { job_id }, retry ? "Requeued." : "Cancelled.")) { loadJobs(); reload(); }
    });
    loadPlugins(); loadJobs();
  }

  // A page that needs a plugin (Sieve) is shown only when core has that plugin: its links carry data-feature and start hidden.
  let featuresP = null;
  function revealFeatures() {
    featuresP = featuresP || api("get_features", {}).then((r) => r.output || {}).catch(() => ({}));
    featuresP.then((f) => qsa("[data-feature]").forEach((el) => { el.hidden = !el.dataset.feature.split(" ").some((k) => f[k]); }));
  }
  document.addEventListener("DOMContentLoaded", revealFeatures);

  function buildSidebar() {
    qsa("[data-oaken-sidebar]").forEach((mount) => {
      mount.innerHTML = SIDEBAR_HTML;
      const nav = qs("[data-oaken-nav]", mount);
      if (nav) { markActive(nav); let open = true; try { open = localStorage.getItem("oaken.foldersOpen") !== "0"; } catch {} foldFolders(open); }
    });
    revealFeatures();
  }

  function paintSidebar(output) {
    const folders = qs("[data-oaken-folders]");
    const labelsBox = qs("[data-oaken-labels]");
    mailbox = { folders: (output && output.folders) || [], labels: (output && output.labels) || [] };
    topBadge("new", (output && output.new && output.new.unread) || 0);
    topBadge("inbox", (output && output.inbox && output.inbox.unread) || 0);
    topBadge("discarded", (output && output.discarded && output.discarded.unread) || 0);
    topBadge("starred", (output && output.starred && output.starred.unread) || 0);
    topBadge("archive", (output && output.archive && output.archive.unread) || 0);
    if (folders) {
      const shut = foldedPaths(), here = new URL(location.href).searchParams.get("location") || "";
      const isShut = (p) => shut.has(p) && !here.startsWith(p + "."); // never hide the folder you are in
      const unreadBelow = (p) => mailbox.folders.some((a) => a.path.startsWith(p + ".") && a.tree.unread > 0); // a count is that folder's own, so a folded parent only hints
      folders.innerHTML = mailbox.folders.length
        ? mailbox.folders.map((f, i) => {
            const kids = mailbox.folders[i + 1] && mailbox.folders[i + 1].depth > f.depth;
            const hidden = mailbox.folders.some((a) => f.path.startsWith(a.path + ".") && isShut(a.path));
            const caret = kids ? `<i class="bi ${isShut(f.path) ? "bi-chevron-right" : "bi-chevron-down"} ms-n1" data-fold-path="${esc(f.path)}" style="font-size:.7rem"></i>` : `<i style="width:.7rem;display:inline-block"></i>`;
            return `<a href="index.html?location=${encodeURIComponent(f.path)}" title="Inbox/${esc(f.path)}"${hidden ? " hidden" : ""} ` +
              `style="padding-left:${0.75 + f.depth * 1.1}rem">${caret}<i class="bi ${FOLDER_ICON.filed}"></i>` +
              `${esc(f.name)}${badge(f.tree.unread)}${isShut(f.path) && unreadBelow(f.path) ? '<span class="ok-below" title="Unread in a folder below">•</span>' : ""}</a>`;
          }).join("")
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
    const nav = qs("[data-oaken-nav]");
    if (nav) markActive(nav); // folders and labels arrive after the first mark, so mark again
  }

  // The last folders, labels and counts are kept in this browser, so the sidebar draws at once and still shows something when core is down.
  // A `lazy` call (a page load, a fold) trusts a copy under 30s old and skips the fetch; any other call asks core.
  async function renderSidebar(opts) {
    if (!qs("[data-oaken-folders]") && !qs("[data-oaken-labels]")) return;
    let shown = null;
    if (opts && opts.lazy) {
      try { shown = JSON.parse(store.get("mailbox", "null")); } catch { /* a bad copy is just not used */ }
      if (shown && shown.output) { paintSidebar(shown.output); if (Date.now() - shown.at < 30000) return; }
    }
    try {
      const { output } = await api("list_mailbox", {});
      store.set("mailbox", JSON.stringify({ at: Date.now(), output }));
      paintSidebar(output);
    } catch (e) {
      // A sidebar failure should not break the page; the copy (or the static fallback) stays in place.
      toast(shown && shown.output ? "Core is not answering: showing the last folders and counts." : (e.message || "Could not load folders."));
    }
  }

  // One sidebar for every page: build the chrome, then fill folders/labels from the backend.
  window.Oaken.sidebar = {
    build: buildSidebar,
    render: renderSidebar,
    refresh: () => { buildSidebar(); return renderSidebar(); },
  };

  /* ---- creating and using folders and labels ---- */
  const modalOf = (el) => (el && window.bootstrap ? window.bootstrap.Modal.getOrCreateInstance(el) : null);
  const folderOptions = (withInbox) =>
    (withInbox ? [{ path: "", label: "Inbox" }] : []).concat(
      mailbox.folders.map((f) => ({ path: f.path, label: indent(f) + f.name }))
    );

  function wireMailboxUi() {
    // The + beside Folders and Labels opens the new-folder / new-label dialog (it lives on the mailbox page).
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-oaken-plus]");
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      const modal = qs(b.dataset.oakenPlus === "label" ? "#newLabelModal" : "#newFolderModal");
      if (modal) modalOf(modal)?.show(); else location.href = "index.html?new=" + b.dataset.oakenPlus;
    });
    const want = new URLSearchParams(location.search).get("new");
    if (want) setTimeout(() => modalOf(qs(want === "label" ? "#newLabelModal" : "#newFolderModal"))?.show(), 300);

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
        out = await mutate("create_folder", { path: parent ? `${parent}/${name}` : name, ids: selectedIds(qs("[data-oaken-list]")) });
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


    // Labels modal (reader kebab): the open message's folder and tags as removable pills, and a box to add a tag.
    {
      const modal = qs("#labelsModal");
      const cur = () => { const id = qs("[data-oaken-reader]")?.dataset.id; return { id, m: STAGE_CACHE[id] }; };
      const x = (attr, v) => `<button type="button" class="btn-close ms-2" style="font-size:.6em" ${attr}="${esc(v)}" aria-label="Remove ${esc(v)}"></button>`;
      const draw = () => {
        const { m } = cur();
        const ls = (m && m.labels) || [];
        const folder = m && m.location ? `<span class="ok-tag" title="Folder${m.location_by ? ", set by " + esc(m.location_by) : ""}"><i class="bi bi-folder2"></i>${esc(m.location)}${x("data-folder-remove", m.location)}</span>` : "";
        qs("[data-oaken-labels-list]", modal).innerHTML = folder + ls.map((l) => `<span class="ok-tag">${esc(l)}${x("data-label-remove", l)}</span>`).join("")
          || '<span class="small text-muted">No labels on this message.</span>';
        qs("#labelsOptions", modal).innerHTML = (mailbox.labels || []).filter((l) => !ls.includes(l.name)).map((l) => `<option value="${esc(l.name)}">`).join("");
        const pills = qs("[data-reader-tags]");
        if (pills && m) pills.innerHTML = (m.tags || []).map((t) => tag(t)).concat(ls.map((l) => tag(l, window.Oaken.labelColor(l)))).join("");
      };
      const done = (id, m, field, val) => {
        m[field] = val;
        const row = itemsById.get(id);
        if (row) row[field] = val;
        draw(); renderSidebar();
        const list = qs("[data-oaken-list]"); list && loadList(list, [id]);
      };
      const change = async (name, on) => {
        const { id, m } = cur();
        if (!m || !(await mutate("set_labels", on ? { ids: [id], add: [name] } : { ids: [id], remove: [name] }))) return;
        const set = new Set(m.labels || []);
        on ? set.add(name) : set.delete(name);
        done(id, m, "labels", [...set]);
      };
      const unfile = async () => {
        const { id, m } = cur();
        if (m && await mutate("move_to_folder", { ids: [id], path: null })) done(id, m, "location", "");
      };
      modal?.addEventListener("show.bs.modal", () => { if (cur().m) draw(); });
      modal?.addEventListener("click", (e) => {
        const b = e.target.closest("[data-label-remove]");
        if (b) change(b.dataset.labelRemove, false);
        else if (e.target.closest("[data-folder-remove]")) unfile();
      });
      qs("[data-oaken-labels-add]", modal)?.addEventListener("submit", async (e) => {
        e.preventDefault();
        const name = e.target.elements.name.value.trim();
        if (name) { await change(name, true); e.target.reset(); }
      });
    }

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
    _sidebarTimer = setTimeout(renderSidebar, 1500); // counts cost seconds in a big mailbox: wait out a burst of marking, then fetch once
  }
  function watchEvents() {
    if (typeof EventSource === "undefined" || window.Oaken.isDemo) return;
    const es = new EventSource("/v1/events");
    es.addEventListener("MessageReadChanged", (e) => {
      try {
        const d = JSON.parse(e.data);
        const row = qs(`.ok-msg[data-id="${CSS.escape(d.message_id)}"]`);
        row && row.classList.toggle("unread", d.read === false);
        // What's new holds only what is unread: once addressed, a row leaves (the open message stays in the reader).
        if (row && d.read === true && new URL(location.href).searchParams.get("view") === "new") { itemsById.delete(d.message_id); row.remove(); }
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
    buildMasthead();
    buildSidebar();
    renderSidebar({ lazy: true });
    refreshPips();
    setInterval(refreshPips, 30000);
    initStagePage();
    wireSearchModes();
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
      const heat = e.target.closest("[data-heat]");
      if (heat) {
        const card = qs(`.ok-thread-msg[data-id="${CSS.escape(heat.dataset.heat)}"]`);
        card?.classList.remove("collapsed"); openThreadCard(card); card?.scrollIntoView({ block: "start", behavior: "smooth" });
        return;
      }
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
