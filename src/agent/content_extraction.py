import re
from bs4 import BeautifulSoup
from playwright.async_api import Page

from .text_budget import extract_candidate_values


def extract_prioritized_data_attrs(soup) -> list:
    """Extract data attributes with deduplication and priority sorting.

    Prioritizes code-like values over common noise like 'unchecked'/'true'.
    """
    seen = set()
    attrs = []

    for el in soup.find_all(True):
        if not el.attrs:
            continue
        for key, val in el.attrs.items():
            if not val or not isinstance(val, str) or len(val) > 200:
                continue
            # Skip generic boolean/state noise
            if val.lower() in (
                "true",
                "false",
                "0",
                "1",
                "on",
                "off",
                "yes",
                "no",
                "checked",
                "unchecked",
                "open",
                "closed",
                "enabled",
                "disabled",
            ):
                continue
            if key.startswith("data-") or key in ("aria-label", "title", "alt"):
                attr_str = f"{key}={val}"
                if attr_str in seen:
                    continue
                seen.add(attr_str)
                # Prioritize code-like values (alphanumeric 4-10 chars)
                priority = 100 if re.match(r"^[A-Z0-9]{4,10}$", val) else 0
                # Boost priority for likely code/answer attributes
                if re.search(r"code|answer|secret|key|value|token", key, re.I):
                    priority += 50
                attrs.append((priority, attr_str))

    # Sort by priority (highest first), return all (dedup via `seen` is sufficient)
    attrs.sort(key=lambda x: -x[0])
    return [a for _, a in attrs]


async def extract_structured_content(page: Page) -> dict:
    """Extract structured content from page using BeautifulSoup."""

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    # FIRST: Extract hidden content and data attrs BEFORE removing any elements
    # This ensures we capture codes that might be inside nav/header/footer/aside

    # Extract hidden content that might contain codes/answers
    hidden_content = []
    seen_hidden_text = set()

    def _add_hidden(prefix, text):
        if text and text not in seen_hidden_text:
            seen_hidden_text.add(text)
            hidden_content.append(f"[{prefix}] {text}")

    for el in soup.find_all(attrs={"hidden": True}):
        _add_hidden("hidden", el.get_text(strip=True))
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        _add_hidden("aria-hidden", el.get_text(strip=True))
    for el in soup.find_all(
        class_=re.compile(
            r"hidden|invisible|sr-only|visually-hidden|d-none|is-hidden|hide|display-none|off-screen",
            re.I,
        )
    ):
        _add_hidden("hidden-class", el.get_text(strip=True))
    for el in soup.find_all(
        style=re.compile(
            r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0", re.I
        )
    ):
        _add_hidden("style-hidden", el.get_text(strip=True))

    # Extract candidate values from scripts BEFORE removing them.
    # This keeps the surface small (values only) while enabling challenges that stash answers in JS.
    try:
        script_vals = set()
        for script in soup.find_all("script"):
            txt = script.string or script.get_text(" ", strip=True) or ""
            if not txt:
                continue
            for v in extract_candidate_values(txt):
                script_vals.add(v)
        for v in sorted(script_vals):
            _add_hidden("script", v)
    except Exception:
        pass

    # Extract prioritized data attributes (deduplicated, codes first)
    data_attrs = extract_prioritized_data_attrs(soup)

    # THEN: Remove noise elements for text extraction
    noise_tags = [
        "script",
        "style",
        "noscript",
        "iframe",
        "nav",
        "footer",
        "header",
        "aside",
    ]
    for tag in soup.find_all(noise_tags):
        tag.decompose()

    # Remove role-based noise
    for el in soup.find_all(attrs={"role": ["banner", "navigation", "contentinfo"]}):
        el.decompose()

    # Extract structured data
    title = soup.find("h1")
    title_text = title.get_text(strip=True) if title else ""

    # Extract ALL unique text content from every element (deduped)
    seen_text = set()
    all_text = []
    for el in soup.find_all(True):
        # Get direct text only (not children's text) to avoid duplication
        direct = el.find(string=True, recursive=False)
        if direct:
            text = direct.strip()
            if len(text) >= 2 and text not in seen_text:
                seen_text.add(text)
                all_text.append(text)

    # Get full text for analysis
    full_text = soup.get_text(separator="\n", strip=True)

    return {
        "title": title_text,
        "all_text": all_text,
        "full_text": full_text,
        "hidden_content": hidden_content,
        "data_attrs": data_attrs,
        "url": page.url,
    }
