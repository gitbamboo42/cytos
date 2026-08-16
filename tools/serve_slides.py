"""Serve the data/ directory over HTTP for the web viewer prototype.

The web viewer reads a `.cytos` slide with plain HTTP GETs, so any static
file server works -- nginx, S3, a lab server. This one exists so local
development needs nothing beyond the Python already installed. It adds two
things the stdlib server lacks:

- **CORS headers** on every response, because the viewer page (vite,
  port 5173) and the data (this server, port 8787) run on different ports,
  and the browser blocks cross-origin reads without them.
- **Range requests** (`Range: bytes=start-end`), because reading a zipped
  store or a packed tile file over HTTP means fetching byte ranges, not
  whole files. Plain zarr directories don't need it; the readers that come
  next do.

Usage::

    python tools/serve_slides.py             # serves ./data on port 8787
    python tools/serve_slides.py --dir X --port N
"""

from __future__ import annotations

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class SlideHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # CORS preflight
        self.send_response(204)
        self.end_headers()

    def send_head(self):
        """Like the stdlib version, but honors a `Range: bytes=a-b` header
        with a 206 response. Only single ranges -- that is all a store
        reader ever sends."""
        range_header = self.headers.get("Range")
        if range_header is None:
            return super().send_head()

        path = self.translate_path(self.path)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        try:
            size = os.fstat(f.fileno()).st_size
            try:
                spec = range_header.split("=", 1)[1]
                start_s, end_s = spec.split("-", 1)
                start = int(start_s) if start_s else size - int(end_s)
                end = min(int(end_s), size - 1) if end_s and start_s else size - 1
            except (IndexError, ValueError):
                self.send_error(400, f"Bad Range header: {range_header!r}")
                f.close()
                return None
            if start < 0 or start > end:
                self.send_error(416, "Requested Range Not Satisfiable")
                f.close()
                return None

            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            f.seek(start)
            self._range_remaining = end - start + 1
            return f
        except Exception:
            f.close()
            raise

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(self, "_range_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        self._range_remaining = None
        while remaining > 0:
            block = source.read(min(remaining, 64 * 1024))
            if not block:
                break
            outputfile.write(block)
            remaining -= len(block)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="data", help="directory to serve (default: data)")
    parser.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    args = parser.parse_args()

    handler = partial(SlideHandler, directory=args.dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"serving {os.path.abspath(args.dir)} at http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
