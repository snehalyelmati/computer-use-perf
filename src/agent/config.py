from datetime import datetime

_now = datetime.now()
LOG_DIR = f"logs/{_now.strftime('%Y-%m-%d')}"
_run_ts = _now.strftime("%H%M%S")
LOG_FILE = f"{LOG_DIR}/agent_{_run_ts}.log"
VERBOSE_LOG_FILE = f"{LOG_DIR}/agent_verbose_{_run_ts}.log"
STUCK_THRESHOLD = 5
FAILURE_RESET_THRESHOLD = 3
REPETITION_WINDOW = 2
CHALLENGE_STEP_BUDGET = 10  # Force diagnosis after this many steps on same challenge
MAX_FAILED_APPROACHES = 5  # Max entries in failed-attempt memory
ORACLE_OVERRIDE_CIRCUIT_BREAKER = (
    5  # Trigger diagnosis after this many consecutive OVERRIDEs
)

# Prompt budgeting (characters). Avoid arbitrary prefix slicing; select high-signal items within budgets.
# These defaults are intentionally generous but still bounded.
OVERVIEW_PAGE_TEXT_BUDGET_CHARS = 20000
DIAGNOSIS_PAGE_TEXT_BUDGET_CHARS = 15000
ORACLE_PAGE_TEXT_BUDGET_CHARS = 20000
ORACLE_MEMORY_BUDGET_CHARS = 3000
DIFF_BUDGET_CHARS = 5000
A11Y_TREE_BUDGET_CHARS = 8000
FAILED_ATTEMPTS_BUDGET_CHARS = 2000

# Additional bounded sections
ELEMENT_SUMMARY_BUDGET_CHARS = 12000
HIDDEN_CONTENT_BUDGET_CHARS = 4000
DATA_ATTRS_BUDGET_CHARS = 4000
MODEL_NAME = None  # Set by main.py from provider defaults / CLI
ORACLE_MODEL = None
REASONING_EFFORT = None  # None = use per-model default (see src/agent/providers.py)
PROVIDER = "cerebras"  # groq | cerebras
ACTION_MODEL_NAME = None
FILTER_MODEL_NAME = None
MAX_BATCH_SIZE = 8
ACTION_DELAY = 0.075
DEFAULT_BASE_URL = "https://serene-frangipane-7fd25b.netlify.app"

# Fixed, immutable goal for Challenge Mode (one challenge at a time).
CHALLENGE_GOAL = "Solve the current challenge by completing any prerequisites required to move to the next challenge."
