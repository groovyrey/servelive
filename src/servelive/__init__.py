"""servelive - static file server with inotify-driven live reload.

A drop-in replacement for ``python3 -m http.server`` that sends
``Cache-Control: no-store`` so dev browsers always fetch fresh files, and
pushes an SSE "reload" event to connected pages whenever a served file changes.
File changes are detected with Linux inotify (via ctypes, no third-party
dependencies) so there is no polling anywhere.
"""

from servelive.server import create_server
from servelive.cli import main
from servelive._version import __version__

__all__ = ["create_server", "main", "__version__"]
