"""Debug script to verify the watch_for_text tool across common patterns.

Covers: immediate find, delayed mutations (childList + characterData), nested
leaf bubbling, non-leaf miss, case sensitivity, timeout, empty text, iframe
limitation, shadow DOM limitation, timeout clamping, whitespace-only text,
partial substring match, SKIP-tag filtering, multiple-match ordering, and
whitespace trimming.
"""

import asyncio
import time
from types import SimpleNamespace

from playwright.async_api import async_playwright

from src.agent.tools.semantic import watch_for_text


# ---------------------------------------------------------------------------
# Minimal context — watch_for_text only touches .page, .last_tool, .last_element_id
# ---------------------------------------------------------------------------

def _make_ctx(page):
    return SimpleNamespace(page=page, last_tool=None, last_element_id=None)


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS = [
    # 1 — Immediate leaf text
    {
        "label": "immediate_leaf",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="leaf">Click TARGET</button>
          <script>
            document.getElementById('leaf').addEventListener('click', () => {
              document.title = 'clicked:leaf';
            });
          </script>
        </body></html>""",
        "text": "TARGET",
        "timeout_ms": 1000,
        "expect_ok": True,
        "expect_title": "clicked:leaf",
        "expect_msg": [],
    },
    # 2 — Delayed text via new element (childList mutation)
    {
        "label": "delayed_childList",
        "html": """
        <!DOCTYPE html><html><body>
          <div id="slot"></div>
          <script>
            setTimeout(() => {
              const btn = document.createElement('button');
              btn.id = 'later';
              btn.textContent = 'Click LATER';
              btn.addEventListener('click', () => { document.title = 'clicked:later'; });
              document.getElementById('slot').appendChild(btn);
            }, 300);
          </script>
        </body></html>""",
        "text": "LATER",
        "timeout_ms": 2000,
        "expect_ok": True,
        "expect_title": "clicked:later",
        "expect_msg": [],
    },
    # 3 — Delayed text via characterData mutation
    {
        "label": "delayed_characterData",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="morph">placeholder</button>
          <script>
            document.getElementById('morph').addEventListener('click', () => {
              document.title = 'clicked:morph';
            });
            setTimeout(() => {
              document.getElementById('morph').textContent = 'Click MORPHED';
            }, 300);
          </script>
        </body></html>""",
        "text": "MORPHED",
        "timeout_ms": 2000,
        "expect_ok": True,
        "expect_title": "clicked:morph",
        "expect_msg": [],
    },
    # 4 — Nested leaf click bubbles to parent handler
    {
        "label": "nested_leaf_bubbles",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="outer"><span><strong>Deep TARGET</strong></span></button>
          <script>
            document.getElementById('outer').addEventListener('click', () => {
              document.title = 'clicked:outer';
            });
          </script>
        </body></html>""",
        "text": "TARGET",
        "timeout_ms": 1000,
        "expect_ok": True,
        "expect_title": "clicked:outer",
        "expect_msg": [],
    },
    # 5 — Non-leaf text is missed (parent has children → not a leaf)
    {
        "label": "non_leaf_missed",
        "html": """
        <!DOCTYPE html><html><body>
          <div id="parent">parent TARGET<span>child</span></div>
          <script>
            document.getElementById('parent').addEventListener('click', () => {
              document.title = 'clicked:parent';
            });
          </script>
        </body></html>""",
        "text": "parent TARGET",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 6 — Case sensitivity ("TARGET" vs "target")
    {
        "label": "case_sensitive",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="lower">Click target</button>
          <script>
            document.getElementById('lower').addEventListener('click', () => {
              document.title = 'clicked:lower';
            });
          </script>
        </body></html>""",
        "text": "TARGET",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 7 — Timeout path (non-existent text)
    {
        "label": "timeout_nonexistent",
        "html": """
        <!DOCTYPE html><html><body>
          <p>Nothing special here</p>
        </body></html>""",
        "text": "NONEXISTENT",
        "timeout_ms": 500,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 8 — Empty text validation
    {
        "label": "empty_text",
        "html": """
        <!DOCTYPE html><html><body>
          <p>Some text</p>
        </body></html>""",
        "text": "",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["empty"],
    },
    # 9 — Iframe limitation (text inside iframe is not reachable)
    {
        "label": "iframe_limitation",
        "html": """
        <!DOCTYPE html><html><body>
          <iframe srcdoc="<!DOCTYPE html><html><body>
            <button id='inside'>Click IFRAME</button>
            <script>
              document.getElementById('inside').addEventListener('click', () => {
                document.title = 'clicked:iframe';
              });
            </script>
          </body></html>"></iframe>
        </body></html>""",
        "text": "IFRAME",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 10 — Shadow DOM limitation (text inside shadow root is not reachable)
    {
        "label": "shadow_dom_limitation",
        "html": """
        <!DOCTYPE html><html><body>
          <div id="host"></div>
          <script>
            const host = document.getElementById('host');
            const root = host.attachShadow({ mode: 'open' });
            const btn = document.createElement('button');
            btn.textContent = 'Click SHADOW';
            btn.addEventListener('click', () => { document.title = 'clicked:shadow'; });
            root.appendChild(btn);
          </script>
        </body></html>""",
        "text": "SHADOW",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 11 — Timeout clamping (tested separately via timing assertions)
    # 12 — Whitespace-only text rejected (distinct from empty string)
    {
        "label": "whitespace_only_text",
        "html": """
        <!DOCTYPE html><html><body>
          <p>Some text</p>
        </body></html>""",
        "text": "   ",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["empty"],
    },
    # 13 — Partial/substring match (JS .includes() is substring, not exact)
    {
        "label": "partial_substring_match",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="sub">Click TARGET_FULL</button>
          <script>
            document.getElementById('sub').addEventListener('click', () => {
              document.title = 'clicked:sub';
            });
          </script>
        </body></html>""",
        "text": "TARGET",
        "timeout_ms": 1000,
        "expect_ok": True,
        "expect_title": "clicked:sub",
        "expect_msg": [],
    },
    # 14 — Text inside SKIP tags (script/style) is ignored
    {
        "label": "skip_tags_ignored",
        "html": """
        <!DOCTYPE html><html><body>
          <style>.x { content: 'HIDDEN_TEXT'; }</style>
          <script>var x = 'HIDDEN_TEXT';</script>
          <p>Nothing here</p>
        </body></html>""",
        "text": "HIDDEN_TEXT",
        "timeout_ms": 1000,
        "expect_ok": False,
        "expect_title": "",
        "expect_msg": ["timeout"],
    },
    # 15 — Multiple matches: first in DOM order gets clicked
    {
        "label": "multiple_matches_first_wins",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="first">Click DUPE</button>
          <button id="second">Click DUPE</button>
          <script>
            document.getElementById('first').addEventListener('click', () => {
              document.title = 'clicked:first';
            });
            document.getElementById('second').addEventListener('click', () => {
              document.title = 'clicked:second';
            });
          </script>
        </body></html>""",
        "text": "DUPE",
        "timeout_ms": 1000,
        "expect_ok": True,
        "expect_title": "clicked:first",
        "expect_msg": [],
    },
    # 16 — Whitespace trimming: padded textContent still matches
    {
        "label": "whitespace_trimming",
        "html": """
        <!DOCTYPE html><html><body>
          <button id="padded">   PADDED   </button>
          <script>
            document.getElementById('padded').addEventListener('click', () => {
              document.title = 'clicked:padded';
            });
          </script>
        </body></html>""",
        "text": "PADDED",
        "timeout_ms": 1000,
        "expect_ok": True,
        "expect_title": "clicked:padded",
        "expect_msg": [],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_all(message: str, substrings: list[str]) -> bool:
    low = message.lower()
    return all(s.lower() in low for s in substrings)


async def _run_one(label, html, text, timeout_ms, expect_ok, expect_title, expect_msg):
    """Run a single scenario, return (label, passed, detail)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        await page.wait_for_load_state("domcontentloaded")

        ctx = _make_ctx(page)
        result = await watch_for_text(text, ctx, timeout_ms=timeout_ms)

        title = await page.title()
        await browser.close()

    ok_match = result.ok == expect_ok
    title_match = (title == expect_title) if expect_title else True
    msg_match = _has_all(result.message, expect_msg)
    passed = ok_match and title_match and msg_match

    detail = f"ok={result.ok}  title={title!r}  msg={result.message!r}"
    return label, passed, detail


async def _run_clamping():
    """Scenario 11: verify timeout clamping via wall-clock timing."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # --- low clamp: 100ms request → clamped to 500ms ---
        page_lo = await browser.new_page()
        await page_lo.set_content("<!DOCTYPE html><html><body><p>x</p></body></html>")
        await page_lo.wait_for_load_state("domcontentloaded")

        t0 = time.monotonic()
        result_lo = await watch_for_text("NOPE", _make_ctx(page_lo), timeout_ms=100)
        elapsed_lo = (time.monotonic() - t0) * 1000
        await page_lo.close()

        # --- high clamp: 99999ms request → clamped to 10000ms ---
        page_hi = await browser.new_page()
        await page_hi.set_content("<!DOCTYPE html><html><body><p>x</p></body></html>")
        await page_hi.wait_for_load_state("domcontentloaded")

        t0 = time.monotonic()
        result_hi = await watch_for_text("NOPE", _make_ctx(page_hi), timeout_ms=99_999)
        elapsed_hi = (time.monotonic() - t0) * 1000
        await page_hi.close()

        await browser.close()

    # Allow generous margin for JS overhead
    lo_pass = (
        not result_lo.ok
        and "500ms" in result_lo.message
        and 400 < elapsed_lo < 1500
    )
    hi_pass = (
        not result_hi.ok
        and "10000ms" in result_hi.message
        and 9000 < elapsed_hi < 12000
    )
    passed = lo_pass and hi_pass
    detail = (
        f"lo: {elapsed_lo:.0f}ms msg={result_lo.message!r} | "
        f"hi: {elapsed_hi:.0f}ms msg={result_hi.message!r}"
    )
    return "timeout_clamping", passed, detail


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    results: list[tuple[str, bool, str]] = []

    print("\n--- watch_for_text debug (16 scenarios) ---\n")

    # Scenarios 1-10
    for s in SCENARIOS:
        label, passed, detail = await _run_one(
            s["label"],
            s["html"],
            s["text"],
            s["timeout_ms"],
            s["expect_ok"],
            s["expect_title"],
            s["expect_msg"],
        )
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {label:30s} {detail}")
        results.append((label, passed, detail))

    # Scenario 11 — clamping (takes ~10.5s due to high-clamp timeout)
    print("\n  (running timeout_clamping — ~10s for high-clamp path) ...")
    label, passed, detail = await _run_clamping()
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {label:30s} {detail}")
    results.append((label, passed, detail))

    # Summary
    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"\n--- Summary: {passed_count}/{total} passed ---\n")
    for label, passed, _ in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    print()
    if passed_count == total:
        print("ALL PASSED")
    else:
        print("SOME FAILED")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
