"""Verify AWS access and Nova Lite entitlement before anything is deployed.

Requirement 1. This is deliverable zero and the gate on every deployment task
(Requirement 1 AC7): `scripts/deploy.sh` refuses to run unless the
`.preflight-ok` marker this script writes exists and is newer than
`infra/template.yaml`.

Four checks run in order, each printing one line, and the first failure exits
non-zero with a message that *is* the remediation:

  1. region resolution  -- explicit configuration only (AC3, AC4)
  2. credentials        -- STS GetCallerIdentity (AC1)
  3. deploy permissions -- a read-only CloudFormation probe (AC1)
  4. Nova Lite          -- one real inference call (AC2, AC5)

Run it from the repository root:

    python preflight/preflight.py

Deliberately standalone. It imports nothing from `src/agent/` -- it runs before
the package is deployed or on the import path, and it restates no limit that
`agent/config.py` owns (that module holds no preflight constant). `boto3` is the
only dependency, matching the design's "no AWS SDK dependency beyond boto3".
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

DEADLINE_SECONDS = 60  # Requirement 1 AC6 -- the self-imposed pass/fail budget
PRIMARY_MODEL_ID = "amazon.nova-lite-v1:0"
FALLBACK_MODEL_ID = "us.amazon.nova-lite-v1:0"  # cross-region inference profile
MARKER = Path(".preflight-ok")

# Values that mean "the Builder did not actually choose a region". A profile
# default or a literal "none" left in config.json is exactly the silent
# inheritance Requirement 1 AC4 exists to reject.
PLACEHOLDER_REGIONS = {"none", "default", "null"}

# Bedrock error codes that mean "try the other model id", not "give up". A
# region that serves Nova only through the cross-region inference profile
# answers the direct id with one of these.
_TRY_NEXT_MODEL_CODES = {
    "AccessDeniedException",
    "ValidationException",
    "ResourceNotFoundException",
}

# Explicit timeouts, sized so the run reports inside the AC6 budget. The
# deadline is checked *between* stages, never mid-call, so the per-stage worst
# cases have to sum under 60 s on their own:
#
#   region    config file read, no network                      ~0 s
#   STS       5 connect + 10 read, no retry                      15 s
#   CFN       5 connect + 10 read, no retry                      15 s
#   Bedrock  (5 connect + 8 read) x 2 attempts + backoff        ~27 s
#                                                     total    ~57 s
#
# `retries.max_attempts` in a client Config counts *retries*: botocore adds one
# for the initial request, so 0 means one attempt and 1 means two. Nova Lite
# answers a 10-token request in about a second, so the 8-second read timeout is
# slack rather than a target, and one retry is enough to ride out a blip
# without pushing the total past the budget.
_CONTROL_PLANE_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 0, "mode": "standard"},
)
_BEDROCK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=8,
    retries={"max_attempts": 1, "mode": "standard"},
)


class PreflightError(Exception):
    """A check failed. The message is printed verbatim and is the remediation."""


@dataclass(frozen=True)
class Settings:
    region: str
    model_id: str
    alarm_email: str


def _text(value: Any) -> str:
    """Read a config field as trimmed text, treating a non-string as absent.

    `{"target_region": 1}` is a plausible hand-edit of config.json. Calling
    `.strip()` on it would exit with a traceback instead of the remediation
    message AC4 owes the reader, so a wrong-typed field reads as unset and
    takes the normal "not set" path.
    """
    return value.strip() if isinstance(value, str) else ""


def load_settings(path: Path) -> Settings:
    """Read Builder-supplied configuration. Requirement 1 AC3, AC4."""
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PreflightError(
                f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}).\n"
                f"Copy config.example.json over it and edit the three fields."
            ) from exc
        if not isinstance(raw, dict):
            raise PreflightError(
                f"{path} must contain a JSON object.\n"
                f"Copy config.example.json over it and edit the three fields."
            )

    # Precedence: CLI env override > config file. No implicit AWS_DEFAULT_REGION
    # fallback -- Requirement 1 AC4 demands the region be explicit.
    region = _text(os.environ.get("TARGET_REGION")) or _text(raw.get("target_region"))
    if not region or region.lower() in PLACEHOLDER_REGIONS:
        raise PreflightError(
            "target_region is not set.\n"
            "The deployment region must be confirmed explicitly, not inherited "
            "from an AWS profile.\n"
            f"Set it in {path} as {{\"target_region\": \"us-east-1\"}} "
            "or export TARGET_REGION."
        )
    return Settings(
        region=region,
        model_id=_text(raw.get("model_id")) or PRIMARY_MODEL_ID,
        alarm_email=_text(raw.get("alarm_email")),
    )


def _client(service: str, region: str, config: Config) -> Any:
    """One place where boto3 clients are built, so tests can inject fakes."""
    return boto3.client(service, region_name=region, config=config)


def check_credentials(region: str, client: Any | None = None) -> str:
    """Requirement 1 AC1."""
    sts = client or _client("sts", region, _CONTROL_PLANE_CONFIG)
    try:
        ident = sts.get_caller_identity()
    except NoCredentialsError as exc:
        raise PreflightError(
            "No AWS credentials resolved.\n"
            "Run 'aws configure' or export AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY, then re-run."
        ) from exc
    except ClientError as exc:
        raise PreflightError(
            "AWS rejected the credentials: "
            f"{exc.response['Error']['Code']}.\n"
            "Confirm the account is active and the keys are not disabled."
        ) from exc
    except BotoCoreError as exc:
        raise PreflightError(
            f"Could not reach AWS STS in {region} ({type(exc).__name__}).\n"
            f"Check network reachability and that {region} is a real region name."
        ) from exc
    return ident["Arn"]


def check_deploy_permissions(region: str, client: Any | None = None) -> None:
    """Requirement 1 AC1 -- 'credentials with deployment permissions'.

    A dry-run style probe: listing stacks requires the same read grant that
    every later deploy needs, and fails fast on a read-only or scoped-down
    principal without creating anything.
    """
    cfn = client or _client("cloudformation", region, _CONTROL_PLANE_CONFIG)
    try:
        cfn.list_stacks(StackStatusFilter=["CREATE_COMPLETE"])
    except ClientError as exc:
        raise PreflightError(
            "The credentials cannot call CloudFormation in "
            f"{region} ({exc.response['Error']['Code']}).\n"
            "Deployment needs CloudFormation, Lambda, API Gateway, S3, "
            "CloudFront, IAM and Logs permissions. Attach a policy that "
            "grants them, or use an administrative principal for this build."
        ) from exc
    except BotoCoreError as exc:
        raise PreflightError(
            f"Could not reach CloudFormation in {region} ({type(exc).__name__}).\n"
            f"Check network reachability and that {region} is a real region name."
        ) from exc


def check_nova_lite(
    region: str, model_id: str, client: Any | None = None
) -> tuple[str, str]:
    """One real inference call. Requirement 1 AC2, AC5.

    Tries the configured model id, then the cross-region inference profile.
    Some regions serve Nova only through the profile id, and discovering that
    here costs one second instead of an hour. The winning id is what the IAM
    policy in `infra/template.yaml` must authorize, so it is returned and
    recorded in the marker file rather than guessed at deploy time.
    """
    runtime = client or _client("bedrock-runtime", region, _BEDROCK_CONFIG)
    attempts = [model_id] + (
        [FALLBACK_MODEL_ID] if model_id != FALLBACK_MODEL_ID else []
    )
    last: ClientError | None = None

    for candidate in attempts:
        try:
            response = runtime.converse(
                modelId=candidate,
                messages=[{"role": "user", "content": [{"text": "Reply with: ok"}]}],
                inferenceConfig={"maxTokens": 10, "temperature": 0.0},
            )
            text = response["output"]["message"]["content"][0]["text"]
            return candidate, text.strip()
        except ClientError as exc:
            last = exc
            code = exc.response["Error"]["Code"]
            if code in _TRY_NEXT_MODEL_CODES:
                continue  # try the profile id
            raise PreflightError(
                f"Bedrock call failed with {code} in {region}.\n"
                "Check that Bedrock is available in this region and retry."
            ) from exc
        except BotoCoreError as exc:
            raise PreflightError(
                f"Could not reach Bedrock in {region} ({type(exc).__name__}).\n"
                "Check network reachability and that Bedrock is available in "
                "this region."
            ) from exc

    raise PreflightError(
        f"Amazon Nova Lite is not available to this account in {region}.\n"
        f"Tried model ids: {', '.join(attempts)}.\n"
        "To request access:\n"
        "  1. Open the Amazon Bedrock console in "
        f"{region}.\n"
        "  2. Go to Model access (left nav, under Bedrock configurations).\n"
        "  3. Choose Modify model access, select Amazon Nova Lite, and submit.\n"
        "  4. Amazon Nova approvals are usually immediate; re-run this script "
        "once the status reads Access granted.\n"
        f"Last error: {last.response['Error']['Code'] if last else 'unknown'}"
    )


def guard_deadline(elapsed: float) -> None:
    """Requirement 1 AC6 -- checked between stages, never mid-call."""
    if elapsed > DEADLINE_SECONDS:
        raise PreflightError(
            f"Preflight exceeded its {DEADLINE_SECONDS}s budget. "
            "Check network reachability to AWS endpoints."
        )


def main(
    config_path: Path | None = None,
    marker: Path = MARKER,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    started = clock()
    if config_path is None:
        config_path = Path(os.environ.get("PREFLIGHT_CONFIG", "config.json"))

    def elapsed() -> float:
        return clock() - started

    try:
        settings = load_settings(config_path)
        print(f"[ok]   region confirmed: {settings.region}")

        arn = check_credentials(settings.region)
        guard_deadline(elapsed())
        print(f"[ok]   credentials resolve: {arn}")

        check_deploy_permissions(settings.region)
        guard_deadline(elapsed())
        print("[ok]   deployment permissions present")

        model_id, reply = check_nova_lite(settings.region, settings.model_id)
        guard_deadline(elapsed())
        print(f"[ok]   Nova Lite responded via {model_id}: {reply!r}")

        if not settings.alarm_email:
            print("[warn] alarm_email is unset; the billing alarm needs it "
                  "before the us-east-1 stack is deployed (Requirement 8 AC5).")
        print("[note] Billing alerts must be enabled once per account in "
              "Billing preferences (us-east-1) or EstimatedCharges will never "
              "publish and the alarm will sit at INSUFFICIENT_DATA.")
        print("[note] After the us-east-1 stack deploys, the SNS subscription "
              "email needs a confirmation click; an unconfirmed subscription "
              "delivers nothing (Requirement 8 AC5).")

        marker.write_text(
            json.dumps({"region": settings.region, "model_id": model_id,
                        "at": time.time()}),
            encoding="utf-8",
        )
        print(f"\nPREFLIGHT PASS in {elapsed():.1f}s -- proceed to deployment.")
        return 0

    except PreflightError as exc:
        print(f"\nPREFLIGHT FAIL in {elapsed():.1f}s\n\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
