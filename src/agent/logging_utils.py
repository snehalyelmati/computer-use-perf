import time
from pathlib import Path
from .config import LOG_FILE, VERBOSE_LOG_FILE

def log(msg: str):
    """Log to both console and file."""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def log_verbose(msg: str):
    """Log to verbose file only (no stdout)."""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    Path(VERBOSE_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(VERBOSE_LOG_FILE, "a") as f:
        f.write(line + "\n")
