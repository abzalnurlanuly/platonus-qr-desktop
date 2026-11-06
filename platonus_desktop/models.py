"""Public types shared by the client, scanner, and integrations."""

from __future__ import annotations

from dataclasses import dataclass, field


class PlatonusError(RuntimeError):
    """An expected failure safe to show to a user."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.upstream_status = upstream_status


@dataclass(frozen=True, slots=True)
class AttendanceQR:
    raw: str = field(repr=False)
    code: str = field(repr=False)
    action: str
    location_flag: bool


@dataclass(frozen=True, slots=True)
class Coordinates:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if (
            type(self.latitude) not in (int, float)
            or type(self.longitude) not in (int, float)
            or not -90 <= self.latitude <= 90
            or not -180 <= self.longitude <= 180
            or (self.latitude == 0 and self.longitude == 0)
        ):
            raise ValueError("Coordinates must contain a valid latitude and longitude.")


@dataclass(frozen=True, slots=True)
class AttendanceResult:
    success: bool
    message: str
    scanned_at: str | None = None


@dataclass(frozen=True, slots=True)
class AccountProfile:
    name: str
    student_id: str | None = None
    person_type: str | None = None
