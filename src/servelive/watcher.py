"""inotify-backed change detection.

Watches a directory tree with the Linux inotify API through ctypes (stdlib
only). Calls a callback once per debounced change burst so consumers get a
single notification for what would otherwise be a burst of raw inotify events.
"""

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import threading
import time

try:
    _libc_path = ctypes.util.find_library("c") or (
        "/lib/libc.so.6" if os.path.exists("/lib/libc.so.6") else "libc.so.6"
    )
    libc = ctypes.CDLL(_libc_path, use_errno=True)
    INOTIFY_OK = True
except Exception:  # pragma: no cover - platform without libc
    libc = None
    INOTIFY_OK = False

IN_ATTRIB = 0x00000002
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MODIFY = 0x00000002
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CLOSE_WRITE = 0x00000008

_WATCH_MASK = (
    IN_ATTRIB
    | IN_CREATE
    | IN_DELETE
    | IN_MODIFY
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CLOSE_WRITE
)

_SIZE_OF_INOTIFY_EVENT = 16
_NAME_MAX = 4096


def _setup_libc():
    if not INOTIFY_OK:
        return
    libc.inotify_init.restype = ctypes.c_int
    libc.inotify_add_watch.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32,
    ]
    libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    libc.read.restype = ctypes.c_ssize_t
    libc.close.argtypes = [ctypes.c_int]


_setup_libc()


def is_watchable():
    """Return True if inotify is available on this system."""
    return INOTIFY_OK


class Watcher:
    """Watch a directory tree and call ``on_change`` per debounced change."""

    def __init__(self, root, on_change, debounce_ms=80, poll_ms=250):
        self.root = root
        self.on_change = on_change
        self.debounce_ms = debounce_ms / 1000.0
        self.poll_ms = poll_ms
        self._fd = None
        self._wd_to_path = {}

    def start(self):
        """Start watching. Returns self, or None if inotify is unavailable."""
        if not INOTIFY_OK:
            return None
        fd = libc.inotify_init()
        if fd < 0:
            return None
        self._fd = fd

        mask = _WATCH_MASK
        poll = select.poll()
        poll.register(fd, select.POLLIN)
        buf = ctypes.create_string_buffer(_NAME_MAX * 4)

        def add_watch(path):
            wd = libc.inotify_add_watch(fd, os.fsencode(path), mask)
            if wd >= 0:
                self._wd_to_path[wd] = path
            return wd

        def add_tree(base):
            for dirpath, dirnames, _ in os.walk(base):
                for d in list(dirnames):
                    try:
                        add_watch(os.path.join(dirpath, d))
                    except OSError:
                        pass

        root_wd = add_watch(self.root)
        if root_wd < 0:
            libc.close(fd)
            self._fd = None
            return None
        add_tree(self.root)

        pending = {"flag": False}
        lock = threading.Lock()
        debounce = threading.Event()

        def mark_changed():
            with lock:
                pending["flag"] = True
            debounce.set()

        def broadcaster():
            while True:
                debounce.wait()
                debounce.clear()
                time.sleep(self.debounce_ms)
                with lock:
                    if not pending["flag"]:
                        continue
                    pending["flag"] = False
                self.on_change()

        threading.Thread(target=broadcaster, daemon=True).start()

        def loop():
            while True:
                if not poll.poll(self.poll_ms):
                    continue
                while True:
                    n = libc.read(fd, buf, len(buf))
                    if n < 0:
                        err = ctypes.get_errno()
                        if err in (errno.EAGAIN, errno.EINTR):
                            break
                        continue
                    if n == 0:
                        break
                    off = 0
                    while off < n:
                        wd, maskv, _cookie, name_len = struct.unpack_from(
                            "iIII", buf, off
                        )
                        off += _SIZE_OF_INOTIFY_EVENT
                        name = (
                            buf.raw[off:off + name_len]
                            .split(b"\x00", 1)[0]
                            .decode("utf-8", "replace")
                            if name_len
                            else ""
                        )
                        off += name_len
                        dirname = self._wd_to_path.get(wd, self.root)
                        if (maskv & (IN_CREATE | IN_MOVED_TO)) and name:
                            newpath = os.path.join(dirname, name)
                            if os.path.isdir(newpath):
                                try:
                                    add_watch(newpath)
                                except OSError:
                                    pass
                        mark_changed()

        threading.Thread(target=loop, daemon=True).start()
        return self

    def close(self):
        """Stop watching and release the inotify fd."""
        if self._fd is not None:
            try:
                libc.close(self._fd)
            except Exception:
                pass
            self._fd = None
