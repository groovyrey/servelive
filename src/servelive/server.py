"""The static HTTP server with no-cache headers and SSE live reload."""

import functools
import http.server
import io
import os
import socketserver
import threading

from servelive.watcher import is_watchable, Watcher

LIVE_RELOAD = b"""<script>
(function () {
  try {
    var es = new EventSource(location.origin + "/__servelive_reload", { withCredentials: false });
    es.onmessage = function (e) {
      if (e.data === "reload") location.reload();
    };
    es.onerror = function () { es.close(); };
  } catch (e) {}
})();
</script>
"""

RELOAD_PATH = "/__servelive_reload"


class ReloadHub:
    """Fan-out reload notifications to connected SSE clients."""

    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()

    def add(self, event):
        with self._lock:
            self._clients.append(event)
        return self._remove_client

    def _remove_client(self, event):
        with self._lock:
            if event in self._clients:
                self._clients.remove(event)

    def broadcast(self):
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
                yield b"data: reload\n\n"
    finally:
        detach()


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "servelive/1.1"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        if self.path.rstrip("/").endswith(RELOAD_PATH):
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
        return super().do_GET()

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        ctype = self.guess_type(path)
        if ctype == "text/html" and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    html = f.read()
                html = inject_reload_script(html)
                self.send_response(200)
                self.send_header("Content-Type", ctype or "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Last-Modified", self.date_time_string())
                self.end_headers()
                return io.BytesIO(html)
            except OSError:
                pass
        return super().send_head()


def inject_reload_script(html):
    if b"</body>" in html:
        return html.replace(b"</body>", LIVE_RELOAD + b"</body>")
    return html + LIVE_RELOAD


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LiveServer:
    """Convenience wrapper combining the HTTP server and the watcher."""

    def __init__(self, directory=".", port=8000):
        self.directory = os.path.abspath(directory)
        self.port = int(port)
        self.hub = ReloadHub()
        self.watcher = None
        self.httpd = None

    def start(self):
        handler = functools.partial(NoCacheHandler, directory=self.directory)
        self.httpd = ThreadingHTTPServer(("", self.port), handler)
        self.httpd.hub = self.hub
        if is_watchable():
            self.watcher = Watcher(self.directory, self.hub.broadcast).start()
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


def create_server(directory=".", port=8000):
    """Create (but do not start) a LiveServer."""
    return LiveServer(directory=directory, port=port)
