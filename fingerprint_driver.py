"""
Fingerprint sensor driver (serial). Used by card-setup enrollment / identification.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

StatusCallback = Callable[[str, str, int, int], None]

PORT = os.environ.get("FINGERPRINT_PORT", "/dev/ttyUSB1")
BAUD = int(os.environ.get("FINGERPRINT_BAUD", "9600"))
SIMULATE = os.environ.get("FINGERPRINT_SIMULATE", "").strip().lower() in ("1", "true", "yes")


class FingerprintDriver:
    def __init__(self, port: str = PORT, baud: int = BAUD, simulate: bool = SIMULATE):
        self.port = port
        self.baud = baud
        self.simulate = simulate
        self._ser: Optional["serial.Serial"] = None
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def reset_cancel(self) -> None:
        self._cancel = False

    def open(self) -> None:
        if self.simulate:
            return
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install pyserial")
        self._ser = serial.Serial(self.port, self.baud, timeout=2)

    def close(self) -> None:
        if self._ser:
            try:
                self.led(False)
            except Exception:
                pass
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def send_packet(self, cmd: int, param: int = 0) -> bytes:
        if self.simulate:
            time.sleep(0.05)
            if cmd == 0x0026:
                return b"\x55\xaa" + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + b"\x30\x00" + b"\x00\x00"
            if cmd == 0x0060:
                return b"\x55\xaa" + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + b"\x30\x00" + b"\x00\x00"
            if cmd in (0x0022, 0x0023, 0x0024, 0x0025, 0x0051, 0x0001, 0x0012):
                return b"\x55\xaa" + (1).to_bytes(2, "little") + param.to_bytes(4, "little") + b"\x30\x00" + b"\x00\x00"
            return b"\x55\xaa" + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + b"\x30\x00" + b"\x00\x00"

        assert self._ser is not None
        packet = bytearray()
        packet += b"\x55\xAA"
        packet += (1).to_bytes(2, "little")
        packet += param.to_bytes(4, "little")
        packet += cmd.to_bytes(2, "little")
        checksum = sum(packet) & 0xFFFF
        packet += checksum.to_bytes(2, "little")

        self._ser.write(packet)
        time.sleep(0.35 if cmd != 0x0051 else 0.3)
        return self._ser.read(64)

    @staticmethod
    def parse(resp: bytes) -> tuple[Optional[int], Optional[int]]:
        if len(resp) < 12:
            return None, None
        param = int.from_bytes(resp[4:8], "little")
        response = int.from_bytes(resp[8:10], "little")
        return param, response

    def is_ack(self, resp: bytes) -> bool:
        _, response = self.parse(resp)
        return response == 0x0030

    def is_nack(self, resp: bytes) -> bool:
        _, response = self.parse(resp)
        return response == 0x0031

    def led(self, on: bool) -> None:
        self.send_packet(0x0012, 1 if on else 0)

    def finger_pressed(self) -> bool:
        resp = self.send_packet(0x0026)
        param, response = self.parse(resp)
        return response == 0x0030 and param == 0

    def wait_put(self, on_status: StatusCallback, step: int, total: int) -> bool:
        while not self.finger_pressed():
            if self._cancel:
                return False
            on_status("wait_put", f"Place your finger on the sensor ({step}/{total})…", step, total)
            time.sleep(1)
        return True

    def wait_remove(self, on_status: StatusCallback) -> bool:
        while self.finger_pressed():
            if self._cancel:
                return False
            on_status("wait_remove", "Remove your finger…", 0, 0)
            time.sleep(1)
        return True

    def capture(self) -> bool:
        on_status_dummy = lambda *a: None
        return self._capture_once(on_status_dummy)

    def _capture_once(self, on_status: StatusCallback) -> bool:
        on_status("capture", "Reading fingerprint…", 0, 0)
        resp = self.send_packet(0x0060, 1)
        return self.is_ack(resp)

    def enroll(self, slot_id: int, on_status: StatusCallback) -> bool:
        self.reset_cancel()
        on_status("init", "Starting fingerprint enrollment…", 0, 3)
        self.send_packet(0x0001)
        self.led(True)

        resp = self.send_packet(0x0022, slot_id)
        if not self.is_ack(resp):
            on_status("error", "Enrollment could not start. Slot may be in use.", 0, 3)
            return False

        steps = [
            (0x0023, "1st scan — place the same finger"),
            (0x0024, "2nd scan — place the same finger again"),
            (0x0025, "3rd scan — place the same finger one more time"),
        ]
        for i, (cmd, label) in enumerate(steps, start=1):
            if self._cancel:
                return False
            on_status("wait_put", label, i, 3)
            if not self.wait_put(on_status, i, 3):
                return False
            if not self._capture_once(on_status):
                on_status("error", f"Scan {i}/3 failed. Try again.", i, 3)
                return False
            resp = self.send_packet(cmd)
            if not self.is_ack(resp):
                on_status("error", f"Enrollment step {i}/3 rejected by sensor.", i, 3)
                return False
            if not self.wait_remove(on_status):
                return False

        on_status("done", "Fingerprint enrolled successfully.", 3, 3)
        return True

    def identify(self, expected_slot: int, on_status: StatusCallback, max_attempts: int = 3) -> tuple[bool, str]:
        self.reset_cancel()
        on_status("init", "Place your finger to verify identity…", 0, 1)
        self.send_packet(0x0001)
        self.led(True)

        for attempt in range(1, max_attempts + 1):
            if self._cancel:
                return False, "Verification cancelled."

            on_status("wait_put", f"Place your finger ({attempt}/{max_attempts})…", attempt, max_attempts)
            while not self.finger_pressed():
                if self._cancel:
                    return False, "Verification cancelled."
                time.sleep(0.5)

            if not self._capture_once(on_status):
                on_status("retry", "Could not read fingerprint. Try again.", attempt, max_attempts)
                self.wait_remove(on_status)
                continue

            on_status("identify", "Matching fingerprint…", attempt, max_attempts)
            ident = self.send_packet(0x0051)
            ident_param, _ = self.parse(ident)

            if self.is_ack(ident) and ident_param == expected_slot:
                on_status("done", "Fingerprint verified.", 1, 1)
                self.wait_remove(on_status)
                return True, f"Authorized fingerprint (slot {ident_param})."

            if self.is_ack(ident):
                msg = (
                    f"Fingerprint recognized but not authorized for this account "
                    f"(got slot {ident_param}, expected {expected_slot})."
                )
            elif self.is_nack(ident):
                msg = "Fingerprint not recognized."
            else:
                msg = "Unknown response from sensor."

            on_status("retry", msg, attempt, max_attempts)
            self.wait_remove(on_status)

        return False, "Fingerprint verification failed after multiple attempts."


def allocate_fingerprint_slot(account_number: str) -> int:
    """Deterministic sensor slot for a new account (0–126)."""
    return abs(hash(account_number.strip().upper())) % 127
