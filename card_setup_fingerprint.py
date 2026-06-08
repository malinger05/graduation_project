"""
Background fingerprint enrollment / verification for card-setup wizard.
"""
from __future__ import annotations

import threading
from typing import Any

import mw_http
from atm_architecture import MIDDLEWARE_URL
from fingerprint_driver import FingerprintDriver, allocate_fingerprint_slot

_sensor_lock = threading.Lock()
_job_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "done": False,
    "ok": False,
    "phase": "idle",
    "message": "",
    "step": 0,
    "total": 0,
    "error": None,
    "mode": None,
    "slot_id": None,
}


def _set_status(phase: str, message: str, step: int = 0, total: int = 0) -> None:
    with _job_lock:
        _job["phase"] = phase
        _job["message"] = message
        _job["step"] = step
        _job["total"] = total


def get_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


def _reset_job(mode: str) -> None:
    with _job_lock:
        _job.update(
            running=True,
            done=False,
            ok=False,
            phase="starting",
            message="Starting…",
            step=0,
            total=0,
            error=None,
            mode=mode,
        )


def _finish(
    ok: bool,
    message: str = "",
    error: str | None = None,
    slot_id: int | None = None,
) -> None:
    with _job_lock:
        _job["running"] = False
        _job["done"] = True
        _job["ok"] = ok
        _job["phase"] = "done" if ok else "error"
        _job["message"] = message
        _job["slot_id"] = slot_id
        if error:
            _job["error"] = error


def cancel_fingerprint() -> None:
    with _job_lock:
        if _job.get("running"):
            _job["cancel_requested"] = True


def _register_in_core_banking(account_number: str, slot_id: int) -> tuple[bool, str]:
    try:
        resp = mw_http.post(
            f"{MIDDLEWARE_URL}/atm/register-fingerprint",
            json={"accountNumber": account_number, "fingerprintSlotId": slot_id},
            timeout=(5, 15),
        )
    except Exception:
        return False, "Cannot reach banking system to save fingerprint."

    try:
        body = resp.json()
    except Exception:
        body = {}

    if not resp.ok:
        detail = body.get("error") or body.get("detail") or "Could not register fingerprint."
        return False, detail

    return True, body.get("message", "Fingerprint registered.")


def _run_enroll(account_number: str) -> tuple[bool, int | None, str]:
    slot_id = allocate_fingerprint_slot(account_number)
    driver = FingerprintDriver()

    def on_status(phase: str, message: str, step: int, total: int) -> None:
        with _job_lock:
            if _job.get("cancel_requested"):
                driver.cancel()
        _set_status(phase, message, step, total)

    if not _sensor_lock.acquire(blocking=False):
        return False, None, "Fingerprint sensor is busy."

    try:
        driver.open()
        ok = driver.enroll(slot_id, on_status)
        if not ok:
            return False, None, get_status().get("message") or "Enrollment failed."
        reg_ok, reg_msg = _register_in_core_banking(account_number, slot_id)
        if not reg_ok:
            return False, None, reg_msg
        return True, slot_id, reg_msg
    except Exception as exc:
        return False, None, str(exc)
    finally:
        driver.close()
        _sensor_lock.release()


def _run_verify(account_number: str, expected_slot: int) -> tuple[bool, str]:
    driver = FingerprintDriver()

    def on_status(phase: str, message: str, step: int, total: int) -> None:
        with _job_lock:
            if _job.get("cancel_requested"):
                driver.cancel()
        _set_status(phase, message, step, total)

    if not _sensor_lock.acquire(blocking=False):
        return False, "Fingerprint sensor is busy."

    try:
        driver.open()
        ok, msg = driver.identify(expected_slot, on_status)
        return ok, msg
    except Exception as exc:
        return False, str(exc)
    finally:
        driver.close()
        _sensor_lock.release()


def _worker(setup: dict[str, Any]) -> None:
    account_number = setup["accountNumber"]
    had_existing = bool(setup.get("hadExistingCards"))
    expected_slot = setup.get("fingerprintSlotId")

    try:
        if had_existing:
            if expected_slot is None:
                _finish(False, error="No fingerprint on file for this account.")
                return
            ok, msg = _run_verify(account_number, int(expected_slot))
            if ok:
                _finish(True, message=msg, slot_id=int(expected_slot))
            else:
                _finish(False, error=msg)
        else:
            ok, slot_id, msg = _run_enroll(account_number)
            if ok and slot_id is not None:
                _finish(True, message=msg, slot_id=slot_id)
            else:
                _finish(False, error=msg or "Enrollment failed.")
    except Exception as exc:
        _finish(False, error=str(exc))


def start_fingerprint(setup: dict[str, Any]) -> tuple[bool, str]:
    with _job_lock:
        if _job.get("running"):
            return False, "Fingerprint operation already in progress."
        _job["cancel_requested"] = False

    mode = "verify" if setup.get("hadExistingCards") else "enroll"
    _reset_job(mode)
    thread = threading.Thread(target=_worker, args=(setup,), daemon=True)
    thread.start()
    return True, mode


def is_job_running() -> bool:
    with _job_lock:
        return bool(_job.get("running"))


def apply_result_to_setup(setup: dict[str, Any]) -> dict[str, Any]:
    """Copy completed fingerprint job result into card_setup session dict."""
    status = get_status()
    if status.get("done") and status.get("ok"):
        setup["fingerprintOk"] = True
        if status.get("slot_id") is not None:
            setup["fingerprintSlotId"] = status["slot_id"]
    return setup


def start_login_verify(account_number: str, expected_slot: int) -> tuple[bool, str]:
    """Verify fingerprint during ATM secure login (identify only)."""
    setup = {
        "accountNumber": account_number,
        "fingerprintSlotId": expected_slot,
        "hadExistingCards": True,
    }
    return start_fingerprint(setup)


def apply_result_to_pending(pending: dict[str, Any]) -> dict[str, Any]:
    """Copy completed fingerprint job result into pending login session dict."""
    status = get_status()
    if status.get("done") and status.get("ok"):
        pending["fingerprintOk"] = True
    return pending
