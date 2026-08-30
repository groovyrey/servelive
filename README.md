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
```

Startup output mirrors a JS dev server:

```
  local:   http://localhost:8000/
  network: http://192.168.100.167:8000/
  serving /home/you/site (live reload via inotify)
```

Open the `local:` or `network:` URL in a browser, edit any file under the served
directory, and the page reloads itself.

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
