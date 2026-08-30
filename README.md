# servelive

A static file server with no-cache headers and live reload, powered by Linux
inotify and Server-Sent Events. A drop-in replacement for `python3 -m http.server`
that refreshes the browser the moment a served file changes. No polling anywhere,
and no third-party dependencies (pure Python standard library + the system libc).

## Features

- No-cache headers on every response so dev browsers always fetch fresh files
- Live reload via **inotify** (real file-change detection) + **SSE** push
- Serves over the network by default (`0.0.0.0`) and prints your LAN address
- Degrades gracefully on systems without inotify (survives, just no reload)
- Optional **token auth**, **SPA fallback**, styled **directory listings**
- **gzip** and **Range** request support for faster local testing
- Request logs with per-request timing and colored HTTP method/status

## Install

```bash
pip install servelive
```

## Usage

```bash
servelive                 # serve ./ on port 8000
servelive 8500            # serve ./ on port 8500
servelive 8500 ./site     # serve ./site on port 8500
servelive --bind 127.0.0.1 9000   # localhost only
servelive -o 8000 ./app   # open the URL in your browser
```

Startup output mirrors a JS dev server:

```
  local:   http://localhost:8000/
  network: http://192.168.100.167:8000/
  serving /home/you/site (live reload via inotify)
```

Open the `local:` or `network:` URL in a browser, edit any file under the served
directory, and the page reloads itself. If the port is busy, the next free port
is used automatically and announced.

### Options

| Flag | Description |
| --- | --- |
| `-o, --open` | Open the served URL in the default browser. |
| `-q, --quiet` | Suppress per-request log lines. |
| `--no-reload` | Disable live reload (no watcher, no injected script, no SSE). |
| `--spa` | Serve `index.html` for unknown paths (single-page app fallback). |
| `--no-index` | Never auto-serve `index.html` in a directory; show a listing. |
| `--token TOKEN` | Require this token to access the server. |
| `--debounce MS` | Change-debounce window in ms before a reload fires (default 80). |
| `--no-auto-port` | Fail instead of incrementing the port when it is busy. |
| `--verbose` | Log each changed file that triggers a reload. |
| `-b, --bind ADDR` | Address to bind (default `0.0.0.0`). |

`--token` pairs HTTP Basic auth for served files with a `?token=` query for the
reload stream, so only people with the token can read files or subscribe. When
you bind to `0.0.0.0` (the default) and skip `--token`, a warning reminds you
that anyone on your network can read the served files.

## How it works

1. A background thread watches the served tree with the Linux `inotify` API
   (called through `ctypes`, stdlib only).
2. On a change, an 80 ms debounce coalesces the raw event burst into a single
   notification.
3. The server broadcasts a reload over an SSE stream to every connected page.

The reload script is injected into served HTML, so any page you open through
`servelive` gets live reload automatically.

## License

MIT
