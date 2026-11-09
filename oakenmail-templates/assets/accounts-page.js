/*
 * Accounts page wiring (prompt 24). A thin client over the account operations:
 *   list_accounts / get_account        the rows and the per-account detail
 *   create_account                     step 1 of the add wizard
 *   set_account_config                 the receive-only switch + display name
 *   remove_account                     drop an account (mail is kept)
 *   sync_now                           the per-row sync button
 *   list_plugins                       the providers, and how each signs in (below)
 *   <provider>.auth / .auth_status     each provider's own sign-in; core names no provider
 *
 * One key, one meaning: `receive_only` in the account's non-secret config is the whole
 * "listen but not send" interlock. The switch here writes that same key the CLI and prompt 28 read.
 */
(function () {
  "use strict";
  const api = (window.Oaken && window.Oaken.api) || null;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // The providers installed, from list_plugins. How one signs in is read from the commands it
  // offers: `<p>.auth_status` means a consent page to open, `<p>.auth` alone means a password.
  let providers = {};
  async function loadProviders() {
    try {
      const out = (await api("list_plugins", {})).output || {};
      providers = {};
      for (const p of out.plugins || []) {
        if (p.kind !== "provider" || p.name.includes("@")) continue;
        providers[p.name] = p;
      }
    } catch (err) { providers = {}; }
    const sel = $("[data-wiz-provider]");
    if (sel) {
      sel.innerHTML = "";
      for (const name of Object.keys(providers).sort()) {
        const o = document.createElement("option");
        o.value = name; o.textContent = name;
        sel.appendChild(o);
      }
      providerChanged();
    }
  }
  function credKind(provider) {
    const ops = ((providers[provider] || {}).operations) || [];
    if (ops.includes(provider + ".auth_status")) return "consent";
    if (ops.includes(provider + ".auth")) return "password";
    return "none";
  }
  function providerChanged() {
    const sel = $("[data-wiz-provider]");
    const p = providers[sel && sel.value] || {};
    const help = $("[data-wiz-provider-help]");
    if (help) help.textContent = p.setup_help || "";
    // The "listen but not send" switch only means something for a provider that can do both.
    const caps = p.capabilities || [];
    const wrap = $("[data-wiz-ro-wrap]");
    if (wrap) wrap.hidden = !(caps.includes("sync.pull") && caps.includes("send"));
    const sw = $("[data-wiz-secret-wrap]");
    if (sw) sw.hidden = !(sel && sel.value === "gmail");
    const ro = $("[data-wiz-receive-only]");
    if (ro) ro.checked = !!(p.account_defaults && p.account_defaults.receive_only);
  }
  document.addEventListener("change", function (ev) {
    if (ev.target.matches("[data-wiz-provider]")) providerChanged();
  });

  function setStatus(el, kind, msg) {
    if (!el) return;
    el.className = (el.classList.contains("ok-acct-status") ? "ok-acct-status" : "ok-acct-status") + (kind ? " is-" + kind : "");
    el.textContent = msg || "";
  }

  // The status dot comes from a single client function (21b A8): a problem is an error; an account
  // with capabilities is ok; otherwise unknown/warn.
  function dotClass(acct) {
    if (acct.problem) return "is-err";
    if ((acct.capabilities || []).length) return "is-ok";
    return "is-warn";
  }

  function renderRow(acct) {
    const tpl = $("#acctCardTpl").content.cloneNode(true);
    const card = $("[data-acct-card]", tpl);
    card.dataset.accountId = acct.account_id;
    card.dataset.provider = acct.provider;
    $("[data-acct-id]", card).textContent = acct.account_id;
    $("[data-acct-provider]", card).textContent = acct.provider;
    const ic = $("[data-acct-icon]", card);
    if (acct.icon) { ic.classList.add(acct.icon); ic.hidden = false; } // the plugin's own icon, vetted in ui_ops
    $("[data-acct-role]", card).textContent = acct.role ? "· " + acct.role : "";
    const pill = $("[data-acct-pill]", card), bad = !!acct.problem || acct.healthy === false;
    pill.textContent = acct.enabled === false ? "off" : bad ? "needs attention" : "healthy";
    pill.className = "badge rounded-pill ok-acct-pill " + (acct.enabled === false ? "text-bg-secondary" : bad ? "text-bg-warning" : "text-bg-success");
    if (acct.problem) pill.title = acct.problem;
    $("[data-acct-enabled]", card).checked = acct.enabled !== false;
    const caps = $("[data-acct-caps]", card);
    caps.innerHTML = (acct.capabilities || []).map((c) => '<span class="ok-tag">' + c + "</span>").join("") || '<span class="text-muted small">none yet</span>';
    $("[data-acct-display]", card).value = acct.display_name || "";
    $("[data-acct-receive-only]", card).checked = !!acct.receive_only;
    return tpl;
  }

  async function load() {
    const list = $("[data-acct-list]");
    const empty = $("[data-acct-empty]");
    if (!api) { if (empty) empty.textContent = "The API bridge is not connected."; return; }
    try {
      const { output } = await api("list_accounts", {});
      const rows = (output && output.accounts) || [];
      list.innerHTML = "";
      if (!rows.length) { list.innerHTML = '<div class="text-muted small px-1 py-2">No accounts yet. Add one to start.</div>'; return; }
      // Each row is rendered from get_account so the detail (capabilities, problem) is accurate.
      for (const r of rows) {
        let detail = r;
        try { detail = (await api("get_account", { account_id: r.account_id })).output || r; } catch (e) { /* keep the summary */ }
        list.appendChild(renderRow(detail));
      }
    } catch (err) {
      list.innerHTML = '<div class="text-danger small px-1 py-2">Could not load accounts: ' + (err && err.message || err) + "</div>";
    }
  }

  async function saveOptions(card) {
    const status = $("[data-acct-status]", card);
    const id = card.dataset.accountId;
    setStatus(status, "", "Saving…");
    try {
      const current = (await api("get_account", { account_id: id })).output || {};
      const config = Object.assign({}, current.config || {});
      if ($("[data-acct-receive-only]", card).checked) config.receive_only = true;
      else delete config.receive_only;
      await api("set_account_config", { account_id: id, config: config, display_name: $("[data-acct-display]", card).value }, { confirm: true });
      setStatus(status, "ok", "Saved — the running provider keeps its old settings until core restarts.");
      load();
    } catch (err) {
      setStatus(status, "err", "Save failed: " + (err && err.message || err));
    }
  }

  async function checkAccount(card) {
    const status = $("[data-acct-status]", card);
    setStatus(status, "", "Testing…");
    try {
      const res = (await api("account_check", { account_id: card.dataset.accountId }, { confirm: true })).output || {};
      if (res.state === "done") setStatus(status, "ok", "Login works.");
      else setStatus(status, "err", res.detail || "The credential was refused.");
    } catch (err) {
      setStatus(status, "err", "Test failed: " + (err && err.message || err));
    }
  }

  async function removeAccount(card) {
    const status = $("[data-acct-status]", card);
    if (!window.confirm("Remove " + card.dataset.accountId + "? Stored mail is kept; the credential is dropped.")) return;
    try {
      await api("remove_account", { account_id: card.dataset.accountId }, { confirm: true });
      load();
    } catch (err) {
      setStatus(status, "err", "Remove failed: " + (err && err.message || err));
    }
  }

  async function reauth(card) {
    const status = $("[data-acct-status]", card);
    try {
      const res = (await api("account_auth", { account_id: card.dataset.accountId }, { confirm: true })).output || {};
      if (res.open_url) { window.open(res.open_url, "_blank", "noopener"); setStatus(status, "warn", "Finish signing in in the new tab, then choose Check sign-in."); }
    } catch (err) {
      setStatus(status, "err", "Could not start: " + (err && err.message || err));
    }
  }

  async function syncAccount(card) {
    const status = $("[data-acct-status]", card);
    try {
      const out = (await api("sync_now", { account_id: card.dataset.accountId }, { confirm: true })).output || {};
      setStatus(status, out.started && out.started.length ? "ok" : "warn", out.started && out.started.length ? "Sync started." : "No sync schedule found for this account.");
    } catch (err) { setStatus(status, "err", "Sync failed: " + (err && err.message || err)); }
  }

  document.addEventListener("submit", (ev) => { if (ev.target.matches("[data-no-submit]")) ev.preventDefault(); });

  document.addEventListener("change", async function (ev) {
    const sw = ev.target.closest("[data-acct-enabled]");
    if (!sw) return;
    const card = sw.closest("[data-acct-card]");
    try { await api("account_toggle", { account_id: card.dataset.accountId, enabled: sw.checked }, { confirm: true }); load(); }
    catch (err) { sw.checked = !sw.checked; setStatus($("[data-acct-status]", card), "err", "Could not switch: " + (err && err.message || err)); }
  });

  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-acct-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-acct-action");
    const card = btn.closest("[data-acct-card]");
    if (action === "add") return; // the modal opens via data-bs-toggle
    if (!card) return;
    if (action === "toggle") { ev.preventDefault(); card.classList.toggle("is-open"); }
    else if (action === "save") saveOptions(card);
    else if (action === "check") checkAccount(card);
    else if (action === "remove") removeAccount(card);
    else if (action === "reauth") reauth(card);
    else if (action === "sync") syncAccount(card);
  });

  // ---- the add wizard ----
  const wiz = { step: 1, accountId: null, provider: "gmail" };
  const modalEl = $("#addModal");

  function showStep(n) {
    wiz.step = n;
    $$(".ok-wizard-step", modalEl).forEach((s) => s.classList.toggle("is-active", Number(s.dataset.step) === n));
    $("[data-wiz-action=back]", modalEl).hidden = n === 1;
    $("[data-wiz-action=next]", modalEl).hidden = n === 3;
    $("[data-wiz-action=done]", modalEl).hidden = n !== 3;
  }

  function resetWizard() {
    wiz.step = 1; wiz.accountId = null; wiz.provider = "gmail";
    $("[data-wiz-id]", modalEl).value = "";
    $("[data-wiz-name]", modalEl).value = "";
    $("[data-wiz-password]", modalEl).value = "";
    $("[data-wiz-receive-only]", modalEl).checked = true;
    setStatus($("[data-wiz-status]", modalEl), "", "");
    setStatus($("[data-wiz-check]", modalEl), "", "");
    showStep(1);
  }

  if (modalEl) modalEl.addEventListener("show.bs.modal", resetWizard);

  async function wizardNext() {
    const status = $("[data-wiz-status]", modalEl);
    if (wiz.step === 1) {
      wiz.provider = $("[data-wiz-provider]", modalEl).value;
      const id = $("[data-wiz-id]", modalEl).value.trim();
      const name = $("[data-wiz-name]", modalEl).value.trim() || id;
      if (!id) { setStatus(status, "err", "An account id is required."); return; }
      const config = {};
      // Receive-only only makes sense for a receiving provider; SMTP is send-only.
      const roWrap = $("[data-wiz-ro-wrap]", modalEl);
      if (roWrap && !roWrap.hidden && $("[data-wiz-receive-only]", modalEl).checked) config.receive_only = true;
      const secret = $("[data-wiz-secret]", modalEl);
      if (wiz.provider === "gmail") {
        if (!secret.value.trim()) { setStatus(status, "err", "Gmail needs the path to your client secret JSON."); return; }
        config.account = id;
        config.client_secret_file = secret.value.trim();
      }
      setStatus(status, "", "Creating…");
      try {
        await api("create_account", { account_id: id, provider: wiz.provider, display_name: name, config: config }, { confirm: true });
        wiz.accountId = id;
        setStatus(status, "", "");
        // Prepare step 2 for the provider's credential kind.
        const kind = credKind(wiz.provider);
        $("[data-wiz-cred-consent]", modalEl).hidden = kind !== "consent";
        $("[data-wiz-cred-password]", modalEl).hidden = kind !== "password";
        if (kind === "consent") {
          const res = (await api(wiz.provider + ".auth", { account_id: id }, { confirm: true })).output || {};
          $("[data-wiz-consent-link]", modalEl).href = res.open_url || "#";
          $("[data-wiz-consent-state]", modalEl).textContent = "Waiting for you to approve…";
        }
        showStep(2);
      } catch (err) {
        setStatus(status, "err", "Could not create: " + (err && err.message || err) + " (the id may be taken).");
      }
    } else if (wiz.step === 2) {
      setStatus(status, "", "Storing credential…");
      try {
        const kind = credKind(wiz.provider);
        const check = $("[data-wiz-check]", modalEl);
        let res = {};
        if (kind === "consent") {
          const st = (await api(wiz.provider + ".auth_status", { account_id: wiz.accountId })).output || {};
          if (st.state !== "done") { setStatus(status, "warn", "Sign-in is not finished yet (" + (st.state || "pending") + "). Approve in the other tab, then Next."); return; }
          res = (await api(wiz.provider + ".auth", { account_id: wiz.accountId, check: true }, { confirm: true })).output || {};
        } else if (kind === "password") {
          const pw = $("[data-wiz-password]", modalEl).value;
          if (!pw) { setStatus(status, "err", "A password is required."); return; }
          // One call stores it in the OS keyring and tries the login.
          res = (await api(wiz.provider + ".auth", { account_id: wiz.accountId, password: pw }, { confirm: true })).output || {};
          $("[data-wiz-password]", modalEl).value = ""; // never kept in JS state beyond the request
        } else {
          res = { state: "done" };
        }
        if (res.state === "done") setStatus(check, "ok", "The account works. It will sync on its schedule.");
        else setStatus(check, "warn", "Created, but the credential was refused: " + (res.detail || "check it and re-authenticate from the account card."));
        setStatus(status, "", "");
        showStep(3);
      } catch (err) {
        setStatus(status, "err", "Failed: " + (err && err.message || err));
      }
    }
  }

  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-wiz-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-wiz-action");
    if (action === "next") wizardNext();
    else if (action === "back") showStep(Math.max(1, wiz.step - 1));
    else if (action === "done") load();
  });

  // Hide write-path controls when core does not offer the operation (21b C5). The demo and a full
  // core offer everything; a reduced core would omit set_account_config / remove_account.
  async function gateByCapabilities() {
    if (!api || window.Oaken && window.Oaken.isDemo) return;
    try {
      const names = new Set(((await api("list_operations", {})).output.operations || []).map((o) => o.name || o));
      $$("[data-oaken-requires]").forEach((el) => { if (!names.has(el.getAttribute("data-oaken-requires"))) el.hidden = true; });
    } catch (e) { /* leave controls visible if the probe is unavailable */ }
  }

  loadProviders().then(load);
  gateByCapabilities();
})();
