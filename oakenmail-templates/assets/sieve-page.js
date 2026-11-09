/* Filters page: a file picker with Test, Save and Reload, for each filter engine core has loaded (Sieve, Filters). */
(function () {
  "use strict";
  const O = window.Oaken || {}, api = O.api, toast = O.toast || (() => {});
  const $ = (s, r) => (r || document).querySelector(s);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  if (!api) return;

  // One entry per engine; the operations are <key>_list, _get, _check, _save, _delete.
  const ENGINES = {
    sieve: { title: "Sieve scripts", ext: ".sieve", sub: "Sieve (RFC 5228), run in name order", fresh: 'require ["fileinto"];\n\n' },
    filters: { title: "Filters", ext: ".filters", sub: "One rule per line: QUERY -> ACTION, ACTION. Same syntax as search.",
      fresh: "# QUERY -> ACTION, ACTION   (label NAME, folder PATH, archive, star, read, discard, stop)\nfrom:ann has:attachment -> label work, star\n" },
  };
  const card = $("[data-file-card]"), pick = $("[data-file-pick]"), text = $("[data-file-text]"), nameIn = $("[data-file-name]"),
    status = $("[data-file-status]"), dirtyEl = $("[data-dirty]");
  let engine = null, saved = "", current = null, creating = false;

  // How a folder label is built, shown live: each folder the text files into, the mail.folder value it becomes, and the sidebar path.
  // ${NAME} is filled in from `set "NAME" "value"` lines; anything only known when mail arrives (${1}, ${localpart}) stays as written.
  function foldersIn(src) {
    const vars = {}; let m;
    const set = /\bset\s+(?::\w+\s+)*"([^"]+)"\s+"([^"]*)"/g;
    while ((m = set.exec(src))) vars[m[1]] = m[2];
    const fill = (t) => t.replace(/\$\{(\w+)\}/g, (all, n) => (n in vars ? vars[n] : all));
    const raw = [];
    if (engine === "sieve") { const re = /\bfileinto\b(?:\s+:\w+)*\s+"([^"]+)"/g; while ((m = re.exec(src))) raw.push(fill(m[1])); }
    else { const re = /->.*?\b(?:folder|file)\s+([^,#\n]+)/g; while ((m = re.exec(src))) raw.push(m[1].trim().replace(/^"|"$/g, "")); }
    return [...new Set(raw)].map((r) => {
      let parts = r.split(engine === "sieve" ? "." : /[./]/).filter(Boolean);
      if (parts[0] && parts[0].toUpperCase() === "INBOX") parts = parts.slice(1);
      return { from: r, value: parts.join(".") || "Inbox", path: parts.join(" › ") || "Inbox" };
    });
  }
  function previewFolders() {
    const box = $("[data-folder-preview]"), list = foldersIn(text.value);
    box.hidden = !list.length;
    box.innerHTML = list.length ? '<div class="text-muted mb-1">Folders this files into, and the label each becomes</div>' + list.slice(0, 12).map((f) =>
      `<div><code>${esc(f.from)}</code> → <code>mail.folder = ${esc(f.value)}</code> <span class="text-muted">sidebar: ${esc(f.path)}</span></div>`).join("") +
      (list.length > 12 ? `<div class="text-muted">and ${list.length - 12} more</div>` : "") : "";
  }

  const say = (kind, msg) => { status.className = "ok-file-status" + (kind ? " is-" + kind : ""); status.textContent = msg || ""; };
  const dirty = () => text.value !== saved || creating;
  const mark = () => { dirtyEl.hidden = !dirty(); previewFolders(); };
  const stem = (n) => n.replace(/\.(sieve|filters)$/, "");
  const call = (op, input, opts) => api(`${engine}_${op}`, input || {}, opts).then((r) => r.output || {});
  const sure = () => !dirty() || confirm("Discard your unsaved changes?");

  async function open(name) {
    creating = false; nameIn.hidden = true;
    if (!name) { text.value = saved = ""; current = null; mark(); return; }
    try {
      const out = await call("get", { name });
      text.value = saved = out.text; current = name; pick.value = name; say("", ""); mark();
    } catch (e) { say("err", e.message || "Could not read it."); }
  }

  async function list(select) {
    const out = await call("list");
    $("[data-file-dir]").textContent = out.directory || "";
    const names = (out.scripts || []).map((s) => s.name);
    pick.innerHTML = names.length ? names.map((n) => `<option value="${esc(n)}">${esc(n)}${ENGINES[engine].ext}</option>`).join("") : '<option value="">(no files yet)</option>';
    const problems = out.problems || [];
    $("[data-file-problems]").innerHTML = problems.map((p) => `<div class="ok-file-status is-warn">${esc(p.name)}${ENGINES[engine].ext} is skipped: ${esc(p.problem)}</div>`).join("");
    const want = select && names.includes(select) ? select : names[0];
    if (want) await open(want); else { text.value = saved = ""; current = null; mark(); say("", "No files yet. New starts one."); }
  }

  async function useEngine(key) {
    if (key === engine || !sure()) return;
    engine = key; const e = ENGINES[key];
    $("[data-engine-title]").textContent = e.title; $("[data-engine-sub]").textContent = e.sub;
    document.querySelectorAll("[data-engines] .nav-link").forEach((b) => b.classList.toggle("active", b.dataset.engine === key));
    try { await list(); } catch (err) { say("err", err.message || "Could not load."); }
  }

  async function test() {
    try {
      const out = await call("check", { script: text.value, name: nameIn.hidden ? current || "main" : nameIn.value || "main" });
      say(out.ok ? "ok" : "err", out.ok ? (out.message || "Looks good.") : (out.line ? `Line ${out.line}: ` : "") + (out.error || "Does not parse."));
    } catch (e) { say("err", e.message || "Test failed."); }
  }

  async function save() {
    const name = creating ? stem(nameIn.value.trim()) : current;
    if (!name) { say("err", "Name the file first."); nameIn.focus(); return; }
    try {
      const out = await call("save", { name, text: text.value }, { confirm: true });
      if (!out.saved) { const c = out.check || {}; say("err", `Not saved. ${c.line ? "Line " + c.line + ": " : ""}${c.error || "It does not parse."} Nothing was written.`); return; }
      toast("Saved. It applies to the next message.");
      await list(name); say("ok", "Saved.");
    } catch (e) { say("err", e.message || "Save failed."); }
  }

  async function remove() {
    if (!current || creating) return;
    if (!confirm(`Delete ${current}${ENGINES[engine].ext}? This cannot be undone.`)) return;
    try { await call("delete", { name: current }, { confirm: true }); toast("Deleted."); saved = text.value = ""; await list(); }
    catch (e) { say("err", e.message || "Delete failed."); }
  }

  card.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-act]"); if (!b) return;
    const act = b.dataset.act;
    if (act === "test") test();
    else if (act === "save") save();
    else if (act === "delete") remove();
    else if (act === "reload") { if (sure()) { try { await list(current); say("ok", "Read from disk."); } catch (e) { say("err", e.message || "Could not reload."); } } }
    else if (act === "new") { if (!sure()) return; creating = true; nameIn.hidden = false; nameIn.value = ""; nameIn.focus(); text.value = ENGINES[engine].fresh; saved = ""; current = null; say("", "Name it, then Save."); mark(); }
  });
  pick.addEventListener("change", () => { if (sure()) open(pick.value); else pick.value = current || ""; });
  text.addEventListener("input", mark);
  text.addEventListener("keydown", (ev) => { if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); save(); } });
  window.addEventListener("beforeunload", (ev) => { if (dirty()) { ev.preventDefault(); ev.returnValue = ""; } });

  (async function init() {
    let have = {};
    try { have = (await api("get_features", {})).output || {}; } catch (e) { /* none shown */ }
    const keys = Object.keys(ENGINES).filter((k) => have[k]);
    if (!keys.length) { $("[data-none]").hidden = false; return; }
    card.hidden = false;
    const tabs = $("[data-engines]");
    if (keys.length > 1) {
      tabs.hidden = false;
      tabs.innerHTML = keys.map((k) => `<li class="nav-item"><button class="nav-link" data-engine="${k}">${esc(ENGINES[k].title)}</button></li>`).join("");
      tabs.addEventListener("click", (ev) => { const b = ev.target.closest("[data-engine]"); if (b) useEngine(b.dataset.engine); });
    }
    useEngine(keys[0]);
  })();
})();
