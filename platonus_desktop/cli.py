"""Run the local web app or work from the terminal."""

from __future__ import annotations

import argparse
from getpass import GetPassWarning, getpass
import json
from pathlib import Path
from secrets import token_urlsafe
import socket
import sys
import warnings
import webbrowser

from .diagnostics import DiagnosticEntry, DiagnosticLog
from .locale import detect_language, text
from .models import AttendanceQR, Coordinates, PlatonusError

DEFAULT_PORT = 8765
HOST = "127.0.0.1"


def _secret(prompt: str, language: str) -> str:
    if not sys.stdin.isatty():
        raise PlatonusError("terminal_required", text(language, "terminal_required"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except GetPassWarning:
            raise PlatonusError("terminal_required", text(language, "terminal_required")) from None


def _listen(port: int) -> socket.socket:
    listener = socket.socket()
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((HOST, port))
    except OSError:
        listener.close()
        raise
    return listener


def _serve(port: int | None, language: str, open_browser: bool, diagnostics: DiagnosticLog) -> None:
    from waitress import create_server

    from .web import MAX_REQUEST_BYTES, create_app

    if port is None:
        try:
            listener = _listen(DEFAULT_PORT)
        except OSError as exc:
            if exc.errno not in (48, 98, 10048) and getattr(exc, "winerror", None) != 10048:
                raise
            listener = _listen(0)
    else:
        listener = _listen(port)

    try:
        actual_port = listener.getsockname()[1]
        key = token_urlsafe(32)
        app = create_app(access_token=key, host=HOST, port=actual_port, language=language, diagnostics=diagnostics)
        server = create_server(
            app, sockets=[listener], threads=4, connection_limit=12,
            channel_timeout=30, cleanup_interval=5, max_request_header_size=16 * 1024,
            max_request_body_size=MAX_REQUEST_BYTES, inbuf_overflow=MAX_REQUEST_BYTES,
            expose_tracebacks=False, log_socket_errors=False,
        )
        try:
            address = f"http://{HOST}:{actual_port}"
            print(text(language, "web_address", address=address), flush=True)
            print(text(language, "access_key", key=key), flush=True)
            print(text(language, "stop_server"), flush=True)
            diagnostics.record("server_started", port=actual_port)
            if open_browser:
                try:
                    opened = webbrowser.open(f"{address}/#{key}", new=2)
                except (OSError, webbrowser.Error):
                    opened = False
                if not opened:
                    print(text(language, "open_manually"), flush=True)
            server.run()
        finally:
            server.task_dispatcher.shutdown()
            server.close()
            app.extensions["platonus_close"]()
    finally:
        listener.close()


def _parser(language: str) -> argparse.ArgumentParser:
    def label(key: str) -> str:
        return text(language, key)

    parser = argparse.ArgumentParser(prog="platonus-qr-desktop", description=label("description"))
    parser.add_argument("--lang", choices=("ru", "en"), help=label("language"))
    commands = parser.add_subparsers(dest="command")
    web = commands.add_parser("web", aliases=["serve"], help=label("web"))
    web.add_argument("--port", type=int, help=label("port"))
    web.add_argument("--no-browser", action="store_true", help=label("no_browser"))

    def add_sources(command: argparse.ArgumentParser) -> None:
        source = command.add_mutually_exclusive_group()
        source.add_argument("--image", type=Path, help=label("image"))
        source.add_argument("--clipboard", action="store_true", help=label("clipboard"))

    add_sources(commands.add_parser("cli", aliases=["check-in"], help=label("cli")))
    add_sources(commands.add_parser("scan", help=label("scan")))
    commands.add_parser("login-check", help=label("login_check"))
    return parser


def _read_qr(args: argparse.Namespace, language: str, *, prompt: bool) -> AttendanceQR:
    from .qr import decode_clipboard, decode_file, parse_qr

    if args.image:
        return decode_file(args.image)
    if prompt and not args.clipboard:
        line = input(text(language, "paste_qr"))
        if line.strip():
            return parse_qr(line)
    return decode_clipboard()


def _location(language: str) -> Coordinates:
    print(text(language, "location_required"))
    try:
        lat = float(input(text(language, "latitude")).strip().replace(",", "."))
        lon = float(input(text(language, "longitude")).strip().replace(",", "."))
        return Coordinates(lat, lon)
    except ValueError:
        raise PlatonusError("invalid_coordinates", text(language, "invalid_coordinates")) from None


def _run_cli(args: argparse.Namespace, language: str, diagnostics: DiagnosticLog) -> int:
    from .client import PlatonusClient, normalize_university_url
    from .service import AttendanceService

    if not sys.stdin.isatty():
        raise PlatonusError("terminal_required", text(language, "terminal_required"))
    university = normalize_university_url(input(text(language, "university_url")))
    username = input(text(language, "username")).strip()
    with PlatonusClient(university, language=1 if language == "ru" else 3, diagnostics=diagnostics) as client:
        password = _secret(text(language, "password"), language)
        try:
            try:
                client.login(username, password)
            except PlatonusError as exc:
                if exc.code != "additional_identity_required":
                    raise
                identity = _secret(text(language, "identity"), language)
                try:
                    client.login(username, password, iin=identity)
                finally:
                    del identity
        finally:
            del password

        try:
            name = client.get_profile().name
        except PlatonusError as exc:
            if exc.code in {"session_expired", "not_authenticated"}:
                raise
            name = text(language, "name_unavailable")
        print(text(language, "signed_in", name=name), flush=True)
        if args.command == "login-check":
            return 0

        qr = _read_qr(args, language, prompt=True)
        print(text(language, "decoded"), flush=True)
        service = AttendanceService(client)
        try:
            result = service.submit(qr)
        except PlatonusError as exc:
            if exc.code != "location_required":
                raise
            result = service.submit(qr, coordinates=_location(language))
        print(text(language, "confirmed" if result.success else "rejected"))
        if result.scanned_at:
            print(text(language, "recorded_at", time=result.scanned_at))
        return 0 if result.success else 1


def _write_log(entry: DiagnosticEntry) -> None:
    print(json.dumps(entry, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    language_parser = argparse.ArgumentParser(add_help=False)
    language_parser.add_argument("--lang", choices=("ru", "en"))
    choice, _ = language_parser.parse_known_args(argv)
    language = choice.lang or detect_language()
    parser = _parser(language)
    args = parser.parse_args(argv)
    diagnostics = DiagnosticLog(on_entry=_write_log)
    try:
        if args.command in (None, "web", "serve"):
            port = getattr(args, "port", None)
            if port is not None and not 0 <= port <= 65535:
                parser.error(text(language, "invalid_port"))
            _serve(port, language, not getattr(args, "no_browser", False), diagnostics)
            return 0
        if args.command == "scan":
            print(_read_qr(args, language, prompt=False).raw)
            return 0
        return _run_cli(args, language, diagnostics)
    except PlatonusError as exc:
        diagnostics.record("command_failed", operation=args.command, error_code=exc.code, upstream_status=exc.upstream_status)
        print(text(language, exc.code, fallback=str(exc)), file=sys.stderr)
        return 1
    except EOFError:
        print(text(language, "input_closed"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except ImportError:
        print(text(language, "missing_dependency"), file=sys.stderr)
        return 1
    except OSError:
        print(text(language, "local_error"), file=sys.stderr)
        return 1
