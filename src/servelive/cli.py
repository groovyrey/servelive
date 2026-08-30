"""Command-line entry point for servelive."""

import argparse
import socket
import subprocess
import sys

from servelive._version import __version__
from servelive.server import LiveServer
from servelive.watcher import is_watchable


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
        description="Static file server with no-cache headers and inotify-driven live reload.",
    )
    parser.add_argument(
        "port", nargs="?", type=int, default=8000,
        help="Port to bind (default: 8000).",
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
        "--version", action="version", version=f"servelive {__version__}",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not (1 <= args.port <= 65535):
        print(f"error: invalid port {args.port}", file=sys.stderr)
        return 2

    server = LiveServer(directory=args.directory, port=args.port)

    watcher = "inotify" if is_watchable() else "none"
    if watcher == "none":
        print("warning: inotify unavailable - live reload disabled", file=sys.stderr)

    try:
        server.start()
    except OSError as e:
        print(f"error: could not bind {args.bind}:{args.port} ({e})", file=sys.stderr)
        return 1

    live = "live reload via inotify" if watcher == "inotify" else "live reload disabled"
    local_host = "127.0.0.1" if args.bind not in ("0.0.0.0", "") else "localhost"
    print(f"  local:   http://{local_host}:{args.port}/", flush=True)
    if args.bind in ("0.0.0.0", ""):
        nip = lan_ip()
        if nip:
            print(f"  network: http://{nip}:{args.port}/", flush=True)
    print(f"  serving {server.directory} ({live})", flush=True)

    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
