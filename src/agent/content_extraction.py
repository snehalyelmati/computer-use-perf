import re
from bs4 import BeautifulSoup
from playwright.async_api import Page

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
            if not val or not isinstance(val, str) or len(val) > 50:
                continue
            if key.startswith('data-') or key in ('aria-label', 'title', 'alt'):
                attr_str = f"{key}={val}"
                if attr_str in seen:
                    continue
                seen.add(attr_str)
                # Skip common noise values (but keep a few for context)
                if val.lower() in ('unchecked', 'checked', 'true', 'false', 'open', 'closed'):
                    # Keep at most one of each noise type for context
                    if sum(1 for _, a in attrs if val.lower() in a.lower()) >= 1:
                        continue
                # Prioritize code-like values (alphanumeric 4-10 chars)
                priority = 100 if re.match(r'^[A-Z0-9]{4,10}$', val) else 0
                # Boost priority for likely code/answer attributes
                if re.search(r'code|answer|secret|key|value|token', key, re.I):
                    priority += 50
                attrs.append((priority, attr_str))

    # Sort by priority (highest first) and return top 30
    attrs.sort(key=lambda x: -x[0])
    return [a for _, a in attrs[:30]]

async def extract_structured_content(page: Page) -> dict:
    """Extract structured content from page using BeautifulSoup."""

    html = await page.content()
    soup = BeautifulSoup(html, 'lxml')

    # FIRST: Extract hidden content and data attrs BEFORE removing any elements
    # This ensures we capture codes that might be inside nav/header/footer/aside

    # Extract hidden content that might contain codes/answers
    hidden_content = []
    for el in soup.find_all(attrs={'hidden': True}):
        text = el.get_text(strip=True)
        if text:
            hidden_content.append(f"[hidden] {text}")
    for el in soup.find_all(class_=re.compile(r'hidden|invisible|sr-only', re.I)):
        text = el.get_text(strip=True)
        if text and text not in str(hidden_content):
            hidden_content.append(f"[hidden] {text}")

    # Extract prioritized data attributes (deduplicated, codes first)
    data_attrs = extract_prioritized_data_attrs(soup)

    # THEN: Remove noise elements for text extraction
    noise_tags = ['script', 'style', 'noscript', 'iframe', 'nav', 'footer', 'header', 'aside']
    for tag in soup.find_all(noise_tags):
        tag.decompose()

    # Remove role-based noise
    for el in soup.find_all(attrs={'role': ['banner', 'navigation', 'contentinfo']}):
        el.decompose()

    # Extract structured data
    title = soup.find('h1')
    title_text = title.get_text(strip=True) if title else ""

    headings = [h.get_text(strip=True) for h in soup.find_all(['h2', 'h3', 'h4'])]

    paragraphs = []
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if text and len(text) >= 2:
            paragraphs.append(text)

    # Extract form elements with context
    forms = []
    for inp in soup.find_all(['input', 'textarea']):
        input_type = inp.get('type', 'text')
        placeholder = inp.get('placeholder', '')
        label = ''
        if inp.get('id'):
            label_el = soup.find('label', {'for': inp.get('id')})
            if label_el:
                label = label_el.get_text(strip=True)
        forms.append(f"{input_type}: {label or placeholder or 'input'}")

    # Get full text for analysis
    full_text = soup.get_text(separator='\n', strip=True)

    # Track if limits were hit
    limits_hit = []
    if len(hidden_content) > 15:
        limits_hit.append(f"hidden_content: {len(hidden_content)} -> 15")
    # data_attrs now uses extract_prioritized_data_attrs with 30 limit and dedup

    return {
        "title": title_text,
        "headings": headings[:5],
        "paragraphs": paragraphs[:10],
        "forms": forms,
        "full_text": full_text,
        "hidden_content": hidden_content[:15],
        "data_attrs": data_attrs,  # Already limited to 30 by extract_prioritized_data_attrs
        "limits_hit": limits_hit,
        "url": page.url
    }
