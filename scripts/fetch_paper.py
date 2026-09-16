"""Download the Paper2Video PDF from a short-lived presigned URL.

Usage:
    python scripts/fetch_paper.py <url> <out_path>
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path


def download(url: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    out_path.write_bytes(data)
    return out_path


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    target = download(sys.argv[1], Path(sys.argv[2]))
    print(f"saved {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
