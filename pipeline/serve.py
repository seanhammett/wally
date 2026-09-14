#!/usr/bin/env python3
"""Static server for site/ that honours HTTP Range requests.

Python's http.server ignores Range and returns the whole file with a 200, which
means a local preview downloads all 33 MB of communes.pmtiles before drawing
anything. Every host the plan targets (Cloudflare Pages, Netlify, GitHub Pages)
supports ranges; this makes local development behave the same way.
"""
from __future__ import annotations

import argparse
import functools
import os
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        match = RANGE_RE.match(header.strip())
        if not match:
            return super().send_head()

        try:
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(fh.fileno()).st_size
        start_s, end_s = match.groups()
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:  # suffix range: bytes=-500
            start = max(0, size - int(end_s))
            end = size - 1
        end = min(end, size - 1)
        if start > end:
            fh.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        fh.seek(start)
        return _Limited(fh, end - start + 1)

    def end_headers(self):
        if "Accept-Ranges" not in self._headers_buffer_names():
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _headers_buffer_names(self) -> set[str]:
        return {
            line.split(b":")[0].decode("latin-1")
            for line in getattr(self, "_headers_buffer", [])
            if b":" in line
        }

    def log_message(self, fmt, *args):  # quieter than the default
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
            super().log_message(fmt, *args)


class _Limited:
    """A file object that stops after n bytes, for copyfile()."""

    def __init__(self, fh, n: int):
        self.fh, self.remaining = fh, n

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        data = self.fh.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve site/ with Range support.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "site"))
    args = ap.parse_args()

    handler = functools.partial(RangeHandler, directory=args.dir)
    print(f"serving {args.dir} on http://127.0.0.1:{args.port}  (Range requests supported)")
    HTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
