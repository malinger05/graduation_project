/**
 * ATM idle prompt: after inactivity, ask whether to continue or end session.
 * Config via data-* attributes on #idle-session-root (injected from base.html).
 */
(function () {
  const root = document.getElementById("idle-session-root");
  if (!root) return;

  const idleMs = parseInt(root.dataset.idleMs || "120000", 10);
  const promptTimeoutMs = parseInt(root.dataset.promptTimeoutMs || "60000", 10);
  const continueUrl = root.dataset.continueUrl;
  const logoutUrl = root.dataset.logoutUrl;

  const modal = document.getElementById("idle-session-modal");
  const btnYes = document.getElementById("idle-session-yes");
  const btnNo = document.getElementById("idle-session-no");
  const countdownEl = document.getElementById("idle-session-countdown");

  let lastActivity = Date.now();
  let promptDeadline = null;
  let modalOpen = false;
  let tickTimer = null;

  function resetActivity() {
    lastActivity = Date.now();
  }

  function showModal() {
    if (modalOpen) return;
    modalOpen = true;
    promptDeadline = Date.now() + promptTimeoutMs;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function hideModal() {
    modalOpen = false;
    promptDeadline = null;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  function logout() {
    hideModal();
    fetch(logoutUrl, { method: "POST", credentials: "same-origin" })
      .finally(() => {
        window.location.href = "/login";
      });
  }

  function updateCountdown() {
    if (!modalOpen || !countdownEl || !promptDeadline) return;
    const left = Math.max(0, Math.ceil((promptDeadline - Date.now()) / 1000));
    countdownEl.textContent = String(left);
    if (left <= 0) logout();
  }

  function onTick() {
    if (modalOpen) {
      updateCountdown();
      return;
    }
    if (Date.now() - lastActivity >= idleMs) {
      showModal();
      updateCountdown();
    }
  }

  function onActivity() {
    if (!modalOpen) resetActivity();
  }

  ["mousedown", "keydown", "touchstart", "click", "scroll"].forEach((ev) => {
    document.addEventListener(ev, onActivity, { passive: true });
  });

  if (!btnYes || !btnNo || !modal) return;

  btnYes.addEventListener("click", () => {
    fetch(continueUrl, { method: "POST", credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error("continue failed");
        hideModal();
        resetActivity();
      })
      .catch(() => logout());
  });

  btnNo.addEventListener("click", logout);

  tickTimer = setInterval(onTick, 1000);
})();
