import unittest

from platonus_desktop.diagnostics import DiagnosticLog


class DiagnosticLogTests(unittest.TestCase):
    def test_log_is_bounded_and_snapshot_is_independent(self):
        log = DiagnosticLog()
        for index in range(110):
            log.record("fixture", index=index)
        entries = log.snapshot()
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0]["index"], 10)
        entries[0]["index"] = 0
        self.assertEqual(log.snapshot()[0]["index"], 10)

    def test_log_limits_text_and_strips_terminal_control_characters(self):
        log = DiagnosticLog()
        log.record("fixture", reason="reason\n\x1b" + "x" * 900)
        reason = log.snapshot()[0]["reason"]
        self.assertTrue(reason.startswith("reason "))
        self.assertLessEqual(len(reason), 512)
        self.assertNotIn("\x1b", reason)
