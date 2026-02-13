from datetime import datetime

_now = datetime.now()
LOG_DIR = f"logs/{_now.strftime('%Y-%m-%d')}"
_run_ts = _now.strftime('%H%M%S')
LOG_FILE = f"{LOG_DIR}/agent_{_run_ts}.log"
VERBOSE_LOG_FILE = f"{LOG_DIR}/agent_verbose_{_run_ts}.log"
STUCK_THRESHOLD = 5
FAILURE_RESET_THRESHOLD = 3
REPETITION_WINDOW = 2
CHALLENGE_STEP_BUDGET = 10  # Force diagnosis after this many steps on same challenge
MODEL_NAME = None  # Set by main.py from provider defaults / CLI
ORACLE_MODEL = None
REASONING_EFFORT = "none"
PROVIDER = "groq"  # groq | cerebras
ACTION_MODEL_NAME = None
FILTER_MODEL_NAME = None
MAX_BATCH_SIZE = 8
ACTION_DELAY = 0.075
DEFAULT_BASE_URL = "https://serene-frangipane-7fd25b.netlify.app"
