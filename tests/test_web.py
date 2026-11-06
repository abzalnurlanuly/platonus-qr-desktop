from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image
import zxingcpp

from platonus_desktop.diagnostics import DiagnosticLog
from platonus_desktop.models import AccountProfile, AttendanceResult, Coordinates, PlatonusError
from platonus_desktop.qr import parse_qr
from platonus_desktop.web import MAX_JSON_BYTES, create_app


HOST = "127.0.0.1"
PORT = 8123
BASE_URL = f"http://{HOST}:{PORT}"
TOKEN = "example_local_key_used_by_tests_only"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
RAW = '{"a":"j","c":"demo-session","l":0}'
LOCATION_RAW = '{"a":"j","c":"location-demo","l":1}'
LOGIN = {
    "university_url": "https://portal.example.edu",
    "username": "demo-user",
    "password": "demo-password",
    "language": "en",
}


class ClientStub:
    def __init__(self, url, *, language, diagnostics):
        self.url = url
        self.language = language
        self.closed = False
        self.calls = []
        self.lookup = []
        self.login_options = {}

    def login(self, username, password, **options):
        if (username, password) != ("demo-user", "demo-password"):
            raise PlatonusError("invalid_credentials", "Sign-in failed.")
        self.login_options = options

    def get_profile(self):
        return AccountProfile("Demo Student")

    def location_required(self, qr):
        self.lookup.append(qr)
        return qr.location_flag

    def submit(self, qr, *, coordinates=None):
        if qr.location_flag and coordinates is None:
            raise PlatonusError("location_required", "Share your location first.")
        self.calls.append((qr, coordinates))
        return AttendanceResult(True, "Attendance confirmed.", "15:00")

    def close(self):
        self.closed = True


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = []
        self.diagnostics = DiagnosticLog()

        def factory(*args, **kwargs):
            client = ClientStub(*args, **kwargs)
            self.sessions.append(client)
            return client

        self.app = create_app(
            access_token=TOKEN, host=HOST, port=PORT, client_factory=factory, diagnostics=self.diagnostics,
        )
        self.addCleanup(self.app.extensions["platonus_close"])
        self.client = self.app.test_client()

    def post(self, route: str, **kwargs):
        return self.client.post(route, base_url=BASE_URL, headers=HEADERS, **kwargs)

    def get(self, route: str):
        return self.client.get(route, base_url=BASE_URL, headers=HEADERS)

    def sign_in(self) -> ClientStub:
        response = self.post("/api/v1/login", json=LOGIN)
        self.assertEqual(response.status_code, 200, response.get_json())
        return self.sessions[-1]

    def test_upload_is_local_until_explicit_submission(self) -> None:
        barcode = zxingcpp.create_barcode(RAW, zxingcpp.BarcodeFormat.QRCode)
        pixels = zxingcpp.write_barcode_to_image(barcode, scale=5)
        with Image.frombytes("L", tuple(reversed(pixels.shape)), bytes(pixels)) as image, BytesIO() as png:
            image.save(png, format="PNG")
            png.seek(0)
            response = self.post("/api/v1/decode", data={"image": (png, "qr.png")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"raw": RAW, "action": "journal", "location_flag": False})
        self.assertFalse(self.sessions)

        upstream = self.sign_in()
        self.assertFalse(upstream.calls)
        submitted = self.post("/api/v1/attendance", json={"qr": response.get_json()["raw"]})
        self.assertEqual(submitted.get_json(), {"success": True, "message": "Attendance confirmed.", "scanned_at": "15:00"})
        self.assertEqual([(qr.code, coord) for qr, coord in upstream.calls], [("demo-session", None)])

    def test_host_origin_and_token_validation(self) -> None:
        response = self.client.get("/health", base_url=f"http://evil.invalid:{PORT}")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/health", base_url=BASE_URL, environ_overrides={"REMOTE_ADDR": "10.1.2.3"})
        self.assertEqual(response.status_code, 403)
        denied = self.client.post("/api/v1/login", base_url=BASE_URL, json=LOGIN)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["error"]["code"], "unauthorized")
        for extra_header in ({"Origin": "null"}, {"Origin": "https://evil.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            response = self.client.post(
                "/api/v1/login", base_url=BASE_URL, json=LOGIN, headers=HEADERS | extra_header,
            )
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.sessions)
        response = self.client.post(
            "/api/v1/login", base_url=BASE_URL, json=LOGIN, headers=HEADERS | {"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200)

    def test_security_headers_and_static_assets(self) -> None:
        response = self.client.get("/", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertIn("https://tile.openstreetmap.org", policy)
        self.assertEqual(response.headers["Permissions-Policy"], "camera=(self), geolocation=(self), microphone=()")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.assertNotIn("{{HOST_LANGUAGE}}", response.get_data(as_text=True))
        script = self.client.get("/assets/app.js", base_url=BASE_URL)
        self.assertEqual(script.status_code, 200)
        self.assertIn("history.replaceState", script.get_data(as_text=True))
        self.assertEqual(self.client.get("/assets/index.html", base_url=BASE_URL).status_code, 404)

    def test_request_bounds_and_invalid_input(self) -> None:
        response = self.post("/api/v1/attendance", json={"qr": "q" * MAX_JSON_BYTES})
        self.assertEqual(response.status_code, 413)
        response = self.post("/api/v1/attendance", json={"qr": RAW, "latitude": 1})
        self.assertEqual(response.status_code, 400)
        response = self.post("/api/v1/attendance", json={"qr": "invalid"})
        self.assertEqual(response.status_code, 422)
        response = self.post("/api/v1/attendance", data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.post("/api/v1/login", json=LOGIN | {"password": "x" * 2048})
        self.assertEqual(response.status_code, 400)
        response = self.post(
            "/api/v1/decode", data={"image": (BytesIO(b"invalid"), "qr.png"), "unneeded": "field"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.sessions)

    def test_signed_out_routes_and_dynamic_port(self) -> None:
        app = create_app(access_token=TOKEN, host=HOST, port=0)
        client = app.test_client()
        self.assertEqual(client.get("/health", base_url=BASE_URL).status_code, 403)
        app.config["LOCAL_PORT"] = PORT
        self.assertEqual(client.get("/health", base_url=BASE_URL).get_json(), {"status": "ok"})
        for route in ("/api/v1/attendance", "/api/v1/location"):
            response = client.post(route, base_url=BASE_URL, headers=HEADERS, json={"qr": RAW})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["error"]["code"], "not_authenticated")
        self.assertEqual(client.get("/api/v1/account", base_url=BASE_URL, headers=HEADERS).get_json(), {
            "authenticated": False, "display_name": None,
        })

    def test_login_logout_and_account_privacy(self) -> None:
        upstream = self.sign_in()
        self.assertEqual(upstream.url, LOGIN["university_url"])
        self.assertEqual(upstream.language, 3)
        public = self.client.get("/api/v1/account", base_url=BASE_URL)
        self.assertEqual(public.status_code, 401)
        for path in ("/", "/health", "/assets/app.js", "/api/v1/account"):
            self.assertNotIn("Demo Student", self.client.get(path, base_url=BASE_URL).get_data(as_text=True))
        account = self.get("/api/v1/account")
        self.assertEqual(account.get_json(), {"authenticated": True, "display_name": "Demo Student"})
        self.assertEqual(account.headers["Cache-Control"], "no-store")

        again = self.post("/api/v1/login", json=LOGIN)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()["error"]["code"], "already_authenticated")
        self.assertFalse(upstream.closed)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.post("/api/v1/logout").get_json(), {"authenticated": False, "display_name": None})
        self.assertTrue(upstream.closed)
        self.assertEqual(self.get("/api/v1/logs").get_json(), {"entries": []})

    def test_bad_credentials_close_client_and_allow_retry(self) -> None:
        denied = self.post("/api/v1/login", json=LOGIN | {"password": "wrong"})
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(denied.get_json()["error"]["code"], "invalid_credentials")
        self.assertTrue(self.sessions[0].closed)
        response = self.post("/api/v1/login", json=LOGIN | {"language": "ru", "iin": "000000000000"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions[-1].language, 1)
        self.assertEqual(self.sessions[-1].login_options, {"iin": "000000000000"})

    def test_session_changes_are_rejected_during_sign_in(self) -> None:
        entering = Event()
        release = Event()

        def hold_login(client, username, password):
            entering.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release sign-in.")

        def sign_in():
            return self.app.test_client().post("/api/v1/login", base_url=BASE_URL, headers=HEADERS, json=LOGIN)

        with patch.object(ClientStub, "login", autospec=True, side_effect=hold_login), ThreadPoolExecutor(1) as pool:
            first = pool.submit(sign_in)
            try:
                self.assertTrue(entering.wait(5))
                blocked = self.post("/api/v1/logout")
                self.assertEqual(blocked.status_code, 503)
                self.assertEqual(blocked.get_json()["error"]["code"], "busy")
                self.assertEqual(len(self.sessions), 1)
                self.assertFalse(self.get("/api/v1/account").get_json()["authenticated"])
            finally:
                release.set()
            self.assertEqual(first.result().status_code, 200)
        self.assertTrue(self.get("/api/v1/account").get_json()["authenticated"])

    def test_location_probe_does_not_submit_and_validates_coordinates(self) -> None:
        upstream = self.sign_in()
        needed = self.post("/api/v1/location", json={"qr": LOCATION_RAW})
        self.assertEqual(needed.get_json(), {"required": True})
        self.assertFalse(upstream.calls)
        self.assertEqual(upstream.lookup, [parse_qr(LOCATION_RAW)])
        missing = self.post("/api/v1/attendance", json={"qr": LOCATION_RAW})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.get_json()["error"]["code"], "location_required")
        for invalid in (None, {"latitude": 91, "longitude": 0}, {"latitude": False, "longitude": 20}):
            result = self.post("/api/v1/attendance", json={"qr": LOCATION_RAW, "coordinates": invalid})
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.get_json()["error"]["code"], "invalid_coordinates")
        response = self.post("/api/v1/attendance", json={
            "qr": LOCATION_RAW, "coordinates": {"latitude": 12.5, "longitude": -23.75},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.calls, [(parse_qr(LOCATION_RAW), Coordinates(12.5, -23.75))])

    def test_expired_session_is_closed_and_university_error_does_not_revoke_bearer(self) -> None:
        upstream = self.sign_in()
        with patch.object(upstream, "submit", side_effect=PlatonusError("session_expired", "Expired.")):
            response = self.post("/api/v1/attendance", json={"qr": RAW})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "session_expired")
        self.assertTrue(upstream.closed)
        self.assertEqual(self.get("/api/v1/account").get_json(), {"authenticated": False, "display_name": None})

    def test_diagnostics_record_metadata_and_omit_camera_misses(self) -> None:
        upstream = self.sign_in()
        self.assertEqual(self.client.get("/api/v1/logs", base_url=BASE_URL).status_code, 401)
        with patch("platonus_desktop.web.decode_image", side_effect=PlatonusError("invalid_qr", "No match.")):
            missed = self.client.post(
                "/api/v1/decode", base_url=BASE_URL, headers=HEADERS | {"X-Scan-Mode": "camera"},
                data={"image": (BytesIO(b"private-image"), "private-name.png")},
            )
        self.assertEqual(missed.status_code, 422)
        with patch("platonus_desktop.web.decode_image", return_value=parse_qr(RAW)):
            decoded = self.post("/api/v1/decode", data={"image": (BytesIO(b"private-image"), "private-name.png")})
        self.assertEqual(decoded.status_code, 200)
        error = PlatonusError("submission_unknown", "Upstream reflected private-value.", upstream_status=502)
        with patch.object(upstream, "submit", side_effect=error):
            result = self.post("/api/v1/attendance", json={"qr": RAW})
        self.assertEqual(result.status_code, 409)
        logs = self.get("/api/v1/logs")
        self.assertEqual(logs.headers["Cache-Control"], "no-store")
        entries = logs.get_json()["entries"]
        self.assertEqual([entry["event"] for entry in entries], [
            "login_started", "login_finished", "decode_finished", "submission_started", "request_failed",
        ])
        self.assertEqual(entries[-1]["error_code"], "submission_unknown")
        self.assertEqual(entries[-1]["http_status"], 409)
        self.assertEqual(entries[-1]["upstream_status"], 502)
        self.assertTrue(entries[-1]["timestamp"].endswith("Z"))
        for sensitive in (
            "private-image", "private-name", "private-value", "Demo Student", TOKEN, "demo-session",
            LOGIN["university_url"], LOGIN["username"], LOGIN["password"],
        ):
            self.assertNotIn(sensitive, logs.get_data(as_text=True))

    def test_uploads_have_a_single_decoder_slot(self) -> None:
        decoding = Event()
        release = Event()

        def hold_decoder(_):
            decoding.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the decoder")
            return parse_qr(RAW)

        def upload():
            return self.app.test_client().post(
                "/api/v1/decode", base_url=BASE_URL, headers=HEADERS,
                data={"image": (BytesIO(b"test"), "qr.png")},
            )

        with patch("platonus_desktop.web.decode_image", side_effect=hold_decoder), ThreadPoolExecutor(1) as pool:
            first = pool.submit(upload)
            try:
                self.assertTrue(decoding.wait(5))
                response = upload()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()["error"]["code"], "busy")
            finally:
                release.set()
            self.assertEqual(first.result().status_code, 200)
            self.assertEqual(upload().status_code, 200)

    def test_upstream_errors_are_structured(self) -> None:
        upstream = self.sign_in()
        error = PlatonusError("rate_limited", "Wait before retrying.", retryable=True, upstream_status=429)
        with patch.object(upstream, "submit", side_effect=error):
            response = self.post("/api/v1/attendance", json={"qr": RAW})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"], {
            "code": "rate_limited",
            "message": "Wait before retrying.",
            "retryable": True,
            "upstream_status": 429,
        })
