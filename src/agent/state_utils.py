import hashlib

def compute_state_hash(url: str, elements: list) -> str:
    """Hash of current page state for change detection."""
    el_sig = "|".join(f"{e['tag']}:{e['text'][:10]}" for e in elements[:20])
    return hashlib.md5(f"{url}|{el_sig}".encode()).hexdigest()[:8]
