from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import Mock, patch

from platonus_desktop.cli import main
from platonus_desktop.locale import text
from platonus_desktop.models import AccountProfile, AttendanceResult, Coordinates, PlatonusError


class TerminalTests(unittest.TestCase):
    def test_web_is_the_default(self):
        with patch("platonus_desktop.cli._serve") as serve:
            self.assertEqual(main(["--lang", "ru"]), 0)
        self.assertEqual(serve.call_args.args[:3], (None, "ru", True))
        self.assertIn("sample-key", text("en", "access_key", key="sample-key"))

    def test_decoded_text_and_required_coordinates(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.get_profile.return_value = AccountProfile("Sample Student")
        client.submit.side_effect = [
            PlatonusError("location_required", "Coordinates required."),
            AttendanceResult(True, "Confirmed."),
        ]
        input_values = ["https://portal.example/v7/", "sample-user", '{"a":"j","c":"sample","l":1}', "48.25", "16.37"]
        output = StringIO()
        with (
            patch("platonus_desktop.client.PlatonusClient", return_value=client) as create,
            patch("platonus_desktop.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=input_values),
            patch("platonus_desktop.cli.getpass", return_value="sample-password"),
            redirect_stdout(output),
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["--lang", "en", "cli"]), 0)

        self.assertEqual(create.call_args.args, ("https://portal.example",))
        client.login.assert_called_once_with("sample-user", "sample-password")
        self.assertEqual(client.submit.call_args.args[0].code, "sample")
        self.assertEqual(client.submit.call_args.kwargs["coordinates"], Coordinates(48.25, 16.37))
        self.assertIn("Sample Student", output.getvalue())
        self.assertNotIn("sample-password", output.getvalue())
