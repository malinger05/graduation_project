/**
 * Poll admin JSON APIs so chain_status / dashboard stats update without manual reload.
 */
(function () {
  const body = document.body;
  const page = body.dataset.adminPage;
  const intervalMs = parseInt(body.dataset.autoRefreshMs || "15000", 10);
  if (!page || !intervalMs) return;

  const liveBadge = document.getElementById("admin-live-badge");
  if (liveBadge) liveBadge.style.display = "inline-block";

  const params = new URLSearchParams(window.location.search);

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMoney(n) {
    return "$" + parseFloat(n || 0).toFixed(2);
  }

  function fmtCreated(iso) {
    if (!iso) return "—";
    return esc(iso.substring(0, 19).replace("T", " "));
  }

  function typeBadge(t) {
    const ty = t.transactionType || "";
    if (ty === "DEPOSIT" || ty === "TRANSFER_IN") {
      return '<span class="badge b-deposit">' + esc(ty) + "</span>";
    }
    if (ty === "WITHDRAW" || ty === "TRANSFER_OUT") {
      return '<span class="badge b-withdraw">' + esc(ty) + "</span>";
    }
    return '<span class="badge b-transfer">' + esc(ty) + "</span>";
  }

  function chainBadge(status) {
    const s = status || "PENDING_SUBMIT";
    if (s === "CONFIRMED") return '<span class="badge b-confirmed">⛓ confirmed</span>';
    if (s === "SUBMITTED") return '<span class="badge b-submitted"><span class="pdot"></span> submitted</span>';
    if (s === "FAILED_SUBMIT") return '<span class="badge b-failed">✗ failed</span>';
    if (s === "TAMPERED") return '<span class="badge b-tampered">⚠ tampered</span>';
    return '<span class="badge b-pending"><span class="pdot"></span> pending</span>';
  }

  function chainLink(tx) {
    if (!tx) return '<span style="color:var(--muted);font-size:12px;">—</span>';
    const h = tx.substring(0, 10);
    return (
      '<a href="https://sepolia.etherscan.io/tx/' +
      esc(tx) +
      '" target="_blank" rel="noopener" class="chain-link">' +
      esc(h) +
      "…</a>"
    );
  }

  function hashCell(hash) {
    if (!hash) return '<span style="color:var(--muted);">—</span>';
    return (
      '<span class="mono" style="color:var(--muted);font-size:10px;" title="' +
      esc(hash) +
      '">' +
      esc(hash.substring(0, 16)) +
      "…</span>"
    );
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  async function refreshDashboard() {
    const resp = await fetch("/admin/api/dashboard", { credentials: "same-origin" });
    if (!resp.ok) return;
    const d = await resp.json();
    setText("stat-total-customers", d.totalCustomers);
    setText("stat-pending-count", d.pendingCount);
    setText("stat-submitted-count", d.submittedCount);
    setText("stat-confirmed-count", d.confirmedCount);
    setText("pipe-pending-count", d.pendingCount);
    setText("pipe-submitted-count", d.submittedCount);
    setText("pipe-confirmed-count", d.confirmedCount);
  }

  function renderTransactionRows(transactions) {
  if (!transactions.length) {
      return "";
    }
    return transactions
      .map(function (t) {
        return (
          "<tr>" +
          '<td><span class="mono" style="color:var(--muted);">#' +
          esc(t.transactionId) +
          "</span></td>" +
          '<td><span class="mono">' +
          esc(t.accountNumber || "—") +
          "</span></td>" +
          "<td>" +
          typeBadge(t) +
          "</td>" +
          '<td><span class="mono" style="color:var(--text);font-weight:500;">' +
          fmtMoney(t.amount) +
          "</span></td>" +
          '<td><span class="mono" style="color:var(--muted);">' +
          fmtMoney(t.balanceAfter) +
          "</span></td>" +
          '<td><span class="badge b-active">APPROVED</span></td>' +
          "<td>" +
          chainBadge(t.chainStatus) +
          "</td>" +
          "<td>" +
          chainLink(t.blockchainTx) +
          "</td>" +
          '<td><span class="mono" style="color:var(--muted);">' +
          esc(t.submitAttempts != null ? t.submitAttempts : "0") +
          "</span></td>" +
          "<td>" +
          fmtCreated(t.createdAt) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function renderBlockchainRows(contracts) {
    if (!contracts.length) return "";
    return contracts
      .map(function (c) {
        return (
          "<tr>" +
          '<td><span class="mono" style="color:var(--muted);">#' +
          esc(c.transactionId) +
          "</span></td>" +
          '<td><span class="mono">' +
          esc(c.accountNumber || "—") +
          "</span></td>" +
          "<td>" +
          typeBadge(c) +
          "</td>" +
          '<td><span class="mono" style="font-weight:500;">' +
          fmtMoney(c.amount) +
          "</span></td>" +
          "<td>" +
          hashCell(c.canonicalHash) +
          "</td>" +
          "<td>" +
          (c.blockchainTx
            ? '<a href="https://sepolia.etherscan.io/tx/' +
              esc(c.blockchainTx) +
              '" target="_blank" rel="noopener" class="chain-link">' +
              esc(c.blockchainTx.substring(0, 12)) +
              "…</a>"
            : '<span style="color:var(--muted);">—</span>') +
          "</td>" +
          "<td>" +
          chainBadge(c.chainStatus) +
          "</td>" +
          "<td>" +
          fmtCreated(c.createdAt) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  async function refreshTransactions() {
    const status = params.get("status") || "ALL";
    const resp = await fetch(
      "/admin/api/transactions?status=" + encodeURIComponent(status),
      { credentials: "same-origin" }
    );
    if (!resp.ok) return;
    const d = await resp.json();
    const sub = document.getElementById("page-subtitle");
    if (sub && d.counts) {
      sub.textContent =
        d.counts.pending +
        " pending · " +
        d.counts.submitted +
        " submitted · " +
        d.counts.confirmed +
        " confirmed";
    }
    const tbody = document.getElementById("transactions-tbody");
    const empty = document.getElementById("transactions-empty");
    const wrap = document.getElementById("transactions-table-wrap");
    if (!tbody) return;
    if (!d.transactions.length) {
      tbody.innerHTML = "";
      if (wrap) wrap.style.display = "none";
      if (empty) empty.style.display = "block";
      return;
    }
    if (wrap) wrap.style.display = "";
    if (empty) empty.style.display = "none";
    tbody.innerHTML = renderTransactionRows(d.transactions);
  }

  async function refreshBlockchain() {
    const type = params.get("type") || "ALL";
    const resp = await fetch(
      "/admin/api/blockchain?type=" + encodeURIComponent(type),
      { credentials: "same-origin" }
    );
    if (!resp.ok) return;
    const d = await resp.json();
    const sub = document.getElementById("page-subtitle");
    if (sub) {
      sub.textContent =
        d.confirmedCount + " confirmed · " + d.tamperedCount + " tampered · Ethereum Sepolia testnet";
    }
    const tbody = document.getElementById("blockchain-tbody");
    const empty = document.getElementById("blockchain-empty");
    const wrap = document.getElementById("blockchain-table-wrap");
    if (!tbody) return;
    if (!d.contracts.length) {
      tbody.innerHTML = "";
      if (wrap) wrap.style.display = "none";
      if (empty) empty.style.display = "block";
      return;
    }
    if (wrap) wrap.style.display = "";
    if (empty) empty.style.display = "none";
    tbody.innerHTML = renderBlockchainRows(d.contracts);
  }

  async function tick() {
    if (document.hidden) return;
    try {
      if (page === "dashboard") await refreshDashboard();
      else if (page === "transactions") await refreshTransactions();
      else if (page === "blockchain") await refreshBlockchain();
    } catch (e) {
      /* ignore transient network errors */
    }
  }

  tick();
  setInterval(tick, intervalMs);
})();
