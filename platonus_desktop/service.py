"""Coordinate single submissions and guard against duplicate requests."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
from threading import Lock
from time import monotonic

from .client import PlatonusClient
from .models import AttendanceQR, AttendanceResult, Coordinates, PlatonusError


_CACHE_SIZE = 128
_CACHE_TTL = 120.0


class AttendanceService:
    """Serialize check-ins and remember confirmed or uncertain outcomes briefly."""

    def __init__(self, client: PlatonusClient, *, coordinates: Coordinates | None = None):
        self._client = client
        self._coordinates = coordinates
        self._lock = Lock()
        self._recent: OrderedDict[bytes, tuple[float, AttendanceResult | None]] = OrderedDict()

    def submit(self, qr: AttendanceQR, *, coordinates: Coordinates | None = None) -> AttendanceResult:
        key = hashlib.sha256(f"{qr.action}\0{qr.code}".encode("utf-8")).digest()
        if not self._lock.acquire(timeout=2.0):
            raise PlatonusError("busy", "Another check-in is in progress. Try again shortly.", retryable=True)
        try:
            now = monotonic()
            while self._recent and next(iter(self._recent.values()))[0] <= now:
                self._recent.popitem(last=False)
            if key in self._recent:
                _, previous = self._recent[key]
                if previous is None:
                    raise PlatonusError(
                        "submission_unknown",
                        "An earlier submission for this QR may have reached Platonus. Check the journal first.",
                    )
                return AttendanceResult(True, "Attendance was already confirmed for this QR.", previous.scanned_at)

            try:
                result = self._client.submit(qr, coordinates=coordinates if coordinates is not None else self._coordinates)
            except PlatonusError as exc:
                if exc.code == "submission_unknown":
                    self._remember(key, None)
                raise
            if result.success:
                self._remember(key, result)
            return result
        finally:
            self._lock.release()

    def _remember(self, key: bytes, result: AttendanceResult | None) -> None:
        self._recent[key] = (monotonic() + _CACHE_TTL, result)
        if len(self._recent) > _CACHE_SIZE:
            self._recent.popitem(last=False)

