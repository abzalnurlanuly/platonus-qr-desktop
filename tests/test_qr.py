from contextlib import closing
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageGrab
import zxingcpp

from platonus_desktop.models import PlatonusError
from platonus_desktop.qr import (
    MAX_IMAGE_SIDE,
    MAX_QR_CHARS,
    MAX_UPLOAD_BYTES,
    decode_clipboard,
    decode_file,
    decode_image,
    parse_qr,
)


def make_qr(text: str) -> Image.Image:
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    bitmap = zxingcpp.write_barcode_to_image(barcode, scale=4)
    with memoryview(bitmap) as pixels:
        height, width = pixels.shape
        return Image.frombytes("L", (width, height), pixels.tobytes())


def png_bytes(image: Image.Image) -> bytes:
    with BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


class QRTests(unittest.TestCase):
    def test_parser_preserves_code_and_raw_text(self):
        raw = ' {"a":"j","c":"  code_123_a+B  ","l":0,"extra":true} '
        result = parse_qr(raw)
        self.assertEqual(result.raw, raw)
        self.assertEqual(result.code, "  code_123_a+B  ")
        self.assertEqual(result.action, "journal")
        self.assertFalse(result.location_flag)

    def test_parser_accepts_long_aliases(self):
        result = parse_qr('{"a":null,"action":"eventAttendance","code":"fixture","l":"TRUE"}')
        self.assertEqual(result.action, "event")
        self.assertTrue(result.location_flag)

    def test_parser_rejects_invalid_or_unsafe_codes(self):
        for raw in (
            "https://example.test/",
            '{"a":"unknown","c":"fixture"}',
            '{"a":"j","c":42}',
            '{"a":"j","c":"\\u0000"}',
            '{"a":"j","c":"\\ud800"}',
            '{"a":"j","c":"fixture","l":NaN}',
            '[' * 1500 + ']' * 1500,
            ' ' * (MAX_QR_CHARS + 1),
        ):
            with self.subTest(raw=raw[:100]), self.assertRaises(PlatonusError) as caught:
                parse_qr(raw)
            self.assertEqual(caught.exception.code, "invalid_qr")

    def test_decode_bytes_file_and_clipboard(self):
        raw = '{"a":"j","c":"fixture_001_X+Z","l":0}'
        with closing(make_qr(raw)) as image:
            data = png_bytes(image)
            self.assertEqual(decode_image(data).raw, raw)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "attendance.png"
                path.write_bytes(data)
                self.assertEqual(decode_file(path).code, "fixture_001_X+Z")
            with patch.object(ImageGrab, "grabclipboard", return_value=image.copy()):
                self.assertEqual(decode_clipboard().raw, raw)

    def test_multiple_distinct_attendance_codes_are_ambiguous(self):
        with (
            closing(make_qr('{"a":"j","c":"first","l":0}')) as first,
            closing(make_qr('{"a":"j","c":"second","l":0}')) as second,
            closing(Image.new("L", (400, 200), 255)) as combined,
        ):
            combined.paste(first, (12, 12))
            combined.paste(second, (212, 12))
            with self.assertRaises(PlatonusError) as caught:
                decode_image(png_bytes(combined))
            self.assertEqual(caught.exception.code, "ambiguous_qr")

    def test_image_limits_and_errors(self):
        with self.assertRaises(PlatonusError) as caught:
            decode_image(bytes(MAX_UPLOAD_BYTES + 1))
        self.assertEqual(caught.exception.code, "image_too_large")
        with closing(Image.new("L", (MAX_IMAGE_SIDE + 1, 1))) as image:
            with self.assertRaises(PlatonusError) as caught:
                decode_image(png_bytes(image))
        self.assertEqual(caught.exception.code, "image_too_large")
        with self.assertRaises(PlatonusError) as caught:
            decode_image(b"not an image")
        self.assertEqual(caught.exception.code, "invalid_image")
        with patch.object(ImageGrab, "grabclipboard", return_value=None):
            with self.assertRaises(PlatonusError) as caught:
                decode_clipboard()
        self.assertEqual(caught.exception.code, "clipboard_empty")


if __name__ == "__main__":
    unittest.main()
