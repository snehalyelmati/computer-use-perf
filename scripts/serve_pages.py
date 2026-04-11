"""Local HTTP server for captured page HTML snapshots.

Usage:
    python scripts/serve_pages.py [--dir logs/pages] [--port 8080]

Serves .html files from the pages directory so the agent can be re-run
against captured page states for debugging and replay.
"""

from __future__ import annotations

import argparse
import http.server
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve captured page HTML snapshots")
    parser.add_argument(
        "--dir",
        default="logs/pages",
        help="Directory containing captured HTML files (default: logs/pages)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to serve on (default: 8080)",
    )
    args = parser.parse_args()

    pages_dir = Path(args.dir).resolve()
    if not pages_dir.is_dir():
        print(f"Error: directory not found: {pages_dir}", file=sys.stderr)
        sys.exit(1)

    html_files = sorted(pages_dir.glob("*.html"))
    if not html_files:
        print(f"No .html files found in {pages_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Serving {len(html_files)} page(s) from {pages_dir}")
    print(f"Server: http://localhost:{args.port}")
    print()
    print("Available pages:")
    for f in html_files:
        print(f"  http://localhost:{args.port}/{f.name}")
    print()

    os.chdir(pages_dir)
    handler = http.server.SimpleHTTPRequestHandler
    with http.server.HTTPServer(("", args.port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
