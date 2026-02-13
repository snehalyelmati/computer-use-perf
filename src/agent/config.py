from datetime import datetime

_now = datetime.now()
LOG_DIR = f"logs/{_now.strftime('%Y-%m-%d')}"
_run_ts = _now.strftime('%H%M%S')
LOG_FILE = f"{LOG_DIR}/agent_{_run_ts}.log"
VERBOSE_LOG_FILE = f"{LOG_DIR}/agent_verbose_{_run_ts}.log"
STUCK_THRESHOLD = 5
FAILURE_RESET_THRESHOLD = 3
REPETITION_WINDOW = 2
MODEL_NAME = "qwen/qwen3-32b"
ORACLE_MODEL = "qwen/qwen3-32b"
REASONING_EFFORT = "none"
ACTION_MODEL_NAME = "meta-llama/llama-4-scout-17b-16e-instruct"
FILTER_MODEL_NAME = "llama-3.1-8b-instant"
MAX_BATCH_SIZE = 8
ACTION_DELAY = 0.075
DEFAULT_BASE_URL = "https://serene-frangipane-7fd25b.netlify.app"
