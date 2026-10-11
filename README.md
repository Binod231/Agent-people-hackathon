# Codebase Welcome Guide

Paste a public GitHub repository URL, ask "where do I start?", get a guided tour
of an unfamiliar codebase. An AWS Builder Center "Build an Agent Weekend
Challenge" submission.

- Spec: [`.kiro/specs/delightful-aws-agent/`](.kiro/specs/delightful-aws-agent/)
- Out-of-scope log (Requirement 12 AC6): [`docs/out-of-scope.md`](docs/out-of-scope.md)
- Public deployed URL: **<https://ciy6fwtatvaifvve5xadk6fnvy0xelpz.lambda-url.us-east-1.on.aws/>**

## Architecture

One AWS SAM stack in us-east-1. One AWS Lambda function (python3.12, 1024 MB,
60 s timeout) behind a Lambda Function URL with `AuthType: NONE`, serving both
halves of the product:

- `GET /` returns the Chat_UI from the deployment package
- `POST /api/chat` runs the agent

No agent framework, no database, no server-side session. One request does the
whole job: validate the URL, fetch metadata, list the tree, select up to 12
files, read them, call the model.

The rest of the stack is three supporting pieces: Amazon Bedrock Nova Lite
(`amazon.nova-lite-v1:0`) for inference, Amazon CloudWatch Logs with 7-day
retention, and Amazon SNS plus two CloudWatch alarms — estimated charges at
10 USD and Lambda invocations at 175 per 5 minutes.

Because one origin serves the page and the API, every request is same-origin and
there is no CORS anywhere. The earlier design used CloudFront, S3, and API
Gateway; that cut is recorded in [`docs/out-of-scope.md`](docs/out-of-scope.md)
entries 5 and 6.

## Layout

```
preflight/preflight.py   the AWS access and Nova Lite entitlement gate (Requirement 1)
src/agent/               config, models, errors, steps, url_parse, github_client,
                         selection, truncate, tokens, context_builder, prompts,
                         bedrock, postprocess, orientation, chat, handler
ui/                      index.html, styles.css, app.js -- no build step
infra/template.yaml      the whole deployed system: function, URL, log group, alarm
infra/samconfig.toml     sam build / sam deploy settings
Makefile                 the SAM makefile build; packages src/agent/ plus ui/
scripts/deploy.sh        the only supported way to create the real resources
scripts/local_server.py  stdlib http.server routing through the same handler
tests/                   pytest plus hypothesis; the whole suite runs offline
docs/out-of-scope.md     the log of what was cut and why (Requirement 12 AC6)
```

Every limit lives in [`src/agent/config.py`](src/agent/config.py) as a named
constant citing the acceptance criterion it implements.

## Configure

```sh
cp config.example.json config.json
```

Set `target_region` (us-east-1 — the billing alarm metric publishes nowhere
else) and `alarm_email`. AWS sends a subscription confirmation on the first
deploy; until that link is clicked the alarm notifies no one.

## Run

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python preflight/preflight.py   # the gate: credentials, region, one real inference
scripts/deploy.sh               # refuses to run without a fresh .preflight-ok
```

The preflight writes the `.preflight-ok` marker recording the region and the
model id that answered. `scripts/deploy.sh` reads both out of it, so the id the
IAM policy authorizes is the id that was actually confirmed.

Local development, no AWS deploy needed:

```sh
python scripts/local_server.py
```

Tests:

```sh
pytest
```

## Security

The deployed endpoint is public and unauthenticated by requirement
(Requirement 7 AC1, Requirement 12 AC2). **There is no rate limit and no daily
quota.** The controls are the AWS account's ceiling of 10 concurrent Lambda
executions, a CloudWatch alarm on invocations at 175 per 5 minutes, the
8,192-byte request body cap, a 600-token output cap, and the 10 USD billing
alarm.

A reserved concurrency of 2 was specified and could not be deployed: AWS holds
10 unreserved executions back and this account's whole limit is 10, so no
reservation fits. The account ceiling is the concurrency bound instead — five
times the rate, and account-wide rather than a per-function guarantee. It bounds
the *rate*; nothing bounds the *total*. `AWS/Billing EstimatedCharges` publishes
roughly every six hours, so that alarm can lag the spend it reports by most of a
day, and sustained abuse reaches the 10 USD threshold in a little over an hour —
faster than the alarm watching for it. The invocation alarm is the compensating
control: it reports abuse within minutes, though it notifies rather than
throttles. The arithmetic is in
[`docs/out-of-scope.md`](docs/out-of-scope.md) entry 9, which records this as an
accepted risk rather than a deferred nicety.

That is bounded enough for a weekend demo and not bounded enough for anything
else. Read entry 9 before pointing anything but a demo at this stack.
