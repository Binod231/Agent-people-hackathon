"""Unit tests for `preflight/preflight.py`. Requirement 1 AC3, AC4, AC5, AC6.

Scope note: AC1 and AC2 are *claims about an account* -- that credentials
resolve and that Nova Lite answers -- and the design settles them with the live
run in task 1.4, not here. What is testable offline is everything around those
calls: which region the script will accept, what it prints when a check fails,
and that it gives up at the 60-second line. Those are the three failure paths a
Builder meets at 2 a.m., so they get examples rather than a property.

Every check takes an injectable client and `main` takes an injectable clock, so
no test here opens a socket. The autouse fixture replaces `boto3.client` with a
tripwire to keep it that way: a test that forgets to inject fails on an
assertion instead of reaching AWS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from preflight import preflight
from preflight.preflight import (
    DEADLINE_SECONDS,
    FALLBACK_MODEL_ID,
    PRIMARY_MODEL_ID,
    PreflightError,
    Settings,
    check_credentials,
    check_deploy_permissions,
    check_nova_lite,
    guard_deadline,
    load_settings,
    main,
)

REGION = "us-east-1"
CALLER_ARN = "arn:aws:iam::123456789012:user/builder"


# ---------------------------------------------------------------------------
# Isolation and test doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the developer's AWS environment and forbid real clients.

    `load_settings` reads `TARGET_REGION` and `main` reads `PREFLIGHT_CONFIG`,
    so an exported value would otherwise decide the outcome of half these
    tests. The AWS region variables go too: proving that none of them leaks in
    is the whole point of AC4.
    """
    for name in (
        "TARGET_REGION",
        "PREFLIGHT_CONFIG",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"a test reached boto3.client{args!r} -- inject a fake")

    monkeypatch.setattr(preflight.boto3, "client", forbidden)


def client_error(code: str, operation: str = "Converse") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "scripted"}}, operation)


class FakeSTS:
    """`get_caller_identity` answers with an ARN, or raises what it was given."""

    def __init__(self, answer: object = CALLER_ARN):
        self._answer = answer
        self.calls = 0

    def get_caller_identity(self) -> dict:
        self.calls += 1
        if isinstance(self._answer, Exception):
            raise self._answer
        return {"Arn": self._answer, "Account": "123456789012"}


class FakeCloudFormation:
    def __init__(self, answer: object = None):
        self._answer = answer
        self.calls: list[dict] = []

    def list_stacks(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        if isinstance(self._answer, Exception):
            raise self._answer
        return {"StackSummaries": []}


class FakeBedrock:
    """`converse` answers per model id: a reply string or an exception.

    Keyed by id because the two-attempt fallback is the behavior under test --
    a single scripted answer could not tell "denied on both" apart from
    "denied on the direct id, served by the profile".
    """

    def __init__(self, answers: dict[str, object]):
        self._answers = answers
        self.calls: list[str] = []

    def converse(
        self, *, modelId: str, messages: list, inferenceConfig: dict  # noqa: N803
    ) -> dict:
        self.calls.append(modelId)
        answer = self._answers.get(modelId, client_error("ResourceNotFoundException"))
        if isinstance(answer, Exception):
            raise answer
        return {"output": {"message": {"content": [{"text": answer}]}}}


class FakeClock:
    """Returns each scripted reading in turn, then holds the last one.

    `main` reads the clock once at the start and again after every stage, so a
    script is just the timeline of readings. Holding the last value means a
    test only scripts as far as the moment it cares about.
    """

    def __init__(self, *readings: float):
        self._readings = list(readings) or [0.0]
        self.reads = 0

    def __call__(self) -> float:
        value = self._readings[min(self.reads, len(self._readings) - 1)]
        self.reads += 1
        return value


def install_clients(
    monkeypatch: pytest.MonkeyPatch,
    sts: object = None,
    cfn: object = None,
    bedrock: object = None,
) -> dict[str, Any]:
    """Replace the one client factory `main` goes through. Returns the fakes."""
    clients = {
        "sts": sts if sts is not None else FakeSTS(),
        "cloudformation": cfn if cfn is not None else FakeCloudFormation(),
        "bedrock-runtime": (
            bedrock if bedrock is not None else FakeBedrock({PRIMARY_MODEL_ID: "ok"})
        ),
    }
    monkeypatch.setattr(
        preflight, "_client", lambda service, region, config: clients[service]
    )
    return clients


def write_config(directory: Path, **fields: object) -> Path:
    path = directory / "config.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Region resolution -- explicit configuration only (AC3, AC4)
# ---------------------------------------------------------------------------


def test_region_is_read_from_builder_configuration(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        target_region="eu-west-1",
        model_id="amazon.nova-micro-v1:0",
        alarm_email="builder@example.com",
    )
    assert load_settings(path) == Settings(
        region="eu-west-1",
        model_id="amazon.nova-micro-v1:0",
        alarm_email="builder@example.com",
    )


@pytest.mark.parametrize(
    ("label", "fields"),
    [
        ("key_absent", {}),
        ("empty_string", {"target_region": ""}),
        ("whitespace_only", {"target_region": "   "}),
        ("placeholder_none", {"target_region": "none"}),
        ("placeholder_none_capitalized", {"target_region": "None"}),
        ("placeholder_default", {"target_region": "DEFAULT"}),
        ("placeholder_null", {"target_region": "null"}),
        ("wrong_type", {"target_region": 1}),
        ("null_literal", {"target_region": None}),
    ],
)
def test_absent_or_placeholder_region_demands_explicit_confirmation(
    tmp_path: Path, label: str, fields: dict
) -> None:
    """AC4. A placeholder left in config.json is not a choice."""
    path = write_config(tmp_path, **fields)
    with pytest.raises(PreflightError) as caught:
        load_settings(path)

    message = str(caught.value)
    assert "target_region is not set" in message
    assert "explicitly, not inherited" in message
    assert "TARGET_REGION" in message
    assert str(path) in message


def test_missing_config_file_reports_the_same_remediation(tmp_path: Path) -> None:
    with pytest.raises(PreflightError) as caught:
        load_settings(tmp_path / "config.json")
    assert "target_region is not set" in str(caught.value)


def test_region_is_never_inherited_from_the_aws_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 is specifically about silent inheritance, so name the variables."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_PROFILE", "default")

    with pytest.raises(PreflightError):
        load_settings(write_config(tmp_path, alarm_email="builder@example.com"))


def test_target_region_env_overrides_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `TARGET_REGION=... ` on the command line is still explicit."""
    monkeypatch.setenv("TARGET_REGION", "eu-central-1")
    settings = load_settings(write_config(tmp_path, target_region="us-east-1"))
    assert settings.region == "eu-central-1"


def test_model_id_and_email_fall_back_to_sensible_defaults(tmp_path: Path) -> None:
    """Only the region has to be explicit; the model id has a right answer."""
    settings = load_settings(write_config(tmp_path, target_region=" us-east-1 "))
    assert settings == Settings(
        region="us-east-1", model_id=PRIMARY_MODEL_ID, alarm_email=""
    )


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("truncated_object", '{"target_region": "us-east-1"'),
        ("trailing_comma", '{"target_region": "us-east-1",}'),
        ("not_json_at_all", "target_region = us-east-1"),
        ("empty_file", ""),
    ],
)
def test_unparseable_config_names_the_recovery_step(
    tmp_path: Path, label: str, body: str
) -> None:
    path = tmp_path / "config.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PreflightError) as caught:
        load_settings(path)

    message = str(caught.value)
    assert "not valid JSON" in message
    assert "config.example.json" in message


@pytest.mark.parametrize("body", ["[]", '"us-east-1"', "42", "null"])
def test_config_must_be_a_json_object(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PreflightError) as caught:
        load_settings(path)
    assert "config.example.json" in str(caught.value)


# ---------------------------------------------------------------------------
# Credentials and the permission probe -- the failure copy (AC1)
# ---------------------------------------------------------------------------


def test_credentials_check_returns_the_caller_arn() -> None:
    assert check_credentials(REGION, FakeSTS()) == CALLER_ARN


def test_no_credentials_points_at_aws_configure() -> None:
    with pytest.raises(PreflightError) as caught:
        check_credentials(REGION, FakeSTS(NoCredentialsError()))

    message = str(caught.value)
    assert "No AWS credentials resolved" in message
    assert "aws configure" in message


def test_rejected_credentials_name_the_aws_error_code() -> None:
    with pytest.raises(PreflightError) as caught:
        check_credentials(
            REGION, FakeSTS(client_error("InvalidClientTokenId", "GetCallerIdentity"))
        )
    assert "InvalidClientTokenId" in str(caught.value)


def test_unreachable_sts_blames_the_network_not_the_account() -> None:
    with pytest.raises(PreflightError) as caught:
        check_credentials(
            REGION, FakeSTS(EndpointConnectionError(endpoint_url="https://sts"))
        )

    message = str(caught.value)
    assert "EndpointConnectionError" in message
    assert REGION in message


def test_permission_probe_is_read_only() -> None:
    """A dry run must not create anything: listing is the whole probe."""
    cfn = FakeCloudFormation()
    check_deploy_permissions(REGION, cfn)
    assert cfn.calls == [{"StackStatusFilter": ["CREATE_COMPLETE"]}]


def test_denied_cloudformation_lists_the_permissions_deployment_needs() -> None:
    cfn = FakeCloudFormation(client_error("AccessDenied", "ListStacks"))
    with pytest.raises(PreflightError) as caught:
        check_deploy_permissions(REGION, cfn)

    message = str(caught.value)
    assert "AccessDenied" in message
    for service in ("CloudFormation", "Lambda", "API Gateway", "S3", "CloudFront"):
        assert service in message


# ---------------------------------------------------------------------------
# Nova Lite entitlement -- the model-access path (AC2, AC5)
# ---------------------------------------------------------------------------


def test_access_denied_on_both_ids_reports_the_console_steps() -> None:
    """AC5. The message has to be the remediation, not a status code."""
    bedrock = FakeBedrock(
        {
            PRIMARY_MODEL_ID: client_error("AccessDeniedException"),
            FALLBACK_MODEL_ID: client_error("AccessDeniedException"),
        }
    )
    with pytest.raises(PreflightError) as caught:
        check_nova_lite(REGION, PRIMARY_MODEL_ID, bedrock)

    assert bedrock.calls == [PRIMARY_MODEL_ID, FALLBACK_MODEL_ID]

    message = str(caught.value)
    assert "Nova Lite is not available to this account" in message
    assert PRIMARY_MODEL_ID in message and FALLBACK_MODEL_ID in message
    assert REGION in message
    # The four steps a Builder can actually follow.
    assert "Bedrock console" in message
    assert "Model access" in message
    assert "Modify model access" in message
    assert "Access granted" in message
    # And the diagnosis, so a denial is not confused with an outage.
    assert "AccessDeniedException" in message


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "ValidationException", "ResourceNotFoundException"]
)
def test_a_region_served_only_by_the_inference_profile_still_passes(code: str) -> None:
    """The fallback is a real finding: these three codes mean 'try the other id'."""
    bedrock = FakeBedrock(
        {PRIMARY_MODEL_ID: client_error(code), FALLBACK_MODEL_ID: "  ok\n"}
    )
    assert check_nova_lite(REGION, PRIMARY_MODEL_ID, bedrock) == (
        FALLBACK_MODEL_ID,
        "ok",
    )
    assert bedrock.calls == [PRIMARY_MODEL_ID, FALLBACK_MODEL_ID]


def test_a_configured_profile_id_is_not_tried_twice() -> None:
    bedrock = FakeBedrock({FALLBACK_MODEL_ID: client_error("AccessDeniedException")})
    with pytest.raises(PreflightError) as caught:
        check_nova_lite(REGION, FALLBACK_MODEL_ID, bedrock)

    assert bedrock.calls == [FALLBACK_MODEL_ID]
    assert str(caught.value).count(FALLBACK_MODEL_ID) == 1


def test_an_unrelated_error_fails_immediately_rather_than_retrying() -> None:
    """Throttling is not an entitlement problem, so the profile id is pointless."""
    bedrock = FakeBedrock({PRIMARY_MODEL_ID: client_error("ThrottlingException")})
    with pytest.raises(PreflightError) as caught:
        check_nova_lite(REGION, PRIMARY_MODEL_ID, bedrock)

    assert bedrock.calls == [PRIMARY_MODEL_ID]
    message = str(caught.value)
    assert "ThrottlingException" in message
    assert "Model access" not in message


def test_unreachable_bedrock_blames_the_network() -> None:
    bedrock = FakeBedrock(
        {PRIMARY_MODEL_ID: EndpointConnectionError(endpoint_url="https://bedrock")}
    )
    with pytest.raises(PreflightError) as caught:
        check_nova_lite(REGION, PRIMARY_MODEL_ID, bedrock)

    assert bedrock.calls == [PRIMARY_MODEL_ID]
    assert "EndpointConnectionError" in str(caught.value)


def test_the_direct_id_wins_when_it_answers() -> None:
    bedrock = FakeBedrock({PRIMARY_MODEL_ID: "ok"})
    assert check_nova_lite(REGION, PRIMARY_MODEL_ID, bedrock) == (
        PRIMARY_MODEL_ID,
        "ok",
    )
    assert bedrock.calls == [PRIMARY_MODEL_ID]


# ---------------------------------------------------------------------------
# The 60-second budget (AC6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("elapsed", [0.0, 1.0, 59.9, float(DEADLINE_SECONDS)])
def test_deadline_allows_everything_up_to_the_budget(elapsed: float) -> None:
    guard_deadline(elapsed)


@pytest.mark.parametrize("elapsed", [60.01, 61.0, 600.0])
def test_deadline_fires_past_the_budget(elapsed: float) -> None:
    with pytest.raises(PreflightError) as caught:
        guard_deadline(elapsed)

    message = str(caught.value)
    assert f"{DEADLINE_SECONDS}s budget" in message
    assert "network reachability" in message


def test_a_slow_stage_stops_the_run_before_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC6 is checked between stages: a blown budget must not start another."""
    clients = install_clients(monkeypatch)
    marker = tmp_path / ".preflight-ok"

    status = main(
        config_path=write_config(tmp_path, target_region=REGION),
        marker=marker,
        clock=FakeClock(0.0, 61.0),  # start, then after credentials
    )

    assert status == 1
    assert clients["sts"].calls == 1
    assert clients["cloudformation"].calls == []  # never reached
    assert clients["bedrock-runtime"].calls == []
    assert not marker.exists()

    captured = capsys.readouterr()
    assert "PREFLIGHT FAIL" in captured.err
    assert f"{DEADLINE_SECONDS}s budget" in captured.err


def test_a_blown_budget_after_the_model_call_still_writes_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The marker is the deploy gate, so a slow run must not leave one behind."""
    install_clients(monkeypatch)
    marker = tmp_path / ".preflight-ok"

    status = main(
        config_path=write_config(tmp_path, target_region=REGION),
        marker=marker,
        clock=FakeClock(0.0, 5.0, 10.0, 75.0),
    )

    assert status == 1
    assert not marker.exists()
    assert f"{DEADLINE_SECONDS}s budget" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `main` wiring -- exit status, ordering, and the marker (AC3, AC4, AC5, AC6)
# ---------------------------------------------------------------------------


def test_missing_region_exits_non_zero_before_any_aws_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC4 end to end: non-zero status plus the explicit-confirmation message."""
    clients = install_clients(monkeypatch)
    marker = tmp_path / ".preflight-ok"

    status = main(
        config_path=write_config(tmp_path, alarm_email="builder@example.com"),
        marker=marker,
        clock=FakeClock(0.0, 0.2),
    )

    assert status == 1
    assert clients["sts"].calls == 0
    assert clients["bedrock-runtime"].calls == []
    assert not marker.exists()

    captured = capsys.readouterr()
    assert "PREFLIGHT FAIL" in captured.err
    assert "target_region is not set" in captured.err
    assert "explicitly, not inherited" in captured.err
    assert captured.out == ""  # no stage ever reported ok


def test_denied_model_access_exits_non_zero_with_the_console_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC5 end to end."""
    bedrock = FakeBedrock(
        {
            PRIMARY_MODEL_ID: client_error("AccessDeniedException"),
            FALLBACK_MODEL_ID: client_error("AccessDeniedException"),
        }
    )
    install_clients(monkeypatch, bedrock=bedrock)
    marker = tmp_path / ".preflight-ok"

    status = main(
        config_path=write_config(tmp_path, target_region=REGION),
        marker=marker,
        clock=FakeClock(0.0, 1.0, 2.0, 3.0),
    )

    assert status == 1
    assert bedrock.calls == [PRIMARY_MODEL_ID, FALLBACK_MODEL_ID]
    assert not marker.exists()

    captured = capsys.readouterr()
    assert "Modify model access" in captured.err
    # The earlier checks did pass, and said so, before the model call failed.
    assert "credentials resolve" in captured.out


def test_main_stops_at_the_first_failing_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four checks in order; a credential failure must not reach Bedrock."""
    clients = install_clients(monkeypatch, sts=FakeSTS(NoCredentialsError()))

    status = main(
        config_path=write_config(tmp_path, target_region=REGION),
        marker=tmp_path / ".preflight-ok",
        clock=FakeClock(0.0, 0.1),
    )

    assert status == 1
    assert clients["cloudformation"].calls == []
    assert clients["bedrock-runtime"].calls == []


def test_a_pass_records_the_winning_model_id_in_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The deploy script reads this file for the IAM policy, so the id matters."""
    bedrock = FakeBedrock(
        {
            PRIMARY_MODEL_ID: client_error("AccessDeniedException"),
            FALLBACK_MODEL_ID: "ok",
        }
    )
    install_clients(monkeypatch, bedrock=bedrock)
    marker = tmp_path / ".preflight-ok"

    status = main(
        config_path=write_config(
            tmp_path, target_region=REGION, alarm_email="builder@example.com"
        ),
        marker=marker,
        clock=FakeClock(0.0, 1.0, 2.0, 3.0, 4.0),
    )

    assert status == 0
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["region"] == REGION
    assert recorded["model_id"] == FALLBACK_MODEL_ID
    assert isinstance(recorded["at"], float)

    captured = capsys.readouterr()
    assert "PREFLIGHT PASS" in captured.out
    assert FALLBACK_MODEL_ID in captured.out
    assert captured.err == ""


def test_a_pass_prints_the_two_manual_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Neither billing preferences nor the SNS click has an API; say so early."""
    install_clients(monkeypatch)

    status = main(
        config_path=write_config(tmp_path, target_region=REGION),
        marker=tmp_path / ".preflight-ok",
        clock=FakeClock(0.0, 1.0, 2.0, 3.0, 4.0),
    )

    assert status == 0
    out = capsys.readouterr().out
    assert "alarm_email is unset" in out  # it was, in this config
    assert "Billing preferences" in out
    assert "confirmation" in out


def test_main_reads_the_config_path_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the path is Builder-supplied too, not hardcoded to the repo root."""
    install_clients(monkeypatch)
    config = write_config(tmp_path, target_region="eu-west-2")
    monkeypatch.setenv("PREFLIGHT_CONFIG", str(config))
    marker = tmp_path / ".preflight-ok"

    status = main(marker=marker, clock=FakeClock(0.0, 1.0, 2.0, 3.0, 4.0))

    assert status == 0
    assert json.loads(marker.read_text(encoding="utf-8"))["region"] == "eu-west-2"
