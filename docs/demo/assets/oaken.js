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
      `<span class="date">${clip}${esc(m.date || "")}</span>` +
      `<span class="line2"><span class="subject">${esc(m.subject)}</span>` +
      `<span class="snippet">— ${snippet}…</span>${tags}</span>` +
      `</li>`
    );
  }

  function groupedListHtml(items) {
    if (!items.length) return '<li class="ok-empty-row text-muted p-4 text-center">No messages.</li>';
    let html = "", group = null;
    for (const m of items) {
      if (m.group && m.group !== group) { group = m.group; html += `<div class="ok-list-group">${esc(group)}</div>`; }
      html += rowHtml(m);
    }
    return html;
  }

  async function loadList(container, keepChecked) {
    const list = qs(".ok-list", container) || container;
    const op = container.dataset.oakenList || "list_messages"; // "list_messages" | "search"
    const input = {};
    const url = new URL(window.location.href);
    if (op === "search" || op === "search_keyword") input.query = url.searchParams.get("q") || "";
    const label = url.searchParams.get("label");
    if (label) input.label = label;
    // Folders (ADR 0008): ?location=X filters to a filed folder; ?folder=inbox|filed|discarded
    // selects a disposition. The sidebar links set these.
    const location = url.searchParams.get("location");
    if (location) { input.location = location; input.disposition = "any"; }
    const folder = url.searchParams.get("folder");
    if (folder && ["inbox", "filed", "discarded"].includes(folder)) input.disposition = folder;
    container.classList.add("ok-loading");
    try {
      const { output } = await api(op, input);
      const items = (output && output.items) || [];
      itemsById.clear();
      items.forEach((m) => itemsById.set(m.message_id, m));
      list.innerHTML = groupedListHtml(items);
      (keepChecked || []).forEach((id) => {
        const box = qs(`.ok-msg[data-id="${CSS.escape(id)}"] input[type=checkbox]`, list);
        if (box) box.checked = true;
      });
      if (output && output.degraded) toast("Search ran keyword-only (meaning search unavailable).");
      bindRows(container);
    } catch (e) {
      toast(e.message || "Could not load messages.");
    } finally {
      container.classList.remove("ok-loading");
    }
  }

  /* Clicking a row: split layout loads the reader inline; otherwise follow the link. Mirrors the
   * presentation handler but also fetches the body when a reader pane is present. */
  function bindRows(container) {
    qsa(".ok-msg[data-id]", container).forEach((row) => {
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

  async function loadReader(container, messageId, opts) {
    container.classList.add("ok-loading");
    try {
      const { output } = await api("get_message", { message_id: messageId, include_text: true });
      container.innerHTML = readerHtml(output || {});
      // The reader starts hidden (d-none) beside the "Select a message" placeholder: swap them.
      container.classList.remove("d-none");
      qs(".ok-empty", container.parentElement)?.classList.add("d-none");
      // Opening a message is a user action: core marks it read after its configured delay (and
      // only if auto-marking is on). message_presented is a mutation, so it must be confirmed or
      // core answers with a proposal and arms nothing. The row and the badges change when core
      // announces MessageReadChanged (live updates below), not before.
      if (opts && opts.markPresented && output && output.message_id) {
        api("message_presented", { message_id: output.message_id }, { confirm: true }).catch(() => {});
      }
    } catch (e) {
      toast(e.message || "Could not open message.");
    } finally {
      container.classList.remove("ok-loading");
    }
  }

  function readerHtml(b) {
    const tags = []
      .concat((b.tags || []).map((t) => tag(t)))
      .concat((b.labels || []).map((l) => tag(l, window.Oaken.labelColor(l))))
      .join("");
    const initials = (b.from_name || b.from_addr || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
    const atts = (b.attachments || [])
      .map((a) => `<a class="ok-attach" href="#"><i class="bi ${esc(a.icon || "bi-file-earmark")} fs-5"></i><span>${esc(a.filename)}<br><span class="small text-muted">${esc(a.size || "")}</span></span></a>`)
      .join("");
    // b.text is the cleaned plain text core produced. Mail is untrusted: it is escaped and shown
    // as text, never as markup.
    return (
      `<div class="ok-reader-inner">` +
      `<h1>${esc(b.subject)}</h1>` +
      `<div class="d-flex gap-2 mb-4 flex-wrap">${tags}</div>` +
      `<article class="ok-thread-msg">` +
      `<header data-action="toggle-thread"><span class="ok-avatar">${esc(initials)}</span>` +
      `<div class="flex-grow-1"><div><span class="fw-semibold">${esc(b.from_name || "")}</span> <span class="small text-muted">&lt;${esc(b.from_addr || "")}&gt;</span></div>` +
      (b.to ? `<div class="small text-muted">to ${esc(b.to)}</div>` : "") + `</div>` +
      `<span class="small text-muted">${esc(b.date || "")}</span></header>` +
      (b.security ? `<div class="small text-muted px-3 pb-2"><i class="bi bi-shield-check text-success"></i> ${esc(b.security)}</div>` : "") +
      `<div class="ok-msg-body" style="white-space:pre-wrap">${b.text ? esc(b.text) : "(no text)"}${atts ? `<div class="mt-3">${atts}</div>` : ""}</div>` +
      `</article></div>`
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

