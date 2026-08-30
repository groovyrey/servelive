"""The static HTTP server with no-cache headers, SSE live reload, and extras.

Features beyond a plain static server:
- no-cache headers on every response
- inotify + SSE live reload (payload carries the changed path)
- optional SPA fallback (--spa) and styled directory listings
- optional token gating (--token), gzip, and Range request support
- request logs with HTTP method/status highlighting and timing
"""

import base64
import functools
import gzip
import html as htmlmod
import http.server
import io
import json
import os
import posixpath
import re
import socketserver
import sys
import threading
import time
import urllib.parse

from servelive.watcher import is_watchable, Watcher

_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_METHOD_COLORS = {
    "GET": _CYAN,
    "POST": _YELLOW,
    "PUT": _YELLOW,
    "DELETE": "\033[31m",
    "HEAD": _DIM,
    "OPTIONS": _DIM,
    "PATCH": _YELLOW,
}

_RE_METHOD = re.compile(r'"([A-Z]+) ')
_RE_STATUS = re.compile(r'HTTP/\d\.\d" (\d{3})')
_RE_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")

_COMPRESSIBLE = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-www-form-urlencoded",
    "application/manifest+json",
    "image/svg+xml",
)

RELOAD_PATH = "/__servelive_reload"


def _use_color():
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _status_color(code):
    if 200 <= code < 300:
        return _GREEN
    if 300 <= code < 400:
        return _CYAN
    if 400 <= code < 600:
        return "\033[31m"
    return "\033[33m"


def _fmt_size(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{int(n)} B"


class ReloadHub:
    """Fan-out reload notifications to connected SSE clients."""

    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()
        self.last_change = None

    def add(self, event):
        with self._lock:
            self._clients.append(event)
        return functools.partial(self._remove_client, event)

    def _remove_client(self, event):
        with self._lock:
            if event in self._clients:
                self._clients.remove(event)

    def broadcast(self, path=None):
        if path is not None:
            self.last_change = path
        with self._lock:
            clients = list(self._clients)
        for ev in clients:
            ev.set()


class _SSEEvent(threading.Event):
    pass


def sse_body(hub):
    yield b"retry: 1000\n\n"
    ev = _SSEEvent()
    detach = hub.add(ev)
    try:
        while True:
            if ev.wait(25):
                ev.clear()
                payload = json.dumps({
                    "reload": True,
                    "path": hub.last_change or "/",
                }).encode("utf-8")
                yield b"data: " + payload + b"\n\n"
            else:
                yield b": keepalive\n\n"
    finally:
        detach()


def _live_reload_script(token):
    query = f"?token={urllib.parse.quote(token)}" if token else ""
    return (
        b"<script>\n"
        b"(function () {\n"
        b"  try {\n"
        b"    var es = new EventSource(location.origin + "
        + bytes(RELOAD_PATH + query, "utf-8") + b");\n"
        b"    es.onmessage = function () { location.reload(); };\n"
        b"  } catch (e) {}\n"
        b"})();\n"
        b"</script>\n"
    )


def inject_reload_script(html, token=None):
    script = _live_reload_script(token)
    if b"</body>" in html:
        return html.replace(b"</body>", script + b"</body>")
    return html + script


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "servelive/1.3"

    def __init__(self, *args, directory=None, reload_enabled=True, spa=False,
                 auto_index=True, token=None, quiet=False, **kwargs):
        self.reload_enabled = reload_enabled
        self.spa = spa
        self.auto_index = auto_index
        self.token = token
        self.quiet = quiet
        self._t0 = time.monotonic()
        self._basic_auth = None
        if token:
            self._basic_auth = "Basic " + base64.b64encode(
                token.encode("utf-8")).decode("ascii")
        super().__init__(*args, directory=directory, **kwargs)

    def handle(self):
        self._t0 = time.monotonic()
        super().handle()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    # --- auth ------------------------------------------------------------

    def _check_auth(self):
        if not self.token:
            return True
        if self.headers.get("Authorization") == self._basic_auth:
            return True
        parts = urllib.parse.urlsplit(self.path)
        if parts.path.rstrip("/") == RELOAD_PATH:
            q = urllib.parse.parse_qs(parts.query)
            if q.get("token") == [self.token]:
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="servelive"')
        self.end_headers()
        self.wfile.write(b"401 Unauthorized\n")
        return False

    # --- logging ----------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.quiet:
            return
        msg = fmt % args
        if _use_color():
            m = _RE_METHOD.search(msg)
            s = _RE_STATUS.search(msg)
            if m:
                status = s.group(1) if s else None
                method = m.group(1)
                col = _METHOD_COLORS.get(method, _DIM)
                msg = msg.replace(m.group(0), f'"{_BOLD}{col}{method}{_RESET} ', 1)
                if status:
                    sc = _status_color(int(status))
                    msg = _RE_STATUS.sub(
                        f"{_BOLD}{sc}{status}{_RESET}", msg, count=1)
        elapsed = (time.monotonic() - self._t0) * 1000.0
        sys.stderr.write(
            f"{self.address_string()} - - [{self.log_date_time_string()}] "
            f"{msg} {elapsed:.0f}ms\n")

    # --- SSE --------------------------------------------------------------

    def do_GET(self):
        if not self._check_auth():
            return
        parts = urllib.parse.urlsplit(self.path)
        if self.reload_enabled and parts.path.rstrip("/") == RELOAD_PATH:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for chunk in sse_body(self.server.hub):
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._check_auth():
            return
        super().do_HEAD()

    # --- response building ------------------------------------------------

    def send_head(self):
        if not self._check_auth():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            index = os.path.join(path, "index.html")
            if os.path.isfile(index) and self.auto_index:
                path = index
            else:
                return self.list_directory(path)
        if not os.path.isfile(path):
            if self.spa:
                root = self.translate_path("/")
                fallback = os.path.join(root, "index.html")
                if os.path.isfile(fallback):
                    return self._serve_file(fallback, force_html=True)
            return super().send_head()
        return self._serve_file(path, force_html=False)

    def list_directory(self, path):
        try:
            names = sorted(os.listdir(path))
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None
        rows = []
        displaypath = htmlmod.escape(urllib.parse.unquote(self.path))
        parent = posixpath.dirname(self.path.rstrip("/"))
        if self.path.rstrip("/") != "/":
            rows.append(
                f'<li class="up"><a href="{htmlmod.escape(parent)}">..</a></li>')
        for name in names:
            full = os.path.join(path, name)
            ftag = "dir" if os.path.isdir(full) else "file"
            url = urllib.parse.quote(name, safe="/:")
            try:
                size = _fmt_size(os.path.getsize(full))
            except OSError:
                size = "-"
            try:
                mtime = time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(os.path.getmtime(full)))
            except OSError:
                mtime = "-"
            rows.append(
                f'<li class="{ftag}">'
                f'<a href="{htmlmod.escape(url)}">{htmlmod.escape(name)}</a>'
                f'<span class="meta">{mtime} · {size}</span></li>')
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Index of " + displaypath + "</title><style>"
            ":root{color-scheme:dark;--bg:#070b16;--surface:#101a2e;--line:#1d2b47;"
            "--ink:#eaf0ff;--ink-2:#93a0c0;--accent:#7fb7ff}"
            "body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);"
            "margin:0;padding:48px 24px;min-height:100dvh}"
            ".wrap{max-width:820px;margin:0 auto;border:1px solid var(--line);"
            "border-radius:18px;background:var(--surface);padding:20px 24px}"
            "h1{font-size:15px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;"
            "color:var(--ink-2);margin:0 0 14px}"
            "ul{list-style:none;margin:0;padding:0}"
            "li{display:flex;justify-content:space-between;gap:16px;padding:11px 2px;"
            "border-top:1px solid var(--line);font-size:15px}"
            "li:first-of-type{border-top:none}"
            "a{color:var(--ink);text-decoration:none}"
            "a:hover{color:var(--accent)}"
            "li.up a{color:var(--accent);font-weight:600}"
            ".meta{color:var(--ink-2);font-size:13px;white-space:nowrap}"
            "</style></head><body><div class=\"wrap\"><h1>Index of "
            + displaypath + "</h1><ul>" + "".join(rows) + "</ul></div></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def _serve_file(self, path, force_html=False):
        ctype = "text/html; charset=utf-8" if force_html else self.guess_type(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return super().send_head()

        tag = ctype or ""
        use_gzip = (
            self.headers.get("Accept-Encoding", "") != ""
            and "gzip" in self.headers.get("Accept-Encoding", "").split(",")
            and tag.startswith(_COMPRESSIBLE)
            and len(data) > 512
        )

        rng = None
        length = len(data)
        if "Range" in self.headers and not use_gzip:
            m = _RE_RANGE.search(self.headers.get("Range", "").strip())
            if m:
                a, b = m.groups()
                start = end = -1
                if a == "" and b != "":
                    suf = int(b)
                    if suf > 0:
                        start = max(length - suf, 0)
                        end = length - 1
                elif a != "":
                    start = int(a)
                    end = int(b) if b else length - 1
                    if start >= length or start > end:
                        start, end = -1, -1
                if start >= 0 and end >= start:
                    rng = (start, min(end, length - 1))

        if tag.startswith("text/html") or force_html:
            if self.reload_enabled:
                data = inject_reload_script(data, self.token)
            use_gzip = use_gzip and len(data) > 512

        status = 200
        total = length
        if rng:
            start, end = rng
            data = data[start:end + 1]
            status = 206
        elif use_gzip:
            data = gzip.compress(data)
            use_gzip = True

        self.send_response(status)
        self.send_header("Content-Type", ctype
                         if ctype else "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{total}")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        try:
            self.send_header("Last-Modified", self.date_time_string(
                os.path.getmtime(path)))
        except OSError:
            pass
        self.end_headers()
        return io.BytesIO(data)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LiveServer:
    """Convenience wrapper combining the HTTP server and the watcher."""

    def __init__(self, directory=".", port=8000, bind="0.0.0.0",
                 reload_enabled=True, debounce_ms=80, spa=False,
                 auto_index=True, token=None, quiet=False, auto_port=True,
                 on_change=None):
        self.directory = os.path.abspath(directory)
        self.port = int(port)
        self.bind = bind
        self.reload_enabled = reload_enabled
        self.debounce_ms = debounce_ms
        self.spa = spa
        self.auto_index = auto_index
        self.token = token
        self.quiet = quiet
        self.auto_port = auto_port
        if on_change is None:
            on_change = self._hub_broadcast
        self._on_change = on_change
        self.hub = ReloadHub()
        self.watcher = None
        self.httpd = None
        self.port_shifted = 0

    def _hub_broadcast(self, path):
        self.hub.broadcast(path)

    def start(self):
        handler = functools.partial(
            NoCacheHandler, directory=self.directory,
            reload_enabled=self.reload_enabled, spa=self.spa,
            auto_index=self.auto_index, token=self.token, quiet=self.quiet)
        port = self.port
        attempts = 0
        while True:
            try:
                self.httpd = ThreadingHTTPServer((self.bind, port), handler)
                break
            except OSError:
                if not self.auto_port or attempts >= 50:
                    raise
                port += 1
                attempts += 1
        self.port_shifted = port - self.port
        self.port = self.httpd.server_address[1]
        self.httpd.hub = self.hub
        if self.reload_enabled and is_watchable():
            self.watcher = Watcher(
                self.directory, self._on_change,
                debounce_ms=self.debounce_ms).start()
        return self

    def serve_forever(self):
        if self.httpd is None:
            self.start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
    def close(self):
        if self.watcher is not None:
            self.watcher.close()
        if self.httpd is not None:
            self.httpd.server_close()


def create_server(directory=".", port=8000, **kwargs):
    """Create (but do not start) a LiveServer."""
    return LiveServer(directory=directory, port=port, **kwargs)