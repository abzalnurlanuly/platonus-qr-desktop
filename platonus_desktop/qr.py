"""Decode Platonus attendance QR payloads without network access."""

from __future__ import annotations

from contextlib import closing, nullcontext
from io import BytesIO
import json
from pathlib import Path
from typing import TYPE_CHECKING

from .models import AttendanceQR, PlatonusError

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
MAX_QR_CHARS = 8192
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP")

_ACTIONS = {
    "j": "journal",
    "journalAttendance": "journal",
    "e": "event",
    "eventAttendance": "event",
}


def _invalid_qr() -> PlatonusError:
    return PlatonusError("invalid_qr", "No valid Platonus attendance QR payload was found.")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value}")


def parse_qr(raw: str) -> AttendanceQR:
    """Validate a scanned payload and preserve its text and attendance code."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_QR_CHARS:
        raise _invalid_qr()
    try:
        payload = json.loads(raw, parse_constant=_reject_non_json_constant)
    except (ValueError, RecursionError):
        raise _invalid_qr() from None
    if not isinstance(payload, dict):
        raise _invalid_qr()

    action = payload.get("a")
    if action is None:
        action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise _invalid_qr()

    code = payload.get("c")
    if code is None:
        code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise _invalid_qr()
    if any(ord(character) < 32 or ord(character) == 127 for character in code):
        raise _invalid_qr()
    try:
        code.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid_qr() from None

    flag = payload.get("l")
    if isinstance(flag, str):
        location_flag = flag.lower() in ("1", "true")
    else:
        location_flag = isinstance(flag, (int, float)) and flag != 0
    return AttendanceQR(raw=raw, code=code, action=_ACTIONS[action], location_flag=location_flag)


def _decode_pixels(image: PillowImage) -> AttendanceQR:
    width, height = image.size
    if min(width, height) <= 0 or max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise PlatonusError("image_too_large", "Use an image within 8192 pixels per side and 16 megapixels.")
    if getattr(image, "n_frames", 1) != 1:
        raise PlatonusError("invalid_image", "Use a single-frame PNG, JPEG, WebP, or BMP image.")
    try:
        import zxingcpp
    except ImportError:
        raise PlatonusError("missing_dependency", "Install zxing-cpp to decode QR images.") from None

    # Luminance uses one byte per pixel and is sufficient for QR detection.
    pixels = nullcontext(image) if image.mode == "L" else closing(image.convert("L"))
    with pixels as grayscale:
        barcodes = zxingcpp.read_barcodes(
            grayscale, formats=zxingcpp.BarcodeFormat.QRCode, text_mode=zxingcpp.TextMode.Plain,
        )

    match = None
    for barcode in barcodes:
        try:
            candidate = parse_qr(barcode.text)
        except PlatonusError:
            continue
        if match is not None and candidate != match:
            raise PlatonusError("ambiguous_qr", "Crop the image to one Platonus attendance QR code.")
        match = candidate
    if match is None:
        raise _invalid_qr()
    return match


def decode_image(data: bytes) -> AttendanceQR:
    """Read an in-memory image using bounded input and decoded dimensions."""
    if not isinstance(data, bytes):
        raise PlatonusError("invalid_image", "Supply the image as bytes.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PlatonusError("image_too_large", "Use an image file of 8 MiB or smaller.")
    try:
        from PIL import Image
    except ImportError:
        raise PlatonusError("missing_dependency", "Install Pillow to open QR images.") from None

    try:
        with BytesIO(data) as stream, closing(Image.open(stream, formats=IMAGE_FORMATS)) as image:
            return _decode_pixels(image)
    except PlatonusError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise PlatonusError("image_too_large", "The image exceeds the decoding limits.") from None
    except (OSError, SyntaxError, TypeError, ValueError, RuntimeError, OverflowError):
        raise PlatonusError("invalid_image", "The file could not be decoded as PNG, JPEG, WebP, or BMP.") from None


def decode_file(path: Path) -> AttendanceQR:
    """Read an image file without loading more than the input limit."""
    try:
        if not path.is_file():
            raise PlatonusError("invalid_image", "Select a readable image file.")
        with path.open("rb") as stream:
            return decode_image(stream.read(MAX_UPLOAD_BYTES + 1))
    except OSError:
        raise PlatonusError("invalid_image", "The image file could not be opened.") from None


def decode_clipboard() -> AttendanceQR:
    """Read a screenshot copied into the desktop clipboard."""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        raise PlatonusError("missing_dependency", "Install Pillow to read screenshots from the clipboard.") from None

    try:
        image = ImageGrab.grabclipboard()
        if not isinstance(image, Image.Image):
            raise PlatonusError("clipboard_empty", "Copy a QR screenshot to the clipboard first.")
        with closing(image):
            return _decode_pixels(image)
    except PlatonusError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise PlatonusError("image_too_large", "The clipboard image exceeds the decoding limits.") from None
    except (OSError, SyntaxError, TypeError, ValueError, RuntimeError, OverflowError):
        raise PlatonusError("invalid_image", "The clipboard image could not be read. Try saving a PNG screenshot.") from None
