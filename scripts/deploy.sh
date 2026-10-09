#!/usr/bin/env bash
#
# Deploy the Codebase Welcome Guide: one SAM stack, one Lambda, one public URL.
#
#   scripts/deploy.sh
#
# What it does, in order:
#
#   1. Refuses to run unless the preflight gate is passed and still fresh
#      (Requirement 1 AC7). `.preflight-ok` must exist and be newer than
#      infra/template.yaml -- an edited template has not been through the gate.
#   2. Reads the Target_Region and the model id out of that marker, so the id
#      the IAM policy authorizes is the id the preflight actually confirmed.
#   3. Reads alarm_email out of config.json (Requirement 8 AC3).
#   4. Checks that the three Chat_UI files exist, because they ship inside the
#      deployment package -- deploying without them serves a 404 page.
#   5. sam build (the Makefile target packages src/agent/ plus ui/), then
#      sam deploy --no-confirm-changeset.
#   6. Prints the Function URL.
#   7. Polls the SNS topic and refuses to report full success while the
#      subscription still reads PendingConfirmation (task 9.2).
#
# Exit codes:
#
#   0  deployed, and the billing alarm subscription is confirmed
#   1  refused to deploy, or the build or the deploy failed
#   3  deployed and reachable, but the SNS subscription is unconfirmed, so the
#      billing alarm cannot notify anyone. One click fixes it; see the message.
#
# Environment escapes, both off by default:
#
#   ALLOW_MISSING_UI=1   deploy without the Chat_UI files (API-only smoke test)
#   SNS_WAIT_ATTEMPTS=6  how many times to poll for the subscription
#   SNS_WAIT_SECONDS=10  seconds between polls
#
# This script creates real AWS resources and is the only supported way to do
# it. Task 9.1 owns running it.

set -euo pipefail

PYTHON="${PYTHON:-python3}"
SNS_WAIT_ATTEMPTS="${SNS_WAIT_ATTEMPTS:-6}"
SNS_WAIT_SECONDS="${SNS_WAIT_SECONDS:-10}"
ALLOW_MISSING_UI="${ALLOW_MISSING_UI:-}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd -- "${REPO_ROOT}"

MARKER="${REPO_ROOT}/.preflight-ok"
TEMPLATE="${REPO_ROOT}/infra/template.yaml"
SAMCONFIG="${REPO_ROOT}/infra/samconfig.toml"
CONFIG_JSON="${REPO_ROOT}/config.json"
BUILT_TEMPLATE="${REPO_ROOT}/.aws-sam/build/template.yaml"
UI_DIR="${REPO_ROOT}/ui"

# The region the billing alarm needs. Not a preference: AWS/Billing publishes
# EstimatedCharges in us-east-1 and nowhere else, and the alarm shares this
# stack. See the header note in infra/template.yaml.
REQUIRED_REGION="us-east-1"

die() {
  printf '\n[fail] %s\n' "$1" >&2
  shift
  for line in "$@"; do printf '       %s\n' "${line}" >&2; done
  exit 1
}

step() { printf '\n== %s\n' "$1"; }
note() { printf '       %s\n' "$1"; }

# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

for tool in sam aws "${PYTHON}"; do
  command -v -- "${tool}" >/dev/null 2>&1 || die \
    "${tool} is not on PATH." \
    "sam:  https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html" \
    "aws:  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
done

# --------------------------------------------------------------------------
# JSON and TOML readers. python3 is already a hard dependency of this project,
# so neither needs jq, and neither can be fooled by a value containing a quote.
# --------------------------------------------------------------------------

json_field() { # $1 = file, $2 = key -> trimmed string value, or empty
  "${PYTHON}" - "$1" "$2" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
except Exception as exc:  # unreadable or malformed: report, do not traceback
    sys.stderr.write(f"cannot read {path}: {exc}\n")
    raise SystemExit(1)
value = raw.get(key) if isinstance(raw, dict) else None
print(value.strip() if isinstance(value, str) else "")
PY
}

samconfig_value() { # $1 = command section, $2 = key -> value, global as fallback
  "${PYTHON}" - "${SAMCONFIG}" "$1" "$2" <<'PY'
import sys
import tomllib

path, section, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
except Exception as exc:
    sys.stderr.write(f"cannot read {path}: {exc}\n")
    raise SystemExit(1)


def params(name: str) -> dict:
    node = data.get("default", {}).get(name, {})
    node = node.get("parameters", {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


value = params(section).get(key, params("global").get(key))
print("" if value is None else str(value).strip())
PY
}

# --------------------------------------------------------------------------
# 1. The preflight gate (Requirement 1 AC7)
# --------------------------------------------------------------------------

step "Preflight gate"

[[ -f "${MARKER}" ]] || die \
  "No .preflight-ok marker, so AWS access and Nova Lite entitlement are unverified." \
  "Run the gate first:  ${PYTHON} preflight/preflight.py" \
  "It writes .preflight-ok on success (Requirement 1 AC7)."

[[ -f "${TEMPLATE}" ]] || die "infra/template.yaml is missing."

if [[ "${TEMPLATE}" -nt "${MARKER}" ]]; then
  die \
    "infra/template.yaml is newer than .preflight-ok." \
    "The template changed after the last gate run, so nothing has verified" \
    "that what it now asks for is available on this account." \
    "Re-run:  ${PYTHON} preflight/preflight.py"
fi

REGION="$(json_field "${MARKER}" region)"
MODEL_ID="$(json_field "${MARKER}" model_id)"

[[ -n "${REGION}" ]] || die "No region in .preflight-ok. Re-run the preflight."
[[ -n "${MODEL_ID}" ]] || die "No model_id in .preflight-ok. Re-run the preflight."

note "marker region:   ${REGION}"
note "marker model id: ${MODEL_ID}"

if [[ "${REGION}" != "${REQUIRED_REGION}" ]]; then
  die \
    "The preflight confirmed ${REGION}, but this stack is ${REQUIRED_REGION} only." \
    "The billing alarm shares this template, and AWS/Billing publishes" \
    "EstimatedCharges in ${REQUIRED_REGION} and nowhere else -- in ${REGION} it" \
    "would sit in INSUFFICIENT_DATA forever and notify nobody." \
    "To deploy elsewhere, split the SNS topic, the subscription, and the alarm" \
    "into their own ${REQUIRED_REGION} stack first (design.md -> Lean Deployment" \
    "Revision says the same thing)."
fi

# An inference-profile id (`us.amazon.nova-lite-v1:0`) is a different kind of
# ARN than a foundation model, so the IAM policy in the template would not
# authorize it. Fail here with the reason rather than at the first inference
# with an AccessDeniedException.
if [[ "${MODEL_ID}" == *.*.* ]]; then
  die \
    "${MODEL_ID} looks like a cross-region inference profile, not a foundation model." \
    "The template scopes bedrock:InvokeModel to" \
    "arn:aws:bedrock:${REQUIRED_REGION}::foundation-model/\${ModelId}, which an" \
    "inference profile id does not match." \
    "Task 1.4 confirmed amazon.nova-lite-v1:0 on this account. If the profile" \
    "id is genuinely the only one available, the policy needs a second" \
    "statement for arn:aws:bedrock:<region>:<account>:inference-profile/<id>."
fi

DEPLOY_REGION="$(samconfig_value deploy region)"
if [[ -n "${DEPLOY_REGION}" && "${DEPLOY_REGION}" != "${REGION}" ]]; then
  die \
    "infra/samconfig.toml deploys to ${DEPLOY_REGION}, but the preflight confirmed ${REGION}." \
    "Fix one of them so the stack lands where the gate actually ran."
fi

STACK_NAME="$(samconfig_value deploy stack_name)"
[[ -n "${STACK_NAME}" ]] || die \
  "No stack_name in infra/samconfig.toml." \
  "Expected it under [default.global.parameters]."

# --------------------------------------------------------------------------
# 2. Builder configuration
# --------------------------------------------------------------------------

step "Configuration"

[[ -f "${CONFIG_JSON}" ]] || die \
  "config.json is missing." \
  "Copy the template and fill in the three fields:" \
  "  cp config.example.json config.json"

ALARM_EMAIL="$(json_field "${CONFIG_JSON}" alarm_email)"
[[ -n "${ALARM_EMAIL}" ]] || die \
  "alarm_email is empty in config.json." \
  "Requirement 8 AC3 wants billing alarm notifications delivered to a" \
  "Builder-confirmed address, and the stack cannot subscribe nothing."

note "stack:        ${STACK_NAME}"
note "region:       ${REGION}"
note "alarm email:  ${ALARM_EMAIL}"

# --------------------------------------------------------------------------
# 3. Chat_UI assets
#
# They ship inside the deployment package (the Makefile copies ui/ to
# agent/ui/ in the artifact), so a missing file is a 404 on the live site
# rather than a build error. Check before spending a deploy on it.
# --------------------------------------------------------------------------

step "Chat_UI assets"

missing=()
for asset in index.html styles.css app.js; do
  [[ -f "${UI_DIR}/${asset}" ]] || missing+=("ui/${asset}")
done

if ((${#missing[@]} > 0)); then
  if [[ -n "${ALLOW_MISSING_UI}" ]]; then
    note "WARNING: missing ${missing[*]} -- deploying anyway (ALLOW_MISSING_UI set)."
    note "         Those paths will 404 on the deployed URL."
  else
    die \
      "Missing Chat_UI files: ${missing[*]}" \
      "They are served from inside the Lambda package, so deploying without" \
      "them puts a 404 behind the public link. Tasks 8.1 and 8.2 write them." \
      "To deploy the API alone anyway:  ALLOW_MISSING_UI=1 scripts/deploy.sh"
  fi
else
  note "index.html, styles.css, app.js present"
fi

# --------------------------------------------------------------------------
# 4. Build
# --------------------------------------------------------------------------

step "sam build"

sam build \
  --template-file "${TEMPLATE}" \
  --config-file "${SAMCONFIG}" \
  --config-env default

[[ -f "${BUILT_TEMPLATE}" ]] || die \
  "sam build reported success but ${BUILT_TEMPLATE} does not exist."

# --------------------------------------------------------------------------
# 5. Deploy
#
# No --template-file here on purpose: sam deploy picks up
# .aws-sam/build/template.yaml, which is the built artifact. Pointing it back
# at the source template would re-package CodeUri: ../ -- the whole repository,
# with no dependencies installed and no ui/ copied in.
# --------------------------------------------------------------------------

step "sam deploy"

sam deploy \
  --config-file "${SAMCONFIG}" \
  --config-env default \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
  "ModelId=${MODEL_ID}" \
  "TargetRegion=${REGION}" \
  "AlarmEmail=${ALARM_EMAIL}"

# --------------------------------------------------------------------------
# 6. Outputs
# --------------------------------------------------------------------------

step "Stack outputs"

stack_output() { # $1 = OutputKey
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

FUNCTION_URL="$(stack_output FunctionUrl)"
TOPIC_ARN="$(stack_output AlarmTopicArn)"
LOG_GROUP="$(stack_output LogGroupName)"

[[ -n "${FUNCTION_URL}" && "${FUNCTION_URL}" != "None" ]] || die \
  "The stack deployed but has no FunctionUrl output." \
  "Check the stack events:" \
  "  aws cloudformation describe-stack-events --stack-name ${STACK_NAME} --region ${REGION}"

printf '\n  Public URL:  %s\n' "${FUNCTION_URL}"
printf '  Log group:   %s\n' "${LOG_GROUP}"
printf '  Alarm topic: %s\n' "${TOPIC_ARN}"

# --------------------------------------------------------------------------
# 7. The one remaining manual step (task 9.2)
#
# An unconfirmed subscription delivers nothing, so the billing alarm would be
# decoration. Requirement 8 AC3 says Builder-confirmed, and only a human
# clicking a link in their inbox can do that.
# --------------------------------------------------------------------------

step "Billing alarm subscription"

pending_count() {
  aws sns list-subscriptions-by-topic \
    --topic-arn "${TOPIC_ARN}" \
    --region "${REGION}" \
    --query "length(Subscriptions[?SubscriptionArn=='PendingConfirmation'])" \
    --output text
}

confirmed_count() {
  aws sns list-subscriptions-by-topic \
    --topic-arn "${TOPIC_ARN}" \
    --region "${REGION}" \
    --query "length(Subscriptions[?SubscriptionArn!='PendingConfirmation'])" \
    --output text
}

attempt=1
while :; do
  confirmed="$(confirmed_count || echo 0)"
  pending="$(pending_count || echo 0)"

  if [[ "${confirmed}" != "0" ]]; then
    note "confirmed: ${confirmed} subscription(s) on the topic"
    break
  fi

  if ((attempt >= SNS_WAIT_ATTEMPTS)); then
    cat <<EOF

  ==========================================================================
  DEPLOYED, BUT NOT FINISHED.

  The agent is live at:

      ${FUNCTION_URL}

  The billing alarm subscription for ${ALARM_EMAIL} still reads
  PendingConfirmation (${pending} pending, 0 confirmed), so the 10 USD alarm
  would fire into nothing. This topic carries both alarms a human ever sees --
  the account's 10-execution concurrency ceiling bounds the rate but reports
  nothing, so until this is confirmed neither the invocation-rate alarm nor
  the bill reaches you.

  One click, then this script reports success:

      1. Open the "AWS Notification - Subscription Confirmation" email
         sent to ${ALARM_EMAIL}
      2. Click the confirmation link
      3. Re-run scripts/deploy.sh, or just verify directly:

           aws sns list-subscriptions-by-topic \\
             --topic-arn ${TOPIC_ARN} \\
             --region ${REGION}

  Then prove delivery once (task 9.2):

           aws sns publish --topic-arn ${TOPIC_ARN} \\
             --region ${REGION} --subject "test" --message "delivery check"
  ==========================================================================

EOF
    exit 3
  fi

  note "waiting for confirmation (${attempt}/${SNS_WAIT_ATTEMPTS}), ${pending} pending..."
  sleep "${SNS_WAIT_SECONDS}"
  ((attempt += 1))
done

cat <<EOF

  ==========================================================================
  DEPLOYED.

      ${FUNCTION_URL}

  Record that as the public deployed URL (Requirement 9 AC1) and exercise it
  end to end: the page loads, an orientation arrives, one follow-up answers,
  and one deliberate failure renders as a chat message (task 9.1).

  Still Builder work: check that the alarm has left INSUFFICIENT_DATA after
  the first EstimatedCharges window, which can be six hours out (task 9.2).
  ==========================================================================

EOF
