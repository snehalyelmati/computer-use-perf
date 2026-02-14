import hashlib


def _norm(s: object) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return " ".join(s.split())


def compute_state_hash(url: str, elements: list) -> str:
    """Hash of current page state for change detection."""
    parts: list[str] = []
    for e in elements or []:
        parts.append(
            "|".join(
                [
                    _norm(e.get("tag")),
                    _norm(e.get("role")),
                    _norm(e.get("name")),
                    _norm(e.get("text")),
                    _norm(e.get("type")),
                    _norm(e.get("href")),
                    _norm(e.get("state")),
                    "d" if e.get("disabled") else "",
                    "c" if e.get("checked") else "",
                    "s" if e.get("selected") else "",
                    _norm(e.get("value")),
                    _norm(e.get("dataValue")),
                ]
            )
        )
    el_sig = "\n".join(sorted(parts))
    # Use a longer digest to reduce collisions.
    return hashlib.md5(f"{url}\n{el_sig}".encode()).hexdigest()
