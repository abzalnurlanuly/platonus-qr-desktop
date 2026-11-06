"""Loopback web UI and API."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hmac import compare_digest
from importlib.resources import files
from io import BytesIO
import re
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING

from flask import Flask, Request, Response, g, jsonify, request
from werkzeug.exceptions import BadRequest, HTTPException, RequestEntityTooLarge

from .client import PlatonusClient
from .models import Coordinates, PlatonusError
from .qr import MAX_UPLOAD_BYTES, decode_image, parse_qr
from .service import AttendanceService

if TYPE_CHECKING:
    from .diagnostics import DiagnosticLog, Scalar


MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
MAX_JSON_BYTES = 16 * 1024

_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' https://tile.openstreetmap.org; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "strings.js": "text/javascript; charset=utf-8",
    "map.js": "text/javascript; charset=utf-8",
    "mark.svg": "image/svg+xml",
}
_INPUT_ERRORS = {
    "additional_identity_required", "ambiguous_qr", "invalid_coordinates", "invalid_credentials",
    "invalid_image", "invalid_qr", "invalid_university_url", "location_required",
    "unsupported_authentication", "unsupported_university",
}
_SESSION_ERRORS = {"not_authenticated", "session_expired"}
_OPERATIONS = {
    "decode": "decode", "attendance": "submission", "login": "login", "location": "location", "logout": "logout",
}


@dataclass(frozen=True, slots=True)
class _Session:
    client: PlatonusClient
    attendance: AttendanceService
    display_name: str | None


class _MemoryRequest(Request):
    def _get_file_stream(
        self,
        total_content_length: int | None,
        content_type: str | None,
        filename: str | None = None,
        content_length: int | None = None,
    ) -> BytesIO:
        if content_length is not None and content_length > MAX_UPLOAD_BYTES:
            raise RequestEntityTooLarge()
        return BytesIO()


def _error(
    code: str,
    message: str,
    status: int,
    *,
    retryable: bool = False,
    upstream_status: int | None = None,
) -> tuple[Response, int]:
    error = {"code": code, "message": message, "retryable": retryable}
    if upstream_status is not None:
        error["upstream_status"] = upstream_status
    return jsonify(error=error), status


def _error_status(error: PlatonusError) -> int:
    if error.code == "image_too_large":
        return 413
    if error.code in _INPUT_ERRORS:
        return 422
    if error.code in _SESSION_ERRORS or error.code in {"already_authenticated", "submission_unknown"}:
        return 409
    if error.code == "rate_limited" or error.upstream_status == 429:
        return 429
    return 503 if error.retryable else 502


def _json_fields(required: set[str], optional: set[str] | None = None) -> dict | tuple[Response, int]:
    if not request.is_json:
        return _error("unsupported_media_type", "Send a JSON object.", 415)
    request.max_content_length = MAX_JSON_BYTES
    try:
        body = request.get_json(cache=False)
    except (BadRequest, RecursionError):
        return _error("invalid_json", "Send a valid JSON object.", 400)
    if not isinstance(body, dict) or not required <= body.keys() <= required | (optional or set()):
        return _error("invalid_json", "The JSON object has missing or unexpected fields.", 400)
    return body


def _coordinates(value: object) -> Coordinates:
    if isinstance(value, dict) and value.keys() == {"latitude", "longitude"}:
        try:
            return Coordinates(value["latitude"], value["longitude"])
        except ValueError:
            pass
    raise PlatonusError("invalid_coordinates", "Enter a valid latitude and longitude.")


def create_app(
    *,
    access_token: str,
    host: str,
    port: int,
    diagnostics: DiagnosticLog | None = None,
    language: str = "en",
    client_factory: Callable[..., PlatonusClient] = PlatonusClient,
) -> Flask:
    """Build the app. Set LOCAL_PORT after binding when using port zero."""
    if host != "127.0.0.1" or not 0 <= port <= 65535:
        raise ValueError("The web app requires IPv4 loopback and a valid local port.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", access_token):
        raise ValueError("The local API requires a strong, URL-safe access token.")
    if language not in ("en", "ru"):
        raise ValueError("The host language must be en or ru.")

    expected_auth = f"Bearer {access_token}".encode("ascii")
    static = files(__package__).joinpath("static")
    home_html = static.joinpath("index.html").read_bytes().replace(b"{{HOST_LANGUAGE}}", language.encode("ascii"))
    assets = {
        name: (static.joinpath(name).read_bytes(), content_type)
        for name, content_type in _ASSETS.items()
        if static.joinpath(name).is_file()
    }
    decoder_slot = BoundedSemaphore(1)
    session_lock = Lock()
    session: _Session | None = None

    app = Flask(__name__, static_folder=None)
    app.request_class = _MemoryRequest
    app.config.update(
        LOCAL_PORT=port,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=1,
    )

    def record(event: str, **fields: Scalar) -> None:
        if diagnostics is not None:
            try:
                diagnostics.record(event, **fields)
            except Exception:
                app.logger.error("Could not record a local diagnostic.")

    def end_session() -> None:
        nonlocal session
        previous, session = session, None
        if previous is not None:
            previous.client.close()

    def shutdown() -> None:
        with session_lock:
            end_session()
            if diagnostics is not None:
                diagnostics.clear()

    app.extensions["platonus_close"] = shutdown

    @contextmanager
    def exclusive_session() -> Iterator[None]:
        if not session_lock.acquire(blocking=False):
            raise PlatonusError("busy", "Another session operation is in progress.", retryable=True)
        try:
            yield
        finally:
            session_lock.release()

    @contextmanager
    def signed_in() -> Iterator[_Session]:
        with exclusive_session():
            if session is None:
                raise PlatonusError("not_authenticated", "Sign in to your university first.")
            try:
                yield session
            except PlatonusError as error:
                if error.code in _SESSION_ERRORS:
                    end_session()
                raise

    def account_response() -> Response:
        current = session
        return jsonify(authenticated=current is not None, display_name=current.display_name if current else None)

    @app.before_request
    def check_request() -> tuple[Response, int] | None:
        local_port = app.config["LOCAL_PORT"]
        local_host = f"{host}:{local_port}"
        local_origin = f"http://{host}" if local_port == 80 else f"http://{local_host}"
        allowed_host = (host, local_host) if local_port == 80 else (local_host,)
        if not local_port or request.remote_addr != host or request.environ.get("HTTP_HOST") not in allowed_host:
            return _error("invalid_host", "Use the local URL printed in the terminal.", 403)
        if not request.path.startswith("/api/"):
            return None

        origin = request.headers.get("Origin")
        if (origin is not None and origin != local_origin) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return _error("invalid_origin", "Cross-origin requests are blocked.", 403)
        supplied_auth = request.headers.get("Authorization", "").encode("utf-8")
        if not compare_digest(supplied_auth, expected_auth):
            return _error("unauthorized", "Use the current local access token.", 401)
        g.local_api_authenticated = True
        return None

    @app.after_request
    def secure_response(response: Response) -> Response:
        operation = _OPERATIONS.get(request.endpoint)
        if operation and g.get("local_api_authenticated") and response.status_code >= 400:
            payload = response.get_json(silent=True) or {}
            error = payload.get("error", {})
            camera_miss = (
                operation == "decode" and request.headers.get("X-Scan-Mode") == "camera"
                and error.get("code") == "invalid_qr"
            )
            if not camera_miss:
                record(
                    "request_failed", operation=operation, http_status=response.status_code,
                    error_code=error.get("code", "http_error"), retryable=error.get("retryable", False),
                    upstream_status=error.get("upstream_status"),
                )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self), microphone=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(PlatonusError)
    def handle_expected(error: PlatonusError) -> tuple[Response, int]:
        return _error(
            error.code, str(error), _error_status(error),
            retryable=error.retryable, upstream_status=error.upstream_status,
        )

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_: RequestEntityTooLarge) -> tuple[Response, int]:
        return _error("request_too_large", "The request exceeds the size or form limit.", 413)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response, int]:
        return _error("http_error", error.name, error.code or 500)

    @app.errorhandler(Exception)
    def handle_unexpected(_: Exception) -> tuple[Response, int]:
        app.logger.error("An unexpected localhost request error occurred.")
        return _error("internal_error", "The local server could not complete the request.", 500)

    @app.get("/")
    def index() -> Response:
        return Response(home_html, content_type="text/html; charset=utf-8")

    @app.get("/assets/<asset_name>")
    def asset(asset_name: str) -> Response | tuple[Response, int]:
        if asset_name not in assets:
            return _error("not_found", "Asset not found.", 404)
        body, content_type = assets[asset_name]
        return Response(body, content_type=content_type)

    @app.get("/health")
    def health() -> Response:
        return jsonify(status="ok")

    @app.get("/api/v1/account")
    def account() -> Response:
        return account_response()

    @app.post("/api/v1/login")
    def login() -> Response | tuple[Response, int]:
        nonlocal session
        body = _json_fields({"university_url", "username", "password", "language"}, {"iin"})
        if isinstance(body, tuple):
            return body

        try:
            limits = (("university_url", 2048), ("username", 254), ("password", 1024))
            if not all(isinstance(body[field], str) and 0 < len(body[field]) <= limit for field, limit in limits):
                return _error("invalid_json", "Enter a university URL, username and password.", 400)
            iin = body.get("iin")
            if "iin" in body and (not isinstance(iin, str) or len(iin) > 32):
                return _error("invalid_json", "Enter a valid identity number.", 400)
            if body["language"] not in ("en", "ru"):
                return _error("invalid_json", "Choose English or Russian.", 400)

            with exclusive_session():
                if session is not None:
                    raise PlatonusError("already_authenticated", "Sign out before switching accounts.")
                if diagnostics is not None:
                    diagnostics.clear()
                record("login_started", operation="login")
                candidate = client_factory(
                    body["university_url"], language=1 if body["language"] == "ru" else 3, diagnostics=diagnostics,
                )
                try:
                    credentials = {"iin": iin} if iin else {}
                    candidate.login(body.pop("username"), body.pop("password"), **credentials)
                    try:
                        display_name = candidate.get_profile().name
                    except PlatonusError as error:
                        if error.code in _SESSION_ERRORS:
                            raise
                        display_name = None
                    session = _Session(candidate, AttendanceService(candidate, coordinates=None), display_name)
                finally:
                    if session is None:
                        candidate.close()
                record("login_finished", operation="login", success=True)
                return account_response()
        finally:
            body.clear()

    @app.post("/api/v1/logout")
    def logout() -> Response:
        with exclusive_session():
            end_session()
            if diagnostics is not None:
                diagnostics.clear()
            return account_response()

    @app.get("/api/v1/logs")
    def logs() -> Response:
        return jsonify(entries=diagnostics.snapshot() if diagnostics is not None else [])

    @app.post("/api/v1/decode")
    def decode() -> Response | tuple[Response, int]:
        if request.mimetype != "multipart/form-data":
            return _error("unsupported_media_type", "Send a multipart image named image.", 415)
        if not decoder_slot.acquire(blocking=False):
            return _error("busy", "Another image is being decoded. Try again shortly.", 503, retryable=True)
        try:
            upload = request.files.get("image")
            if upload is None:
                return _error("missing_image", "Choose an image to decode.", 400)
            try:
                data = upload.stream.read(MAX_UPLOAD_BYTES + 1)
            finally:
                upload.close()
            if len(data) > MAX_UPLOAD_BYTES:
                return _error("image_too_large", "Choose an image no larger than 8 MiB.", 413)
            qr = decode_image(data)
            record("decode_finished", operation="decode", action=qr.action, location_flag=qr.location_flag)
            return jsonify(raw=qr.raw, action=qr.action, location_flag=qr.location_flag)
        finally:
            decoder_slot.release()

    @app.post("/api/v1/location")
    def location() -> Response | tuple[Response, int]:
        body = _json_fields({"qr"})
        if isinstance(body, tuple):
            return body
        qr = parse_qr(body["qr"])
        with signed_in() as current:
            return jsonify(required=current.client.location_required(qr))

    @app.post("/api/v1/attendance")
    def attendance() -> Response | tuple[Response, int]:
        body = _json_fields({"qr"}, {"coordinates"})
        if isinstance(body, tuple):
            return body
        qr = parse_qr(body["qr"])
        coordinates = _coordinates(body["coordinates"]) if "coordinates" in body else None
        with signed_in() as current:
            record("submission_started", operation="submission", action=qr.action, location_flag=qr.location_flag)
            result = current.attendance.submit(qr, coordinates=coordinates)
            record("submission_finished", operation="submission", action=qr.action, success=result.success)
            return jsonify(success=result.success, message=result.message, scanned_at=result.scanned_at)

    return app
