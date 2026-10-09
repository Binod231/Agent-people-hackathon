"""Every limit, budget, and deadline the agent enforces, in one place.

Each constant names the acceptance criterion it implements. Tests assert these
values against the requirement numbers directly, so a tuning change that breaks
a requirement fails a test rather than shipping quietly.

Constants annotated "design" are tuning values this design chose rather than
numbers an acceptance criterion names. They live here so there is exactly one
place to change them, and each cites the criterion it serves.

Nothing in this module imports anything. It is the leaf of the dependency graph.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Repository ingestion caps (Requirement 3)
# ---------------------------------------------------------------------------

MAX_TREE_ENTRIES = 200  # Requirement 3 AC2 -- entries in the tree summary
MAX_TREE_DEPTH = 1  # Requirement 3 AC2 -- "first two levels" == at most one "/"
MAX_SELECTED_FILES = 12  # Requirement 3 AC3
MAX_FILE_CHARS = 8_000  # Requirement 3 AC4 -- characters, not bytes
TRUNCATE_NEWLINE_WINDOW = 0.10  # design: tail of the cap searched for a newline (3 AC4)
MAX_HISTORY_TURNS = 10  # Requirement 3 AC7
MAX_REPO_SIZE_KB = 102_400  # Requirement 3 AC8 -- 100 MB; GitHub reports KILOBYTES
MAX_REPO_SIZE_MB = 100  # Requirement 3 AC8 -- the figure the user-facing copy names
MAX_TREE_FILES = 5_000  # Requirement 3 AC9 -- blobs on the default branch
MAX_FILE_BYTES = 500 * 1024  # Requirement 3 AC10 -- per-file size skip

# ---------------------------------------------------------------------------
# Token accounting (Requirement 3 AC6)
# ---------------------------------------------------------------------------

TOKEN_BUDGET = 30_000  # Requirement 3 AC6 -- the contractual input cap
TOKEN_BUDGET_EFFECTIVE = 27_000  # design: 10% margin against estimator error (3 AC6)
CHARS_PER_TOKEN = 3.5  # design: pessimistic estimate; code tokenizes denser than prose
BLOCK_OVERHEAD_TOKENS = 8  # design: message framing charged per content block
MIN_USEFUL_CHARS = 400  # design: below this a shortened block is dropped instead
RESERVE_SYSTEM = 800  # design: system prompt allowance inside TOKEN_BUDGET_EFFECTIVE
RESERVE_HISTORY = 4_000  # design: conversation history allowance (serves 3 AC7)
BUDGET_TREE = 1_500  # design: directory tree summary allowance (serves 3 AC2)

# ---------------------------------------------------------------------------
# Model call and response shape (Requirements 4, 6)
# ---------------------------------------------------------------------------

MAX_RESPONSE_WORDS = 350  # Requirement 4 AC4
CAP_SENTENCE_WINDOW = 0.25  # design: tail searched for a sentence end (4 AC4)
MAX_OUTPUT_TOKENS = 600  # design: ~350 words of headroom for Requirement 4 AC4
MODEL_TEMPERATURE = 0.3  # design: grounded, not creative (serves 4 AC1)
MODEL_TOP_P = 0.9  # design
MAX_BEDROCK_RETRIES = 2  # Requirement 6 AC2 -- initial attempt plus two retries
BASE_BACKOFF_SECONDS = 0.5  # design: exponential base for Requirement 6 AC2
BACKOFF_JITTER_SECONDS = 0.25  # design: jitter ceiling added to each backoff
MIN_INFERENCE_SECONDS = 6.0  # design: window a Bedrock attempt needs (4 AC6, 5 AC6)

# ---------------------------------------------------------------------------
# Orientation_Message (Requirement 5)
# ---------------------------------------------------------------------------

ORIENTATION_FILE_COUNT = 4  # design: lean context tier for Requirement 5 AC6
ORIENTATION_FILE_CHARS = 3_000  # design: per-file chars in the lean tier (5 AC6)
ORIENTATION_MIN_PATHS = 3  # Requirement 5 AC4
ORIENTATION_MIN_QUESTIONS = 2  # Requirement 5 AC5
ORIENTATION_TARGET_WORDS = 250  # design: under MAX_RESPONSE_WORDS so trimming is rare

# ---------------------------------------------------------------------------
# Deadline layering (Requirements 4 AC6, 5 AC6, 8 AC4)
#
# Ordered innermost first. Three limits, not four: the Lambda Function URL
# replaced API Gateway, so there is no 29-second integration ceiling to sit
# inside any more. The soft deadlines stay because Requirement 4 AC6 promises a
# response inside 30 s and Requirement 5 AC6 an orientation inside 20 s -- those
# are commitments to the user, not workarounds for a service limit.
# ---------------------------------------------------------------------------

ORIENTATION_DEADLINE_MS = 18_000  # Requirement 5 AC6 -- 20 s minus margin
SOFT_DEADLINE_MS = 26_000  # Requirement 4 AC6 -- 30 s minus margin
LAMBDA_TIMEOUT_SECONDS = 60  # Requirement 8 AC4 -- infra/template.yaml Timeout

# ---------------------------------------------------------------------------
# Transport (Requirements 2, 8 AC1)
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 8_192  # Requirement 8 AC1 -- measured on encoded bytes
GITHUB_CONNECT_TIMEOUT_SECONDS = 2.0  # design (serves 4 AC6)
GITHUB_READ_TIMEOUT_SECONDS = 5.0  # design (serves 4 AC6)
RAW_FETCH_WORKERS = 8  # design: parallel raw.githubusercontent.com reads (4 AC6)
BEDROCK_CONNECT_TIMEOUT_SECONDS = 3  # design (serves 4 AC6)
BEDROCK_READ_TIMEOUT_SECONDS = 25  # design: inside SOFT_DEADLINE_MS (4 AC6)
RATE_LIMIT_WAIT_MIN_MINUTES = 1  # Requirement 2 AC7 -- clamp floor for the copy
RATE_LIMIT_WAIT_MAX_MINUTES = 60  # Requirement 2 AC7 -- clamp ceiling for the copy
RATE_LIMIT_WAIT_FALLBACK_SECONDS = 600  # Requirement 2 AC7 -- missing reset header

# ---------------------------------------------------------------------------
# Cost_Guard values (Requirement 8)
#
# Enforced in infra/template.yaml. They are named here because this module is
# where a reader looks for a limit, and because the deployed values and the
# documented ones should not be able to drift silently.
#
# The API Gateway half of the Cost_Guard is gone: no REST API means no throttle
# rate, no burst, no daily quota, and no 29-second integration timeout, so the
# four constants that carried those numbers were deleted rather than left
# unreachable. The reasoning and what it costs are in docs/out-of-scope.md
# entries 6 and 9.
#
# `LAMBDA_RESERVED_CONCURRENCY = 2` was here and is gone too, because the value
# is not deployed and a constant that names an undeployed limit is worse than no
# constant. The stack create failed on it: AWS holds 10 concurrent executions
# back as unreserved, this account's whole ConcurrentExecutions limit is 10 (the
# new-account value against a default of 1000), so no reservation of any size
# fits. See the note on AgentFunction in infra/template.yaml.
#
# Requirement 8 AC5 names an AWS Lambda reserved concurrency of 2. It is not met
# as written and this comment is not going to pretend otherwise. What stands in
# for it is ACCOUNT_CONCURRENCY_LIMIT below: the account's own ceiling bounds
# concurrency at 10 instead, which is weaker in two ways -- account-wide rather
# than a per-function guarantee, and five times the rate. The invocation alarm
# is the control added in response, and it is a notification rather than a
# bound. Entry 9 does the arithmetic.
# ---------------------------------------------------------------------------

ACCOUNT_CONCURRENCY_LIMIT = 10  # Requirement 8 AC5, by account limit not template
INVOCATION_ALARM_THRESHOLD = 175  # design: compensates for 8 AC5 being undeployable
INVOCATION_ALARM_PERIOD_SECONDS = 300  # design: window the threshold counts over
LAMBDA_MEMORY_MB = 1024  # design: cold-start budget for Requirement 5 AC6
BILLING_ALARM_USD = 10  # Requirement 8 AC2
LOG_RETENTION_DAYS = 7  # Requirement 12 AC3
