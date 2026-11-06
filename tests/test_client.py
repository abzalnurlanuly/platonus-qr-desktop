import json
import socket
from unittest import TestCase
from unittest.mock import patch

import httpx

from platonus_desktop.client import (
    AUTH_TYPE_PATH, MAX_PROFILE_BYTES, MAX_RESPONSE_BYTES, PlatonusClient, normalize_university_url,
)
from platonus_desktop.models import AccountProfile, AttendanceQR, Coordinates, PlatonusError


BASE_URL = "https://university.example"
LOGIN = "/rest/api/mobile/authentication/login"
REFRESH = "/rest/api/mobile/authentication/refresh"
QR_API = "/rest/mobile/qr-code"
JOURNAL = QR_API + "/journalAttendance"
EVENT = QR_API + "/eventAttendance"


class ClientTests(TestCase):
    def make_client(self, replies, *, diagnostics=None, auth_type="1"):
        pending = iter(replies)
        requests = []

        def handle(request):
            self.assertEqual(request.url.host, "university.example")
            self.assertEqual(request.url.scheme, "https")
            if request.url.path == AUTH_TYPE_PATH:
                self.assertEqual(request.method, "GET")
                self.assertNotIn("token", request.headers)
                self.assertEqual(request.content, b"")
                return httpx.Response(200, json={"authenticationType": auth_type})
            requests.append(request)
            reply = next(pending)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client = PlatonusClient(
            BASE_URL, language=3, transport=httpx.MockTransport(handle), diagnostics=diagnostics,
        )
        self.addCleanup(client.close)
        return client, requests

    def login_response(self, *, refresh=True):
        data = {"auth_token": "test-session", "additional_field": "ignored"}
        if refresh:
            data["refresh_token"] = "test-refresh"
        return httpx.Response(200, json=data)

    def test_login_and_submit_use_the_observed_mobile_contract(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(200, json={
                "success": "true", "scan_date": "2026-11-06 10:30:00",
                "message": "user-specific remote message",
            }),
        ])
        client.login(" test-user ", "test-password")
        result = client.submit(AttendanceQR("payload", "code+test", "journal", False))
        self.assertTrue(result.success)
        self.assertEqual(result.scanned_at, "2026-11-06T10:30:00")
        self.assertNotIn("user-specific", result.message)
        login, submit = requests
        self.assertEqual(login.url.path, LOGIN)
        self.assertEqual(dict(login.url.params), {"language": "3", "lang": "3"})
        self.assertEqual(json.loads(login.content), {
            "login": "test-user", "iin": "", "icNumber": "test-user", "password": "test-password",
        })
        self.assertEqual(submit.url.path, JOURNAL)
        self.assertEqual(dict(submit.url.params), {"lang": "3"})
        self.assertEqual(submit.headers["token"], "test-session")
        self.assertEqual(submit.headers["Content-Type"], "text/plain")
        self.assertEqual(submit.content, b"code+test")

    def test_known_false_location_setting_suppresses_coordinates(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(200, json={"locationRequired": 0}),
            httpx.Response(200, json={"success": True}),
        ])
        client.login("test-user", "test-password")
        qr = AttendanceQR("payload", "plus+slash/", "journal", True)
        self.assertTrue(client.submit(qr, coordinates=Coordinates(51.0, 71.0)).success)
        self.assertEqual(requests[1].method, "GET")
        self.assertIn(b"/locationRequired/plus%2Bslash%2F?", requests[1].url.raw_path)
        self.assertEqual(dict(requests[2].url.params), {"lang": "3"})

    def test_location_is_required_conservatively_if_lookup_changes(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(200, json={"locationCheckMoved": True}),
        ])
        client.login("test-user", "test-password")
        with self.assertRaises(PlatonusError) as raised:
            client.submit(AttendanceQR("payload", "code", "journal", True))
        self.assertEqual(raised.exception.code, "location_required")
        self.assertEqual([request.method for request in requests], ["POST", "GET"])

    def test_location_get_retries_once_then_uses_confirmed_coordinates(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(503),
            httpx.Response(200, json={"locationRequired": True}),
            httpx.Response(200, json={"success": True}),
        ])
        client.login("test-user", "test-password")
        result = client.submit(
            AttendanceQR("payload", "code", "journal", True), coordinates=Coordinates(51.0, 71.0),
        )
        self.assertTrue(result.success)
        self.assertEqual([request.method for request in requests], ["POST", "GET", "GET", "POST"])
        self.assertEqual(dict(requests[-1].url.params), {"lang": "3", "latitude": "51.0", "longitude": "71.0"})

    def test_event_does_not_query_or_include_location(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(200, json={"success": False, "message": "token=test-session"}),
        ])
        client.login("test-user", "test-password")
        result = client.submit(
            AttendanceQR("payload", "code", "event", True), coordinates=Coordinates(51.0, 71.0),
        )
        self.assertFalse(result.success)
        self.assertNotIn("test-session", result.message)
        self.assertEqual(requests[1].url.path, EVENT)
        self.assertEqual(dict(requests[1].url.params), {"lang": "3"})

    def test_explicit_unauthorized_submission_is_renewed_and_retried_once(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(401),
            httpx.Response(200, json={"auth_token": "renewed-session"}),
            httpx.Response(401),
        ])
        client.login("test-user", "test-password")
        with self.assertRaises(PlatonusError) as raised:
            client.submit(AttendanceQR("payload", "code", "journal", False))
        self.assertEqual(raised.exception.code, "session_expired")
        self.assertEqual([request.url.path for request in requests], [LOGIN, JOURNAL, REFRESH, JOURNAL])
        self.assertEqual(requests[2].headers["authorization"], "Bearer test-refresh")
        self.assertNotIn("token", requests[2].headers)
        self.assertEqual(dict(requests[2].url.params), {})
        self.assertEqual(requests[3].headers["token"], "renewed-session")

    def test_rate_limit_is_reported_without_retry(self):
        client, requests = self.make_client([self.login_response(), httpx.Response(429)])
        client.login("test-user", "test-password")
        with self.assertRaises(PlatonusError) as raised:
            client.submit(AttendanceQR("payload", "code", "journal", False))
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.upstream_status, 429)
        self.assertEqual(len(requests), 2)

    def test_ambiguous_submission_outcomes_are_never_retried(self):
        for response in (
            httpx.ReadTimeout("unexpected sensitive response"),
            httpx.Response(200, text="not JSON"),
            httpx.Response(200, json={"accepted": True}),
            httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
            httpx.Response(503),
            httpx.Response(307, headers={"location": "https://other.example/"}),
        ):
            with self.subTest(response=response):
                client, requests = self.make_client([self.login_response(), response])
                client.login("test-user", "test-password")
                with self.assertRaises(PlatonusError) as raised:
                    client.submit(AttendanceQR("payload", "code", "journal", False))
                self.assertEqual(raised.exception.code, "submission_unknown")
                self.assertNotIn("sensitive", str(raised.exception))
                self.assertEqual(len(requests), 2)

    def test_failed_login_redacts_server_reply_and_disables_session(self):
        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(200, json={"login_status": "invalid", "message": "test-password"}),
        ])
        client.login("test-user", "test-password")
        with self.assertRaises(PlatonusError) as raised:
            client.login("test-user", "test-password")
        self.assertEqual(raised.exception.code, "invalid_credentials")
        self.assertNotIn("test-password", str(raised.exception))
        with self.assertRaises(PlatonusError) as raised:
            client.submit(AttendanceQR("payload", "code", "journal", False))
        self.assertEqual(raised.exception.code, "not_authenticated")
        self.assertEqual(len(requests), 2)

    def test_login_never_follows_a_redirect_with_credentials(self):
        client, requests = self.make_client([
            httpx.Response(307, headers={"location": "https://other.example/"}),
        ])
        with self.assertRaises(PlatonusError) as raised:
            client.login("test-user", "test-password")
        self.assertEqual(raised.exception.code, "api_changed")
        self.assertEqual(len(requests), 1)

    def test_profile_uses_authenticated_mobile_route_and_extracts_only_identity(self):
        client, requests = self.make_client([
            httpx.Response(200, json={"auth_token": "test-session"}, headers={
                "set-cookie": "ignored=value; Path=/; Secure",
            }),
            httpx.Response(503),
            httpx.Response(200, json={
                "name": "  Test   Account  ", "studentID": 42, "personType": "STUDENT",
                "photoBase64": "a" * (MAX_RESPONSE_BYTES + 1), "sensitive": "unused",
            }),
        ])
        client.login("test-user", "test-password")
        self.assertEqual(client.get_profile(), AccountProfile("Test Account", "42", "STUDENT"))
        self.assertEqual([request.method for request in requests], ["POST", "GET", "GET"])
        for request in requests[1:]:
            self.assertEqual(request.url.path, "/rest/mobile/personInfo/en")
            self.assertEqual(dict(request.url.params), {"lang": "3"})
            self.assertEqual(request.headers["token"], "test-session")
            self.assertNotIn("cookie", request.headers)

    def test_profile_rejects_missing_name_and_oversized_response(self):
        for reply in (
            httpx.Response(200, json={"studentID": 42}),
            httpx.Response(200, content=b"x" * (MAX_PROFILE_BYTES + 1)),
        ):
            with self.subTest(reply=reply):
                client, requests = self.make_client([self.login_response(), reply])
                client.login("test-user", "test-password")
                with self.assertRaises(PlatonusError) as raised:
                    client.get_profile()
                self.assertEqual(raised.exception.code, "api_changed")
                self.assertEqual(len(requests), 2)

    def test_iin_is_requested_only_for_universities_that_need_both_fields(self):
        client, requests = self.make_client([self.login_response()], auth_type="2")
        with self.assertRaises(PlatonusError) as raised:
            client.login("test-user", "test-password")
        self.assertEqual(raised.exception.code, "additional_identity_required")
        self.assertEqual(requests, [])
        client.login("test-user", "test-password", iin="012345678901")
        self.assertEqual(json.loads(requests[0].content), {
            "login": "test-user", "iin": "012345678901", "icNumber": "test-user", "password": "test-password",
        })

    def test_iin_only_uses_the_identifier_field(self):
        client, requests = self.make_client([self.login_response()], auth_type="3")
        client.login("012345678901", "test-password")
        self.assertEqual(json.loads(requests[0].content), {
            "login": "", "iin": "012345678901", "icNumber": "", "password": "test-password",
        })

    def test_coordinate_validation(self):
        for latitude, longitude in [(0, 0), (True, 71), (91, 71), (51, -181), (float("nan"), 71)]:
            with self.subTest(latitude=latitude, longitude=longitude):
                with self.assertRaises(ValueError):
                    Coordinates(latitude, longitude)

    def test_diagnostics_show_rejection_reason_without_sensitive_values(self):
        records = []

        class Recorder:
            def record(self, event, **fields):
                records.append((event, fields))

        client, requests = self.make_client([
            httpx.Response(200, json={
                "auth_token": "test-session", "refresh_token": "test-refresh",
                "message": "test-user logged in with test-password",
            }),
            httpx.Response(200, json={
                "name": "Private Account Name", "studentID": 42, "photoBase64": "profile-photo",
                "message": "Profile response must never be included.",
            }),
            httpx.Response(200, json={"locationRequired": True}),
            httpx.Response(200, json={
                "success": False, "errorCode": "QR_ALREADY_USED", "tutor": "Private Tutor",
                "message": "QR already used. Student private@example.com, tutor Private Tutor. "
                "Code original-code+qr or original-code%2Bqr (original-code%2bqr). "
                "At 51.12345, 71.76543 with Bearer test-session and test-refresh. "
                "password=echoed-password, path https://server.example/?private=opaque",
            }),
        ], diagnostics=Recorder())
        client.login("test-user", "test-password")
        client.get_profile()
        qr = AttendanceQR("raw-private-qr", "original-code+qr", "journal", True)
        result = client.submit(qr, coordinates=Coordinates(51.12345, 71.76543))
        self.assertFalse(result.success)
        self.assertEqual(len(requests), 4)
        events = [fields for event, fields in records if event == "upstream_response"]
        self.assertEqual([event["operation"] for event in events], [
            "authentication_settings", "login", "account_profile", "location_required", "journal_attendance",
        ])
        self.assertTrue(all(event["status"] == 200 and event["duration_ms"] >= 0 for event in events))
        attendance = records[-1][1]
        self.assertEqual(attendance["outcome"], "rejected")
        self.assertFalse(attendance["success"])
        self.assertEqual(attendance["server_code"], "QR_ALREADY_USED")
        self.assertIn("QR already used.", attendance["server_message"])
        log = json.dumps(records)
        for secret in (
            "test-user", "test-password", "test-session", "test-refresh", "Private Account Name",
            "Private Tutor", "profile-photo", "raw-private-qr", "original-code", "51.12345",
            "71.76543", "private@example.com", "echoed-password", "private=opaque",
        ):
            self.assertNotIn(secret, log)

    def test_diagnostics_read_small_4xx_reasons_without_changing_submit_policy(self):
        records = []

        class Recorder:
            def record(self, event, **fields):
                records.append((event, fields))

        client, requests = self.make_client([
            self.login_response(),
            httpx.Response(409, json={"error": {"message": "Already recorded", "code": "DUPLICATE"}}),
            httpx.Response(400, content=b"x" * (8 * 1024 + 1)),
            httpx.ReadTimeout("unsafe private transport detail"),
        ], diagnostics=Recorder())
        client.login("test-user", "test-password")
        qr = AttendanceQR("payload", "code", "journal", False)
        with self.assertRaises(PlatonusError) as raised:
            client.submit(qr)
        self.assertEqual(raised.exception.code, "upstream_rejected")
        self.assertEqual(records[-1][1]["server_message"], "Already recorded")
        self.assertEqual(records[-1][1]["server_code"], "DUPLICATE")
        self.assertEqual(records[-1][1]["status"], 409)
        with self.assertRaises(PlatonusError) as raised:
            client.submit(qr)
        self.assertEqual(raised.exception.code, "upstream_rejected")
        self.assertNotIn("server_message", records[-1][1])
        self.assertEqual(records[-2][1]["diagnostic_body_error"], "ValueError")
        with self.assertRaises(PlatonusError) as raised:
            client.submit(qr)
        self.assertEqual(raised.exception.code, "submission_unknown")
        self.assertEqual(records[-2][1]["transport_error"], "ReadTimeout")
        self.assertNotIn("unsafe private transport detail", json.dumps(records))
        self.assertEqual(len(requests), 4)



class UniversityURLTests(TestCase):
    def test_copied_portal_urls_normalize_without_query_or_fragment(self):
        for entered, expected in (
            ("university.example", BASE_URL),
            (" https://UNIVERSITY.example:443/v7/#/auth ", BASE_URL),
            ("https://university.example/auth?lang=en", BASE_URL),
            ("https://university.example/installation/v7/login#panel", BASE_URL + "/installation"),
            ("https://university.example/installation/", BASE_URL + "/installation"),
        ):
            with self.subTest(entered=entered):
                self.assertEqual(normalize_university_url(entered), expected)

    def test_invalid_destinations_are_rejected(self):
        for url in (
            "http://university.example", "file:///tmp/credentials", "https://localhost",
            "https://service.localhost", "https://campus.internal", "https://127.0.0.1",
            "https://[::1]", "https://93.184.216.34", "https://example.org:444",
            "https://example.org:", "https://user:password@university.example",
            "https://university.example%40other.example", "https://example.org/..",
            "https://example.org/a//b", "https://example.org/%2F",
            "https://example.org\\@host.example", "https://example.org\\n.example",
        ):
            with self.subTest(url=url), self.assertRaises(PlatonusError) as raised:
                normalize_university_url(url)
            self.assertEqual(raised.exception.code, "invalid_university_url")

    def test_public_probe_uses_the_selected_installation_prefix(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"authenticationType": "1"})
            return httpx.Response(200, json={"auth_token": "test-session"})

        with PlatonusClient(
            "https://second.example/portal/v7/#/auth", transport=httpx.MockTransport(handle),
        ) as client:
            client.login("test-user", "test-password")
        self.assertEqual([request.url.host for request in requests], ["second.example", "second.example"])
        self.assertEqual([request.url.path for request in requests], ["/portal" + AUTH_TYPE_PATH, "/portal" + LOGIN])
        self.assertEqual(requests[0].content, b"")
        self.assertNotIn("token", requests[0].headers)
        self.assertEqual(json.loads(requests[1].content)["password"], "test-password")

    def test_login_does_not_send_credentials_to_unrecognized_servers(self):
        for reply, code in (
            (httpx.Response(200, text="<html>Login page</html>"), "unsupported_university"),
            (httpx.Response(404), "unsupported_university"),
            (httpx.Response(302, headers={"Location": "https://other.example/"}), "unsupported_university"),
            (httpx.Response(200, json={"authenticationType": "sso"}), "unsupported_authentication"),
        ):
            with self.subTest(reply=reply):
                requests = []

                def handle(request):
                    requests.append(request)
                    return reply

                with PlatonusClient(BASE_URL, transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(PlatonusError) as raised:
                        client.login("test-user", "test-password")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].method, "GET")
                self.assertNotIn("test-password", str(requests[0].url))
                self.assertEqual(requests[0].content, b"")

    def test_production_transport_blocks_mixed_public_and_private_dns(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"authenticationType": "1"})

        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]
        with (
            patch("platonus_desktop.client.socket.getaddrinfo", return_value=addresses),
            patch("platonus_desktop.client.httpx.HTTPTransport", return_value=httpx.MockTransport(handle)),
            PlatonusClient(BASE_URL) as client,
            self.assertRaises(PlatonusError) as raised,
        ):
            client.login("test-user", "test-password")
        self.assertEqual(raised.exception.code, "invalid_university_url")
        self.assertEqual(requests, [])

    def test_production_transport_pins_address_and_preserves_tls_hostname(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"authenticationType": "1"})
            return httpx.Response(200, json={"auth_token": "test-session"})

        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
        with (
            patch("platonus_desktop.client.socket.getaddrinfo", return_value=addresses) as resolver,
            patch("platonus_desktop.client.httpx.HTTPTransport", return_value=httpx.MockTransport(handle)),
            PlatonusClient(BASE_URL) as client,
        ):
            client.login("test-user", "test-password")
        resolver.assert_called_once_with("university.example", 443, type=socket.SOCK_STREAM)
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.url.host, "8.8.8.8")
            self.assertEqual(request.headers["host"], "university.example")
            self.assertEqual(request.extensions["sni_hostname"], "university.example")
