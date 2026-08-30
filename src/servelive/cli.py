"""Command-line entry point for servelive."""

import argparse
import os
import socket
import subprocess
import sys
import webbrowser

from servelive._version import __version__
from servelive.server import LiveServer
from servelive.watcher import is_watchable

_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _color(enabled):
    """Return a no-op wrapper when colors are disabled."""

    def wrap(text, code):
        return f"{code}{text}{_RESET}" if enabled else text

    return wrap


def lan_ip():
    """Determine the primary non-loopback IPv4 address on this host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ip", "-4", "addr", "show", "scope", "global"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1].split("/")[0]
                if not ip.startswith("127."):
                    return ip
    except Exception:
        pass
    return None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="servelive",
        description="Static file server with no-cache headers, inotify-driven "
                    "live reload, and extras.",
    )
    parser.add_argument(
        "port", nargs="?", type=int, default=8000,
        help="Port to bind (default: 8000). If busy, the next free port is used.",
    )
    parser.add_argument(
        "directory", nargs="?", default=".",
        help="Directory to serve (default: current directory).",
    )
    parser.add_argument(
        "--bind", "-b", default="0.0.0.0",
        help="Address to bind (default: 0.0.0.0 so other devices can connect).",
    )
    parser.add_argument(
        "--open", "-o", action="store_true",
        help="Open the served URL in the default browser after starting.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-request log lines.",
    )
    parser.add_argument(
        "--no-reload", action="store_true",
        help="Disable live reload (no watcher, no injected script, no SSE).",
    )
    parser.add_argument(
        "--spa", action="store_true",
        help="Serve index.html for unknown paths (single-page app fallback).",
    )
    parser.add_argument(
        "--no-index", action="store_true",
        help="Never auto-serve index.html in a directory; show a listing instead.",
    )
    parser.add_argument(
        "--token", metavar="TOKEN",
        help="Require this token to access the server (Basic auth for files, "
             "query token for the reload stream).",
    )
    parser.add_argument(
        "--debounce", metavar="MS", type=int, default=80,
        help="Change-debounce window in ms before a reload fires (default: 80).",
    )
    parser.add_argument(
        "--no-auto-port", action="store_true",
        help="Fail instead of incrementing the port when it is busy.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log each changed file that triggers a reload.",
    )
    parser.add_argument(
        "--version", action="version", version=f"servelive {__version__}",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    c = _color(use_color)

    if not (1 <= args.port <= 65535):
        print(c(f"error: invalid port {args.port}", _RED), file=sys.stderr)
        return 2

    if args.debounce < 0:
        print(c(f"error: invalid debounce {args.debounce}", _RED), file=sys.stderr)
        return 2

    server = LiveServer(
        directory=args.directory, port=args.port, bind=args.bind,
        reload_enabled=not args.no_reload, debounce_ms=args.debounce,
        spa=args.spa, auto_index=not args.no_index, token=args.token,
        quiet=args.quiet, auto_port=not args.no_auto_port)

    if args.verbose and not args.no_reload:
        def on_change(path):
            if path:
                print(c(f"changed: {path}", _DIM), flush=True)
            server.hub.broadcast(path)
        server._on_change = on_change

    watcher = "inotify" if (not args.no_reload and is_watchable()) else "none"
    if args.no_reload:
        pass
    elif watcher == "none":
        print(c("warning: inotify unavailable - live reload disabled", _YELLOW),
              file=sys.stderr)

    try:
        server.start()
    except OSError as e:
        print(c(f"error: could not bind {args.bind}:{args.port} ({e})", _RED),
              file=sys.stderr)
        return 1

    exposed = args.bind in ("0.0.0.0", "")
    if exposed and not args.token:
        print(c("note: bound to 0.0.0.0 without --token; anyone on your "
                "network can read served files", _YELLOW), file=sys.stderr)

    live = "live reload via inotify" if watcher == "inotify" else "live reload disabled"
    local_host = "127.0.0.1" if not exposed else "localhost"
    print(f"  {c('local:', _BOLD)}   {c(f'http://{local_host}:{args.port}/', _CYAN)}",
          flush=True)
    if exposed:
        nip = lan_ip()
        if nip:
            print(f"  {c('network:', _BOLD)} {c(f'http://{nip}:{args.port}/', _CYAN)}",
                  flush=True)
    print(f"  {c('serving', _BOLD)} {c(server.directory, _DIM)} "
          f"({c(live, _GREEN)})", flush=True)
    if server.port_shifted:
        print(c(f"note: port {args.port} busy, using {server.port} instead",
                _YELLOW), flush=True)

    if args.open:
        webbrowser.open(f"http://localhost:{server.port}/")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print(c("shutting down servelive", _DIM), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())