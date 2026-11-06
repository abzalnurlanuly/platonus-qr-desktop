import unittest
from unittest.mock import Mock

from platonus_desktop.models import AttendanceQR, AttendanceResult, Coordinates, PlatonusError
from platonus_desktop.service import AttendanceService


QR = AttendanceQR('{"a":"j","c":"test-code","l":0}', "test-code", "journal", False)


class AttendanceServiceTests(unittest.TestCase):
    def test_confirmed_check_in_is_submitted_once(self):
        client = Mock()
        client.submit.return_value = AttendanceResult(True, "Confirmed.", "2026-11-06 12:00:00")
        service = AttendanceService(client)

        first = service.submit(QR)
        second = service.submit(QR)

        client.submit.assert_called_once_with(QR, coordinates=None)
        self.assertTrue(second.success)
        self.assertEqual(first.scanned_at, second.scanned_at)

    def test_uncertain_write_is_not_repeated(self):
        client = Mock()
        client.submit.side_effect = PlatonusError("submission_unknown", "Outcome unknown.")
        service = AttendanceService(client)

        for _ in range(2):
            with self.assertRaises(PlatonusError) as raised:
                service.submit(QR)
            self.assertEqual(raised.exception.code, "submission_unknown")
        client.submit.assert_called_once()

    def test_explicit_rejection_does_not_cache_a_success(self):
        client = Mock()
        client.submit.side_effect = [AttendanceResult(False, "Rejected.", None), AttendanceResult(True, "Confirmed.", None)]
        service = AttendanceService(client, coordinates=None)

        self.assertFalse(service.submit(QR).success)
        self.assertTrue(service.submit(QR).success)
        self.assertEqual(client.submit.call_count, 2)
        client.submit.assert_called_with(QR, coordinates=None)

    def test_explicit_coordinates_reach_the_client(self):
        client = Mock()
        client.submit.return_value = AttendanceResult(True, "Confirmed.")
        service = AttendanceService(client)
        point = Coordinates(48.25, 16.37)

        service.submit(QR, coordinates=point)

        client.submit.assert_called_once_with(QR, coordinates=point)
