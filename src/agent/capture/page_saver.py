"""Save page HTML snapshots for local replay."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page

from src.agent.metrics import utc_now_iso

logger = logging.getLogger(__name__)


class PageSaver:
    """Captures page HTML at each unique fingerprint for offline replay.

    Pages are saved to ``<log_dir>/pages/`` with a JSONL manifest for indexing.
    Deduplicates by fingerprint so the same page state is only saved once.
    """

    def __init__(self, log_dir: str, run_id: str) -> None:
        self.pages_dir = Path(log_dir) / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.seen_fingerprints: set[str] = set()
        self.manifest_path = self.pages_dir / "manifest.jsonl"

    async def capture_page(
        self,
        page: Page,
        step: int,
        url: str,
        title: str,
        fingerprint: str,
    ) -> None:
        """Save page HTML if this fingerprint hasn't been seen before."""
        try:
            if fingerprint in self.seen_fingerprints:
                return
            self.seen_fingerprints.add(fingerprint)

            html = await page.content()

            hostname = urlparse(url).hostname or "unknown"
            # Sanitize hostname for filename
            hostname = hostname.replace(".", "_")[:30]
            fp_short = fingerprint[:8]
            filename = f"step{step:04d}_{hostname}_{fp_short}.html"
            filepath = self.pages_dir / filename

            filepath.write_text(html, encoding="utf-8")
            size_bytes = filepath.stat().st_size

            record = {
                "ts": utc_now_iso(),
                "run_id": self.run_id,
                "step": step,
                "fingerprint": fingerprint,
                "url": url,
                "title": title,
                "filename": filename,
                "size_bytes": size_bytes,
            }
            with self.manifest_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            logger.debug(
                "page_saved step=%s fingerprint=%s filename=%s size=%s",
                step,
                fp_short,
                filename,
                size_bytes,
            )
        except Exception:
            logger.debug("Failed to capture page HTML", exc_info=True)
