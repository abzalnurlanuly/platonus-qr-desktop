"""Client for university Platonus mobile APIs."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address, ip_network
import json
import re
import socket
from threading import RLock
from time import monotonic, sleep
from urllib.parse import quote, quote_plus, urlsplit

import httpx

from .diagnostics import DiagnosticLog
from .models import AccountProfile, AttendanceQR, AttendanceResult, Coordinates, PlatonusError


MINIAPP_ORIGIN = "https://m.platonus.kz"
AUTH_PATH = "/rest/api/mobile/authentication"
AUTH_TYPE_PATH = "/rest/registration/mobile/checkSettings/authentication_type"
QR_PATH = "/rest/mobile/qr-code"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_RESPONSE_BYTES = 8 * 1024
MAX_DIAGNOSTIC_TEXT = 320
MAX_RESPONSE_SECONDS = 15.0
TRANSIENT_STATUSES = frozenset({408, 500, 502, 503, 504})
PROFILE_LANGUAGES = {1: "ru", 2: "kz", 3: "en"}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH_PART = re.compile(r"[A-Za-z0-9_~.-]{1,80}")
_UI_PATH = re.compile(r"v[0-9]{1,3}|auth|login|index\.html?|index\.jsp|login\.jsp", re.IGNORECASE)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa")
_IPV6_TRANSITION = tuple(ip_network(prefix) for prefix in ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_EMBEDDED_OBJECT = re.compile(r"\{[^{}]*\}")
_BEARER = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_SECRET_FIELD = re.compile(
    r"""\b(password|passphrase|pwd|authorization|cookie|token|auth_token|access_token|refresh_token|session_id|"""
    r"""qr(?:[_ -]?code)?|latitude|longitude|lat|lon|lng|coordinates|username|login|email|studentID|personID|"""
    r"""studentName|fullName|tutorName)\b"""
    r"""["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
    re.IGNORECASE,
)
_COORDINATE = re.compile(r"(?<!\w)-?\d{1,3}\.\d{3,}(?!\w)")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_IDENTIFIER = re.compile(r"\b\d{10,16}\b")
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_SECRET = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
_ENCODED_BYTE = re.compile(r"%[0-9A-F]{2}")
_PRIVATE_RESPONSE_FIELDS = (
    "auth_token", "access_token", "refresh_token", "session_id", "password", "login", "username",
    "studentID", "personID", "name", "fullName", "studentName", "tutor", "tutorName", "email",
)


def _invalid_university_url() -> PlatonusError:
    return PlatonusError("invalid_university_url", "Enter the university's public HTTPS Platonus address.")


def normalize_university_url(raw: str) -> str:
    """Turn a portal address into a public HTTPS API base URL."""
    if not isinstance(raw, str):
        raise _invalid_university_url()
    raw = raw.strip()
    if not raw or len(raw) > 2048 or "\\" in raw or any(not c.isprintable() or c.isspace() for c in raw):
        raise _invalid_university_url()
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").removesuffix(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise _invalid_university_url() from None
    if (
        parts.scheme != "https" or "@" in parts.netloc or "%" in parts.netloc or "[" in parts.netloc
        or port not in (None, 443) or (":" in parts.netloc and not parts.netloc.endswith(":443"))
        or len(host) > 253 or "." not in host or host.endswith(_LOCAL_SUFFIXES)
    ):
        raise _invalid_university_url()
    labels = host.split(".")
    if any(_DNS_LABEL.fullmatch(label) is None for label in labels) or not any(c.isalpha() for c in labels[-1]):
        raise _invalid_university_url()
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid_university_url()

    path = parts.path.strip("/")
    segments = path.split("/") if path else []
    if "//" in parts.path or any(
        segment in (".", "..") or _PATH_PART.fullmatch(segment) is None for segment in segments
    ):
        raise _invalid_university_url()
    for index, segment in enumerate(segments):
        if _UI_PATH.fullmatch(segment):
            segments = segments[:index]
            break
    return "https://" + host + ("/" + "/".join(segments) if segments else "")


def _public_addresses(host: str) -> tuple[str, ...]:
    try:
        addresses = {
            ip_address(item[4][0])
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        raise httpx.ConnectError("Could not resolve the university host.") from None
    except ValueError:
        raise _invalid_university_url() from None
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        or (address.version == 6 and (
            address.ipv4_mapped is not None or address.scope_id is not None
            or any(address in network for network in _IPV6_TRANSITION)
        ))
        for address in addresses
    ):
        raise _invalid_university_url()
    return tuple(str(address) for address in sorted(addresses, key=lambda address: (address.version, int(address))))


class _PublicHTTPSTransport(httpx.BaseTransport):
    def __init__(self, host: str, limits: httpx.Limits) -> None:
        self._host = host
        self._addresses: tuple[str, ...] = ()
        self._transport = httpx.HTTPTransport(limits=limits, trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != self._host or request.url.port not in (None, 443):
            raise _invalid_university_url()
        if not self._addresses:
            self._addresses = _public_addresses(self._host)
        timeouts = request.extensions.get("timeout", {})
        connect_timeout = timeouts.get("connect") or 5.0
        deadline = monotonic() + connect_timeout
        error = None
        for address in self._addresses:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            # Pin DNS while keeping the portal hostname for Host and TLS.
            target = httpx.Request(
                request.method, request.url.copy_with(host=address), headers=request.headers,
                stream=request.stream,
                extensions={
                    **request.extensions, "sni_hostname": self._host,
                    "timeout": {**timeouts, "connect": remaining},
                },
            )
            try:
                response = self._transport.handle_request(target)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                error = exc
            else:
                if address != self._addresses[0]:
                    self._addresses = (address, *(other for other in self._addresses if other != address))
                return response
        if error is not None:
            raise error
        raise httpx.ConnectTimeout("University connection timed out.")

    def close(self) -> None:
        self._transport.close()


def _token(value: object) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and value not in {"undefined", "null"}
        and all("!" <= char <= "~" for char in value)
    ):
        return value
    return None


def _boolean(value: object) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return {"false": False, "true": True, "0": False, "1": True}.get(value.strip().lower())
    return None


def _scanned_at(value: object) -> str | None:
    if not isinstance(value, str) or not 8 <= len(value) <= 40:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _profile_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    value = " ".join(value.split())
    return value if value and value.isprintable() else None


def _diagnostic_text(value: object, secrets: set[str]) -> str | None:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value[0] in "{[<":
        return None
    if len(value) > MAX_DIAGNOSTIC_RESPONSE_BYTES:
        return "[redacted long detail]"
    variants = set()
    for secret in secrets:
        if not secret or len(secret) > len(value):
            continue
        variants.add(secret)
        try:
            for encoded in (quote(secret, safe=""), quote_plus(secret, safe="")):
                variants.add(encoded)
                variants.add(_ENCODED_BYTE.sub(lambda match: match[0].lower(), encoded))
        except UnicodeError:
            pass
    if variants:
        marker = next(
            (chr(code) for code in range(32) if all(chr(code) not in variant for variant in variants)),
            None,
        )
        if marker is None:
            return "[redacted detail]"
        for variant in sorted(variants, key=len, reverse=True):
            value = value.replace(variant, marker)
        value = value.replace(marker, "[redacted]")
    value = _URL.sub("[redacted URL]", value)
    value = _EMBEDDED_OBJECT.sub("[redacted object]", value)
    value = _BEARER.sub("Bearer [redacted]", value)
    value = _SECRET_FIELD.sub(lambda match: match[1] + "=[redacted]", value)
    value = _COORDINATE.sub("[redacted]", value)
    value = _EMAIL.sub("[redacted]", value)
    value = _IDENTIFIER.sub("[redacted]", value)
    value = _JWT.sub("[redacted]", value)
    value = _LONG_SECRET.sub("[redacted]", value)
    value = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return value[:MAX_DIAGNOSTIC_TEXT] or None


def _validate_qr(qr: AttendanceQR) -> None:
    if (
        not isinstance(qr, AttendanceQR)
        or not isinstance(qr.raw, str)
        or qr.action not in ("journal", "event")
        or not isinstance(qr.code, str)
        or not 0 < len(qr.code) <= 4096
        or not qr.code.isprintable()
        or not qr.code.strip()
    ):
        raise PlatonusError("invalid_qr", "The QR payload is not a supported attendance code.")


def _response_data(response: httpx.Response, deadline: float, limit: int) -> dict:
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise ValueError("Unexpected response encoding.")
    content_length = response.headers.get("content-length")
    if content_length and int(content_length) > limit:
        raise ValueError("The response exceeds the size limit.")

    if response.is_stream_consumed:
        payload = response.content
        if len(payload) > limit:
            raise ValueError("The response exceeds the size limit.")
    else:
        payload = bytearray()
        for chunk in response.iter_raw():
            if monotonic() > deadline:
                raise httpx.ReadTimeout("Response deadline exceeded.")
            if len(payload) + len(chunk) > limit:
                raise ValueError("The response exceeds the size limit.")
            payload.extend(chunk)
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def _http_error(status: int, *, submission: bool = False) -> PlatonusError:
    if status == 429:
        return PlatonusError(
            "rate_limited", "Platonus is limiting requests. Wait before trying again.",
            retryable=True, upstream_status=status,
        )
    if submission and (300 <= status < 400 or status == 408 or status >= 500):
        return PlatonusError(
            "submission_unknown", "The submission outcome is unknown. Check the journal before trying again.",
            upstream_status=status,
        )
    if status == 408 or (status >= 500 and status != 501):
        return PlatonusError(
            "upstream_unavailable", "Platonus is temporarily unavailable.",
            retryable=True, upstream_status=status,
        )
    if status in {404, 405, 410, 415, 501} or 300 <= status < 400:
        return PlatonusError(
            "api_changed", "The mobile API endpoint or request format may have changed.",
            upstream_status=status,
        )
    return PlatonusError(
        "upstream_rejected", f"Platonus rejected the request (HTTP {status}).",
        upstream_status=status,
    )


class PlatonusClient:
    """One in-memory university session with bounded upstream connections.

    Methods are synchronous. A bot can call them in a worker thread. Use one
    client per signed-in account and close it when that session is no longer needed.
    """

    def __init__(
        self, base_url: str, *, language: int = 1, transport: httpx.BaseTransport | None = None,
        diagnostics: DiagnosticLog | None = None,
    ) -> None:
        if type(language) is not int or language not in (1, 2, 3):
            raise ValueError("Language must be 1 (Russian), 2 (Kazakh), or 3 (English).")
        self.base_url = normalize_university_url(base_url)
        self._language = language
        self._auth_type: int | None = None
        self._diagnostics = diagnostics
        self._lock = RLock()
        self._closed = False
        self._access: str | None = None
        self._refresh: str | None = None
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=30.0)
        if transport is None:
            transport = _PublicHTTPSTransport(httpx.URL(self.base_url).host, limits)
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Origin": MINIAPP_ORIGIN,
                "Referer": MINIAPP_ORIGIN + "/",
            },
            timeout=httpx.Timeout(8.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> PlatonusClient:
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._clear_session()
                self._http.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise PlatonusError("client_closed", "The Platonus session is closed.")

    def _ensure_session(self) -> None:
        self._ensure_open()
        if not self._access:
            raise PlatonusError("not_authenticated", "Sign in to Platonus first.")

    def _clear_session(self) -> None:
        self._access = None
        self._refresh = None
        self._http.cookies.clear()

    def _record(self, event: str, **fields: str | int | bool | None) -> None:
        if self._diagnostics is not None:
            try:
                self._diagnostics.record(
                    event, **{key: value for key, value in fields.items() if value is not None},
                )
            except Exception:
                # A broken diagnostic sink must not change the result of a write.
                pass

    def _exchange(
        self, operation: str, method: str, path: str, *, submission: bool = False,
        response_limit: int = MAX_RESPONSE_BYTES, **kwargs: object,
    ) -> tuple[int, dict | None]:
        started = monotonic()
        deadline = started + MAX_RESPONSE_SECONDS
        response = None
        status = None
        error = None
        transport_error = None
        diagnostic_body_error = None
        self._record("upstream_request", operation=operation, method=method)
        try:
            with self._http.stream(method, path, **kwargs) as response:
                status = response.status_code
                if not response.is_success:
                    if (
                        self._diagnostics is not None and submission
                        and 400 <= status < 500 and status not in {401, 408}
                    ):
                        try:
                            return status, _response_data(response, deadline, MAX_DIAGNOSTIC_RESPONSE_BYTES)
                        except (httpx.RequestError, httpx.StreamError, ValueError, RecursionError) as exc:
                            diagnostic_body_error = type(exc).__name__
                    return status, None
                return status, _response_data(response, deadline, response_limit)
        except (httpx.RequestError, httpx.StreamError) as exc:
            transport_error = type(exc).__name__
            if submission:
                error = PlatonusError(
                    "submission_unknown",
                    "The submission outcome is unknown. Check the journal before trying again.",
                    upstream_status=status,
                )
            else:
                error = PlatonusError(
                    "upstream_unavailable", "Could not reach the university’s Platonus mobile API.",
                    retryable=True, upstream_status=status,
                )
        except (ValueError, RecursionError):
            if submission:
                error = PlatonusError(
                    "submission_unknown",
                    "Platonus returned an unrecognized result. Check the journal before trying again.",
                    upstream_status=status,
                )
            else:
                error = PlatonusError(
                    "api_changed", "Platonus returned an unrecognized response.", upstream_status=status,
                )
        finally:
            kwargs.clear()
            response = None
            self._http.cookies.clear()
            self._record(
                "upstream_response", operation=operation, method=method, status=status,
                duration_ms=round((monotonic() - started) * 1000),
                error_code=error.code if error else None, transport_error=transport_error,
                diagnostic_body_error=diagnostic_body_error,
            )
        raise error

    def authentication_type(self) -> int:
        """Check the public sign-in settings before sending credentials."""
        with self._lock:
            self._ensure_open()
            if self._auth_type is not None:
                return self._auth_type
            try:
                status, data = self._exchange(
                    "authentication_settings", "GET", AUTH_TYPE_PATH, params={"lang": self._language},
                )
            except PlatonusError as exc:
                if exc.code != "api_changed":
                    raise
                status, data = exc.upstream_status, None
            if status == 429 or status in TRANSIENT_STATUSES:
                raise _http_error(status)
            if data is None or status != 200:
                raise PlatonusError(
                    "unsupported_university", "This address did not expose the expected Platonus sign-in settings.",
                    upstream_status=status,
                )
            value = data.get("authenticationType")
            if (type(value) is not int and not isinstance(value, str)) or str(value) not in {"1", "2", "3", "4"}:
                raise PlatonusError(
                    "unsupported_authentication", "This Platonus sign-in method is not supported.",
                    upstream_status=status,
                )
            self._auth_type = int(value)
            return self._auth_type

    def login(self, username: str, password: str, *, iin: str | None = None) -> None:
        """Start a session without retaining the submitted credentials."""
        with self._lock:
            self._ensure_open()
            self._clear_session()
            data = None
            try:
                if (
                    not isinstance(username, str) or len(username) > 256
                    or not isinstance(password, str) or not 0 < len(password) <= 1024
                    or (iin is not None and (not isinstance(iin, str) or len(iin) > 64))
                ):
                    raise PlatonusError("invalid_credentials", "Enter your Platonus login and password.")
                username = username.strip()
                iin = (iin or "").strip()
                if not username and not iin:
                    raise PlatonusError("invalid_credentials", "Enter your Platonus login and password.")
                method = self.authentication_type()
                if method == 2 and not iin:
                    raise PlatonusError("additional_identity_required", "This university also requires your IIN.")
                if method == 3 or (method == 4 and (iin or re.fullmatch(r"[0-9]{12}", username))):
                    iin, username = iin or username, ""
                if method == 1:
                    iin = ""
                if method in (1, 2) and not username:
                    raise PlatonusError("invalid_credentials", "Enter your Platonus login and password.")
                status, data = self._exchange(
                    "login", "POST", AUTH_PATH + "/login",
                    params={"language": self._language, "lang": self._language},
                    json={"login": username, "iin": iin, "icNumber": username, "password": password},
                )
                if status in {401, 403} or (data and data.get("login_status") == "invalid"):
                    raise PlatonusError(
                        "invalid_credentials", "Platonus did not accept the username or password.",
                        upstream_status=status,
                    )
                if data is None:
                    raise _http_error(status)
                access = _token(data.get("auth_token"))
                if not access:
                    raise PlatonusError(
                        "api_changed", "Platonus did not return a session token. The sign-in flow may have changed.",
                        upstream_status=status,
                    )
                self._access = access
                self._refresh = _token(data.get("refresh_token"))
                self._record("login_result", outcome="authenticated", status=status)
            except PlatonusError as exc:
                self._clear_session()
                self._record("login_result", outcome=exc.code, status=exc.upstream_status)
                raise
            finally:
                username = password = iin = ""
                data = None

    def _expired(self) -> PlatonusError:
        self._clear_session()
        return PlatonusError(
            "session_expired", "Your Platonus session expired. Sign in again.", upstream_status=401,
        )

    def _refresh_session(self) -> None:
        if not self._refresh:
            raise self._expired()
        try:
            status, data = self._exchange(
                "session_refresh", "POST", AUTH_PATH + "/refresh",
                headers={"Authorization": f"Bearer {self._refresh}"},
                json={},
            )
        except PlatonusError:
            status, data = 0, None
        if status == 429:
            raise _http_error(status)
        if data is None:
            raise self._expired()
        access = _token(data.get("auth_token"))
        if not access:
            self._clear_session()
            raise PlatonusError("api_changed", "Platonus did not return a renewed session token.")
        self._access = access
        self._refresh = _token(data.get("refresh_token")) or self._refresh

    def _authorized(
        self, operation: str, method: str, path: str, *, submission: bool = False,
        params: dict | None = None, content: bytes | None = None,
        response_limit: int = MAX_RESPONSE_BYTES,
    ) -> tuple[int, dict | None]:
        self._ensure_session()
        query = {"lang": self._language, **(params or {})}
        headers = {"token": self._access}
        if submission:
            headers["Content-Type"] = "text/plain"
        status, data = self._exchange(
            operation, method, path, submission=submission, response_limit=response_limit,
            params=query, headers=headers, content=content,
        )
        if status == 401:
            self._refresh_session()
            self._record("upstream_retry", operation=operation, reason="session_renewed", status=status)
            headers["token"] = self._access
            status, data = self._exchange(
                operation, method, path, submission=submission, response_limit=response_limit,
                params=query, headers=headers, content=content,
            )
            if status == 401:
                raise self._expired()
        return status, data

    def _read_authorized(
        self, operation: str, path: str, *, response_limit: int = MAX_RESPONSE_BYTES,
    ) -> tuple[int, dict | None]:
        try:
            result = self._authorized(operation, "GET", path, response_limit=response_limit)
        except PlatonusError as exc:
            if exc.code != "upstream_unavailable":
                raise
            self._record(
                "upstream_retry", operation=operation, reason=exc.code, status=exc.upstream_status,
            )
        else:
            if result[0] not in TRANSIENT_STATUSES:
                return result
            self._record("upstream_retry", operation=operation, reason="transient_status", status=result[0])
        sleep(0.2)
        return self._authorized(operation, "GET", path, response_limit=response_limit)

    def get_profile(self) -> AccountProfile:
        """Read the signed-in account name without retaining the full profile."""
        with self._lock:
            language = PROFILE_LANGUAGES[self._language]
            status, data = self._read_authorized(
                "account_profile", f"/rest/mobile/personInfo/{language}", response_limit=MAX_PROFILE_BYTES,
            )
            if data is None:
                raise _http_error(status)
            name = _profile_text(data.get("name"), 256)
            student_id = data.get("studentID")
            if type(student_id) is int and 0 <= student_id < 2**63:
                student_id = str(student_id)
            person_type = _profile_text(data.get("personType"), 64)
            del data
            if name is None:
                raise PlatonusError("api_changed", "Platonus did not return a recognized account name.")
            return AccountProfile(name, _profile_text(student_id, 64), person_type)

    def location_required(self, qr: AttendanceQR) -> bool:
        """Read the location requirement for a flagged journal QR."""
        _validate_qr(qr)
        with self._lock:
            self._ensure_open()
            if qr.action == "event" or not qr.location_flag:
                self._record("location_check", required=False, source="qr_flag")
                return False
            segment = quote(qr.code, safe="")
            if segment in {".", ".."}:
                segment = segment.replace(".", "%2E")
            path = f"{QR_PATH}/locationRequired/{segment}"
            try:
                status, data = self._read_authorized("location_required", path)
            except PlatonusError as exc:
                if exc.code in {"api_changed", "upstream_unavailable"}:
                    self._record("location_check", required=True, source="conservative_fallback")
                    return True
                raise
            if status == 429:
                raise _http_error(status)
            required = _boolean(data.get("locationRequired")) if data is not None else None
            self._record(
                "location_check", required=required is not False,
                source="conservative_fallback" if required is None else "server",
            )
            return required is not False

    def _record_attendance(
        self, operation: str, outcome: str, *, status: int | None, success: bool | None,
        data: dict | None, secrets: set[str],
    ) -> None:
        if self._diagnostics is None:
            return
        details = {}
        if data and success is not True:
            error = data.get("error")
            sources = (data, error) if isinstance(error, dict) else (data,)
            for source in sources:
                for key in _PRIVATE_RESPONSE_FIELDS:
                    value = source.get(key)
                    if isinstance(value, str):
                        secrets.add(value)
                    elif type(value) is int:
                        secrets.add(str(value))
            secrets.update(token for token in (self._access, self._refresh) if token)
            fields = {
                "server_message": ("message", "errorDescription", "error_description", "error"),
                "server_reason": ("reason", "detail"),
                "server_code": ("errorCode", "error_code", "code"),
            }
            for field, keys in fields.items():
                details[field] = next(
                    (
                        text for source in sources for key in keys
                        if (text := _diagnostic_text(source.get(key), secrets)) is not None
                    ),
                    None,
                )
        self._record(
            "attendance_result", operation=operation, outcome=outcome, status=status,
            success=success, **details,
        )

    def submit(self, qr: AttendanceQR, *, coordinates: Coordinates | None = None) -> AttendanceResult:
        """Submit one scanned QR. Ambiguous outcomes require a manual journal check."""
        _validate_qr(qr)
        if coordinates is not None and not isinstance(coordinates, Coordinates):
            raise ValueError("Supply a Coordinates instance for location-verified attendance.")
        with self._lock:
            endpoint = "eventAttendance" if qr.action == "event" else "journalAttendance"
            operation = f"{qr.action}_attendance"
            secrets = set()
            if self._diagnostics is not None:
                secrets.update(value for value in (qr.code, qr.raw, self._access, self._refresh) if value)
            status = data = None
            try:
                self._ensure_session()
                params = {}
                if self.location_required(qr):
                    if coordinates is None:
                        raise PlatonusError("location_required", "This attendance code requires your current location.")
                    params = {"latitude": coordinates.latitude, "longitude": coordinates.longitude}
                    if self._diagnostics is not None:
                        secrets.update(str(value) for value in params.values())
                status, data = self._authorized(
                    operation, "POST", f"{QR_PATH}/{endpoint}", submission=True, params=params,
                    content=qr.code.encode("utf-8"),
                )
                if not 200 <= status < 300:
                    raise _http_error(status, submission=True)
                success = _boolean(data.get("success")) if data is not None else None
                if success is None:
                    raise PlatonusError(
                        "submission_unknown",
                        "Platonus returned an unrecognized result. Check the journal before trying again.",
                        upstream_status=status,
                    )
            except PlatonusError as exc:
                self._record_attendance(
                    operation, exc.code, status=exc.upstream_status,
                    success=False if status is not None and 400 <= status < 500 and status != 408 else None,
                    data=data, secrets=secrets,
                )
                raise
            self._record_attendance(
                operation, "confirmed" if success else "rejected", status=status, success=success,
                data=data, secrets=secrets,
            )
            message = (
                "Platonus confirmed attendance."
                if success else "Platonus did not confirm attendance. Check the code and the official journal."
            )
            return AttendanceResult(success, message, _scanned_at(data.get("scan_date")))
