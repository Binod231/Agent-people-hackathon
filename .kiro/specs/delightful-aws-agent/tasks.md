# Implementation Plan: delightful-aws-agent

## Overview

Python 3.12 on AWS Lambda, no agent framework, one AWS SAM stack in us-east-1. A single function sits behind a Lambda Function URL and serves both halves of the product: `GET /` returns the Chat_UI from the deployment package, `POST /api/chat` runs the agent. No CloudFront distribution, no S3 bucket, no API Gateway, no API key, and therefore no CORS anywhere.

This is the lean revision of the plan. The original had 54 sub-tasks and nineteen property tests; this one has 29, of which 14 are already finished. Every cut is recorded with its reasoning in `docs/out-of-scope.md` entries 5 through 9 and in the design's `## Lean Deployment Revision` section, so a later non-demo deployment can put the dropped controls back deliberately instead of rediscovering why they were there.

Three things shape what is left:

- **The preflight gate is passed.** Task 1.4 ran against the real account: Target_Region us-east-1, model id `amazon.nova-lite-v1:0` — the direct foundation-model id, not the `us.` inference profile. `.preflight-ok` and `config.json` exist, and `scripts/deploy.sh` still checks the marker so a fresh clone cannot deploy blind.
- **The offline pipeline is built and verified.** Every module is on disk and green: `url_parse`, `github_client`, `selection`, `truncate`, `tokens`, `context_builder`, `prompts`, `bedrock`, `postprocess`, `errors` with its full `ERROR_COPY` table, `orientation`, `chat`, and `handler` — 680 tests, the whole suite in about five seconds, no network. The Chat_UI has its own `node:test` suite at `tests/ui/test_app.mjs` (5 tests).
- **Builder-performed tasks are marked `[Builder]`.** Those involve console clicks, a confirmation email, a screen recording, a published article, or a profile edit. An executing agent must not attempt them.

Properties 1, 2, 4, and 6 are the four that survived the cut, and all four are written and passing. The other fifteen are replaced by targeted unit tests named in the task bullets below — fewer guarantees, stated honestly, rather than nineteen properties half-written on a deadline.

Acceptance-criteria numbers in the trace lines are the **revised** ones. Requirement 6 AC4 and Requirement 8 AC1, AC2, and AC9 are gone, and the criteria after them renumbered; the design's `## Lean Deployment Revision` carries the old-to-new mapping.

## Tasks

- [x] 1. Scaffolding and the preflight gate
  - [x] 1.1 Create the project skeleton and shared primitives
    - Directory layout per the design's Project Layout section
    - `src/agent/config.py` — every limit as a named constant with its criterion in a comment
    - `src/agent/models.py` — frozen dataclasses: `RepoRef`, `RepoMeta`, `TreeEntry`, `Candidate`, `FileBlock`, `RepoContext`, `StepRecord`, `Deadline`
    - `src/agent/errors.py` — `ErrorCode` enum, `AgentError`, `ALLOWED_PARAMS` only; the copy table and `render()` land in 5.1
    - `src/agent/steps.py` — `StepName`, `StepRecorder`, `Deadline` implementation
    - `config.example.json`, `src/requirements.txt` (boto3 pinned), `requirements-dev.txt` (pytest, hypothesis), `.gitignore`, `docs/out-of-scope.md` seeded with the four design-time entries
    - _Requirements: 12.1, 12.6_

  - [x] 1.2 Implement `preflight/preflight.py`
    - Four checks in order: region resolution, credentials, CloudFormation permission probe, one real Nova Lite inference
    - No `AWS_DEFAULT_REGION` fallback — absent region must fail
    - Try `amazon.nova-lite-v1:0` then the `us.` inference profile; print which one answered
    - 60-second self-imposed deadline; write `.preflight-ok` recording region and winning model id
    - Print the billing-preferences and SNS-confirmation reminders
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 1.3 Write unit tests for preflight configuration and failure paths
    - Missing or placeholder region exits non-zero with the explicit-confirmation message
    - `AccessDeniedException` on both model ids yields the model-access remediation steps
    - Deadline guard fires past 60 s with a stubbed clock
    - _Requirements: 1.3, 1.4, 1.5, 1.6_

  - [x] 1.4 [Builder] Run the preflight against real AWS credentials — HARD GATE
    - Blocks every deployment task; `scripts/deploy.sh` checks for `.preflight-ok`
    - If Nova Lite access is denied, request it in the Bedrock console (Model access → Modify model access) and re-run. Nova approvals are usually immediate, but this is the one step that can wait on an external decision
    - Record the winning model id for the IAM policy in 7.1
    - **Result:** passed in us-east-1 with `amazon.nova-lite-v1:0`
    - _Requirements: 1.1, 1.2, 1.7_

  - [x] 1.5 [Builder] Enable billing alerts in Billing preferences (us-east-1 console)
    - One-time per account, no API exists for this preference
    - Do it now, not with the alarm in 7.1: `AWS/Billing EstimatedCharges` only publishes every ~6 hours, and until the preference is on the alarm sits in `INSUFFICIENT_DATA` forever
    - _Requirements: 8.2_

- [x] 2. Repository intake and file selection
  - [x] 2.1 Implement `src/agent/url_parse.py`
    - Accept the four surface forms of `https://github.com/{owner}/{repo}`; reject `http://`, SSH, gist, deeper paths, and credential-bearing URLs
    - Raise `AgentError(BAD_URL)` on anything else
    - _Requirements: 2.2, 2.5_

  - [x] 2.2 Write property test for URL canonicalization and rejection
    - **Property 1: Repository URL canonicalization and rejection**
    - **Validates: Requirements 2.2, 2.5**

  - [x] 2.3 Implement `src/agent/github_client.py`
    - `get_metadata`, `list_tree`, parallel `read_files` from `raw.githubusercontent.com` (8 workers)
    - `HEADERS` is a module constant with no credential path into it; no `Authorization` header anywhere
    - URL-escape branch names; treat `size` as kilobytes; map 404/403/429/5xx and tree `truncated` to the right error codes; `_reset_minutes` clamps to 1–60
    - A single failed raw fetch degrades to `unread_paths` rather than failing the request
    - _Requirements: 2.3, 2.4, 2.6, 2.7, 3.8, 3.9_

  - [x] 2.4 Write property test for unauthenticated, default-branch-pinned requests
    - **Property 2: Every GitHub request is unauthenticated and pinned to the default branch**
    - **Validates: Requirements 2.3, 2.4**
    - Build `FakeGitHub` (recording transport) and the three tree fixtures in `tests/conftest.py`; later tests reuse them

  - [x] 2.5 Implement `src/agent/selection.py`
    - `classify` five-tier priority order, noise directory and lock-file exclusion, binary-extension pre-filter, 500 KB size filter
    - `select` with the 12-file cap, deterministic in-tier ranking, round-robin tier-5 fill across top-level directories, `package.json` `main` entry-point resolution
    - `decode_text` as the hard binary check: NUL-byte scan plus strict UTF-8 decode
    - _Requirements: 3.1, 3.3, 3.10, 3.11_

  - [x] 2.6 Write property test for the priority order and the twelve-file cap
    - **Property 4: File selection honors the priority order and the twelve-file cap**
    - **Validates: Requirements 3.1, 3.3**
    - Include the recorded Flask, Gin, and Express trees as concrete cases

- [x] 3. Context assembly and the token budget
  - [x] 3.1 Implement `src/agent/truncate.py`
    - 8,000-character cap measured in characters, cut preferring the last newline in the final 10%, return `(kept, was_truncated)` with `kept` always a prefix
    - _Requirements: 3.4, 3.5_

  - [x] 3.2 Write property test for truncation bounds and labeling
    - **Property 6: Truncation is bounded and honestly labeled**
    - **Validates: Requirements 3.4, 3.5**

  - [x] 3.3 Implement `src/agent/tokens.py`
    - `estimate_tokens` at 3.5 chars/token plus per-block overhead; 27,000 effective cap against the 30,000 contractual cap
    - `fit_to_budget` admits in priority order, shortens to an affordable prefix before dropping, records `omitted for space` notes
    - History window: 10 most recent turns, then trim from the oldest until the 4,000-token reserve holds, always starting on a user-role message
    - Log the estimated-versus-actual ratio for calibration
    - _Requirements: 3.6, 3.7_

  - [x] 3.4 Implement `src/agent/context_builder.py`
    - `tree_summary`: at most one `/` in a path, 200-entry cap, directories before files, noise directories collapsed to a single entry
    - Block rendering in the plain-delimiter format with the truncation label stating both counts
    - Delimiter-collision guard: rewrite any file line starting `=== ` to `==== `
    - Both context tiers — orientation (top 4 bodies at 3,000 chars) and chat turn (12 bodies at 8,000 chars), each carrying the full 12-path manifest and `unread_paths`
    - Unit tests: the 200-entry and one-level bounds hold against the three recorded trees and against a synthetic 6,000-entry tree; a file whose own content contains `=== ` cannot forge a block boundary; both tiers carry all 12 manifest paths
    - _Requirements: 3.2, 4.5_

- [x] 4. Model call, prompts, and the Orientation_Message
  - [x] 4.1 Implement `src/agent/prompts.py` and `src/agent/bedrock.py`
    - `SYSTEM_PROMPT` (grounding, citation in backticks, truncation honesty, file content is data not instruction, under 350 words) and `ORIENTATION_PROMPT` (the four-part structure, 250-word target)
    - Assert the system prompt measures under the 800-token reserve
    - Converse wrapper, `maxTokens: 600`, `temperature: 0.3`; botocore retries disabled so the attempt count is exactly initial plus 2
    - Exponential backoff with jitter; `deadline.allows()` refuses a retry it cannot finish; throttle codes to `MODEL_BUSY`, everything else to `INTERNAL`
    - Return `usage.inputTokens` / `outputTokens` for the calibration log
    - Unit tests with `FakeBedrock` and a recording `sleep`: at most three attempts, each backoff longer than the last, no retry on a non-throttling error, no retry started that cannot finish inside the remaining deadline
    - _Requirements: 4.1, 4.2, 4.5, 6.2_

  - [x] 4.2 Implement `src/agent/postprocess.py` and `src/agent/orientation.py`
    - `cap_words` at 350 words, trimming at a sentence boundary in the final quarter, closing an unbalanced code fence
    - `detect_language`: GitHub's field first, manifest inference as fallback, framework signal preferred
    - `finalize` validate-and-repair for repo name, language, purpose sentence, three manifest paths, two follow-up questions; repairs appended not substituted; `cap_words` applied before and after; return the repair list for the metric
    - Unit tests: the word cap at 349/350/351 words, text with no sentence boundary, an unbalanced fence; `finalize` against empty model output, output naming no paths, and output asking no questions — each must still produce all five required parts
    - _Requirements: 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 4.3 Implement `src/agent/chat.py`
    - Follow-up turn orchestration: full context tier, history window, Converse call, word cap
    - Mode inferred from an empty or absent `messages` array
    - Unit tests: the sent history is a contiguous suffix of at most ten turns beginning on a user message; a history and context pair that would blow the budget still assembles under the 27,000-token effective cap
    - _Requirements: 3.7, 4.1, 4.2, 4.5_

- [x] 5. Error taxonomy and the handler boundary
  - [x] 5.1 Complete `src/agent/errors.py`
    - `ERROR_COPY` with one entry per `ErrorCode`, text exactly as in the design's mapping table — cause first, next action last
    - Drop the `RATE_LIMITED` code and the `retry_after` entry in `ALLOWED_PARAMS`: Requirement 6 AC4 is gone, and with no usage plan nothing can produce a 429
    - Refresh the per-code criterion comments to the revised numbering (`BODY_TOO_LARGE` → 8.1, `INTERNAL` → 6.4, the `ALLOWED_PARAMS` note → 6.6)
    - `render()` interpolating only whitelisted numeric params; `render()` must not be able to raise
    - Import-time assertion that the table is complete over the enum
    - Unit tests: one row per code renders non-empty text that names a next action; an exception carrying an account id, a role ARN, an `AKIA` string, a bearer token, and a traceback fragment renders none of those substrings
    - _Requirements: 2.5, 2.6, 2.7, 3.8, 3.9, 6.1, 6.3, 6.6, 8.1_

  - [x] 5.2 Implement `src/agent/handler.py` and `scripts/local_server.py`
    - Function URL routing, since one function serves both halves: `GET /` → `index.html`, `GET /styles.css` and `GET /app.js` → their assets with the right content type, `POST /api/chat` → the agent, anything else → 404. Assets are read from the package directory next to the module
    - 8,192-byte body guard as the first statement on the chat route, measured on encoded bytes, before any outbound call
    - `parse_request`, `run_turn`, step recording, the single `except AgentError` / `except Exception` boundary, the `finally` structured log line with the correlation id
    - Soft deadlines: 26,000 ms chat, 18,000 ms orientation. There is no API Gateway integration timeout to sit inside any more — the 26 s guard exists purely to keep Requirement 4 AC6's 30-second promise
    - `_ok` / `_response` envelope — exactly one of `message` or `error`, `steps` always present
    - `scripts/local_server.py`: stdlib `http.server` that hands every request to `handler` in-process, so local and deployed traffic go through the same routing code
    - Unit tests: a 8,193-byte body returns 413 with zero GitHub and zero Bedrock calls, and the oversize-repo and too-many-files guards do the same; every outcome — success, `AgentError`, bare exception — returns a complete envelope with a non-empty step array; each asset route returns its content type; two successive invocations each re-issue their own GitHub calls
    - _Requirements: 2.1, 4.6, 5.6, 5.7, 6.4, 6.5, 7.2, 7.7, 8.1, 12.3, 12.4_

- [x] 6. Checkpoint — full offline suite green, ready to deploy
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Infrastructure: one stack, one function, one URL
  - [x] 7.1 Write `infra/template.yaml`, `infra/samconfig.toml`, and `scripts/deploy.sh`
    - One SAM template, deployed to us-east-1 — the region task 1.4 confirmed, and the only region where `AWS/Billing EstimatedCharges` publishes, so the alarm needs no second stack
    - Lambda: python3.12, 1024 MB, `Timeout: 60`, `ReservedConcurrentExecutions: 2`, explicit `AWS::Logs::LogGroup` at `RetentionInDays: 7`
    - `FunctionUrlConfig` with `AuthType: NONE` as the only entry point; `ui/` copied into the function package so the same code serves the Chat_UI. No distribution, no bucket, no REST API, no CORS block
    - IAM inline policy: `bedrock:InvokeModel` on `arn:${AWS::Partition}:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0`, plus `logs:CreateLogStream` and `logs:PutLogEvents` on the log group ARN. No wildcard action, no wildcard resource, no `logs:CreateLogGroup`
    - Cost_Guard in the same template: SNS topic, email subscription from a parameter, and the `EstimatedCharges` alarm at 10 USD with `Period: 21600`, `EvaluationPeriods: 1`, `TreatMissingData: notBreaching`
    - Update `src/agent/config.py`: delete `API_THROTTLE_RATE_RPS`, `API_THROTTLE_BURST`, `API_DAILY_QUOTA`, and `API_GATEWAY_TIMEOUT_MS`; set `LAMBDA_RESERVED_CONCURRENCY` to 2 and retag it Requirement 8 AC5
    - `scripts/deploy.sh`: refuse to run unless `.preflight-ok` exists and is newer than `infra/template.yaml`; read region and model id from the marker and pass the model id as `ModelId`; `sam build` then `sam deploy`; print the Function URL and the one remaining manual step (7.2)
    - _Requirements: 1.7, 7.1, 7.9, 8.2, 8.3, 8.4, 8.5, 8.6, 12.2, 12.3_

- [x] 8. Chat_UI
  - [x] 8.1 Write `ui/index.html` and `ui/styles.css`
    - Transcript as `role="log" aria-live="polite"` present before the first message; separate `#status` `role="status"` for step progress
    - Real `<label for>` on the input, text content on the send button, skip link first in tab order, `:focus-visible` preserved, `#transcript` focusable
    - `100dvh` grid, one breakpoint at 40rem, `max-width: 48rem` centered, sticky composer with `env(safe-area-inset-bottom)`; usable at 390 px and 1280 px
    - `<details id="ingested">` for the files-I-read disclosure
    - _Requirements: 4.5, 5.7, 7.3, 7.4, 7.5, 7.6_

  - [x] 8.2 Write `ui/app.js`
    - State `{repoUrl, messages[], ingestedPaths}` in the client, mirrored to `sessionStorage`; history in the request body, trimmed to 10 turns then shrunk from the oldest end until under 8,192 bytes, saying so inline when it drops turns
    - `splitBlocks` plus `textContent` only — no `innerHTML` anywhere in the file
    - `STEPS` vocabulary matching `agent/steps.py`, advanced on a timer in flight and replaced by the response's authoritative `steps` array as a completed checklist with durations
    - Render every error response as a chat message. No 429 countdown — with no usage plan there is no quota response to render
    - Single input serving both phases, label and button text switching after the orientation arrives; focus returns to the input after each assistant message
    - Unit tests (`node:test`, minimal DOM shim, no browser): the body's `messages` array is a contiguous suffix of the client transcript in order, at most ten turns, and the encoded body stays at or under 8,192 bytes; every assistant node lands inside `#transcript`
    - _Requirements: 5.7, 6.1, 7.7, 7.8_

- [ ] 9. Deploy and verify
  - [x] 9.1 Deploy the stack and exercise the live URL
    - Run `scripts/deploy.sh`; record the Function URL as the public deployed URL
    - End-to-end pass through the deployed URL: the page loads, an orientation arrives, one follow-up question answers, one deliberate failure (a private repository) renders as a chat message
    - Log the per-step durations from five live runs across the three proof repositories and compare the orientation path against the 20 s budget; if p95 is above 15 s, drop `ORIENTATION_FILE_COUNT` to 2 and `ORIENTATION_FILE_CHARS` to 1500
    - Keyboard-only walkthrough and a layout look at 390 px and 1280 px — tab to the input, send, read the transcript, confirm nothing is clipped. The formal screen-reader audit is out of scope (entry 8)
    - **Live run results:**
      - Deployed to us-east-1, stack `codebase-welcome-guide`
      - Function URL: `https://ciy6fwtatvaifvve5xadk6fnvy0xelpz.lambda-url.us-east-1.on.aws/`. `GET /`, `/styles.css`, and `/app.js` all returned 200 with the correct content types; `/nope` and `/../config.json` both returned 404
      - Orientation on `pallets/flask`: 3 s end to end against the 20 s budget. Step timings — validating_url 0 ms, fetching_metadata 161 ms, listing_files 117 ms, selecting_files 2 ms, reading_files 428 ms, building_context 2 ms, asking_nova 1293 ms
      - Three proof repositories, language detected correctly on each: flask Python 1858 ms, gin Go 2305 ms, express JavaScript 2428 ms
      - Follow-up turn answered in chat mode
      - Failure taxonomy live: a deeper path and a garbage URL both → 400 `BAD_URL`; a nonexistent repository → 404 `REPO_NOT_FOUND`
      - Body guard: 8,100 bytes → 200, 8,300 bytes → 413 `BODY_TOO_LARGE`
      - p95 orientation sits well under the 15 s threshold, so the `ORIENTATION_FILE_COUNT` / `ORIENTATION_FILE_CHARS` latency escape hatch was **not** needed
    - **Note on the first deploy attempt:** it failed its stack create on `ReservedConcurrentExecutions: 2` — this account's whole `ConcurrentExecutions` limit is 10 and AWS holds 10 back as unreserved, so no reservation of any size fits. The stack rolled back cleanly with no orphaned resources. The reservation was removed from the template and replaced by an invocation-rate alarm (`AWS/Lambda` `Invocations`, 175 per 5 minutes). That trade and its corrected arithmetic are in `docs/out-of-scope.md` entry 9
    - _Requirements: 4.6, 5.6, 7.1, 7.2, 7.3, 7.4, 7.9_

  - [ ] 9.2 [Builder] Confirm the SNS subscription and the billing alarm
    - Click the confirmation link in the subscription email. Requirement 8 AC3 says Builder-confirmed, and an unconfirmed subscription delivers nothing. The topic ships with the main stack now, so the email arrives after the first deploy rather than before it — `scripts/deploy.sh` polls `ListSubscriptionsByTopic` and refuses to report success while the ARN still reads `PendingConfirmation`
    - Check that the alarm has left `INSUFFICIENT_DATA` after the first `EstimatedCharges` window, and prove delivery with one manual `sns publish` to the topic
    - This is the only spend control a human ever sees. Reserved concurrency of 2 bounds the rate; the alarm is what tells you the bill moved
    - _Requirements: 8.2, 8.3_

- [ ] 10. Checkpoint — deployed agent working end to end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Proof_Bundle
  - [ ] 11.1 [Builder] Capture the Proof_Bundle
    - Orientation_Message screenshot for a repository the Builder did not author
    - At least one failure-message screenshot from the Requirement 6 taxonomy
    - Transcripts from three public repositories of differing primary languages into `docs/proof/`
    - Clip of 90 seconds or less: URL submission, the Orientation_Message, one follow-up question and its answer
    - `docs/proof/manifest.json` listing the deployed URL, the public source URL, and every captured artifact
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

- [ ] 12. Builder Center article
  - [ ] 12.1 Draft `docs/article.md`
    - At least 500 words across four sections: what it does, how it was built (naming Amazon Bedrock Nova Lite, AWS Lambda, AWS Lambda Function URLs, Amazon CloudWatch, Amazon SNS), the build experience, and the proof
    - Reference at least two Proof_Bundle items as evidence that the Orientation_Message and the failure messages improved the experience; link both URLs from the manifest
    - The build-experience section is the natural place for the scope cut: what was designed, what was dropped, and why one Lambda behind a Function URL beat five services for a weekend
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [ ] 12.2 [Builder] Publish the article on builder.aws.com with the `#agents` tag
    - _Requirements: 10.7_

- [ ] 13. [Builder] Builder_Profile and submission
  - [ ] 13.1 [Builder] Complete the profile and submit
    - On builder.aws.com: profile image, selected country, About section of at least one sentence
    - Confirm all three at publication time, then submit before the October 12, 2026 close
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 12.5_

## Notes

- Tasks marked `[Builder]` require a human: console clicks, a confirmation email, a screen recording, a published article, a profile edit. An executing agent should stop and hand these back.
- **Checkbox state is current as of the deploy.** Tasks 1 through 9.1 are confirmed complete and ticked, verified by the offline suite (680 tests passing) plus the live run recorded under 9.1. What is left is the five tasks that need a human: 9.2 (confirm the SNS subscription), 11.1 (capture the Proof_Bundle), 12.1 and 12.2 (the article), and 13.1 (profile and submission).
- Four property tests remain (Properties 1, 2, 4, 6) and all four pass. The other fifteen were dropped in favor of the unit tests named in each task's bullets — see `docs/out-of-scope.md` entry 7 for which guarantees that gives up.
- Task 1.4 gated every deployment task mechanically and is now passed: us-east-1, `amazon.nova-lite-v1:0`. `scripts/deploy.sh` still checks the `.preflight-ok` marker, so a fresh clone re-runs the gate.
- Task 1.5 stayed in task 1 rather than moving beside the alarm in 7.1 on purpose: `EstimatedCharges` publishes roughly every six hours, so the account preference needs a head start.
- The billing alarm and its SNS topic moved into the main template. That works only because the Target_Region is us-east-1, the one region where the metric publishes. If the region ever changes, the alarm has to split back into its own us-east-1 stack.
- The deployed endpoint is public, unauthenticated, and has no rate limit or daily quota. The reserved concurrency of 2 this note used to cite does not exist: it was refused by the account's 10-execution limit on the first deploy (see 9.1). The controls are the account concurrency ceiling, the invocation alarm at 175 per 5 minutes, the 8,192-byte body cap, the 600-token output cap, and the 10 USD billing alarm. That trade and its corrected numbers are recorded in `docs/out-of-scope.md` entry 9 — read it before pointing anything but a demo at this stack.
- Requirement 4 AC7 and AC8 are `WHERE the Strands Agents SDK is used`. The design uses a direct Converse call, so both are vacuously satisfied and have no task.
- Requirement 12 AC6 has no task of its own: anything proposed mid-build that falls outside Requirements 1–11 goes into `docs/out-of-scope.md`, which now holds nine entries.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.5"] },
    { "id": 1,  "tasks": ["1.2", "2.1", "2.3", "2.5", "3.1"] },
    { "id": 2,  "tasks": ["1.3", "1.4", "2.2", "2.4", "2.6", "3.2", "3.3"] },
    { "id": 3,  "tasks": ["3.4", "4.1", "7.1", "8.1"] },
    { "id": 4,  "tasks": ["4.2", "5.1"] },
    { "id": 5,  "tasks": ["4.3"] },
    { "id": 6,  "tasks": ["5.2"] },
    { "id": 7,  "tasks": ["8.2"] },
    { "id": 8,  "tasks": ["9.1"] },
    { "id": 9,  "tasks": ["9.2"] },
    { "id": 10, "tasks": ["11.1"] },
    { "id": 11, "tasks": ["12.1"] },
    { "id": 12, "tasks": ["12.2"] },
    { "id": 13, "tasks": ["13.1"] }
  ]
}
```
