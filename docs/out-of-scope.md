# Out of scope

Requirement 12 AC6: anything proposed during the build that falls outside
Requirements 1 through 11 is recorded here rather than implemented. The entry
says what was proposed, why it was declined for this submission, and what would
change the call. Nothing is deleted from this log — a declined proposal with its
reasoning is more useful later than a silent omission.

Entries 1 through 4 were recorded during the design, before implementation
started. Entries 5 through 9 were recorded mid-build, when the deployment
architecture was cut to one Lambda behind a Function URL and the scope was
trimmed to what a hackathon weekend actually finishes.

One caveat on reading the early entries: they cite acceptance-criteria numbers
as they stood before that cut. Requirement 6 AC4 and Requirement 8 AC1, AC2, and
AC9 are gone and the criteria after them renumbered. The old-to-new mapping is in
`design.md` → Lean Deployment Revision → Acceptance Criteria Renumbering.

## 1. A GitHub token for a higher rate limit

**Proposed:** store a GitHub personal access token in AWS Secrets Manager and
send it on the two metered API calls, raising the anonymous allowance of roughly
60 requests per hour per IP to 5,000.

**Declined because:** it adds a secret to manage and a credential to rotate on a
public unauthenticated endpoint, for a demo whose own usage plan caps traffic at
500 requests per day (Requirement 8 AC2). File bodies already come from
`raw.githubusercontent.com`, which is not metered, so only two metered calls
remain per request. When the allowance is hit the user gets the
`GITHUB_RATE_LIMITED` message with a wait computed from `x-ratelimit-reset`
(Requirement 2 AC7) rather than a dead end.

**What would change the call:** sustained traffic, or a judging window where a
shared egress IP makes the anonymous allowance unreliable.

**Design reference:** Key Decisions → Read File Bodies From `raw.githubusercontent.com`.

## 2. Repository reading as an agent tool via the Strands Agents SDK

**Proposed:** build the Agent_Loop on the Strands Agents SDK and expose
repository reading as a tool the model calls, which would bring Requirement 4
AC7 and AC8 into play.

**Declined because:** the flow has no decision to delegate — validate, fetch
metadata, list the tree, select, read, ask is fixed before the request arrives.
Tool calling would hand the token budget to the model, when Requirement 3 AC3
(12 files) and AC6 (30,000 tokens) are our guarantees to keep, and each tool
round trip costs an extra inference against the 20-second Orientation_Message
budget (Requirement 5 AC6). Both criteria are `WHERE the Strands Agents SDK is
used`, so declining leaves them vacuously satisfied.

**What would change the call:** growing the product to answer questions that need
files outside the initial selection. A read-file tool becomes genuinely useful
then, and Strands becomes the right call.

**Design reference:** Key Decisions → No Agent Framework: Direct Bedrock Converse.

## 3. A cross-request ingestion cache

**Proposed:** cache repository metadata, the tree listing, and file bodies in the
warm Lambda container, keyed by `owner/repo`, to save roughly 1.1 seconds per
turn.

**Declined because:** Requirement 12 AC4 requires reading repository content from
GitHub public endpoints at request time in each session. A warm-container cache
would serve one user's session from another session's data, which reads against
that requirement, and Property 18 asserts the agent holds no state between
requests. The 30-second allowance (Requirement 4 AC6) absorbs the re-ingestion
comfortably.

**What would change the call:** a requirement change permitting shared caching,
or latency measurements that leave no headroom against Requirement 5 AC6. The
cheaper fix in that case is the already-parameterized escape hatch —
`ORIENTATION_FILE_COUNT` to 2 and `ORIENTATION_FILE_CHARS` to 1,500.

**Design reference:** Key Decisions → No Cross-Request Cache.

## 4. AWS WAF rate-based rules on the distribution

**Proposed:** attach AWS WAF to the CloudFront distribution with rate-based rules
and a bot-control rule set, and enforce the 8,192-byte body cap at WAF instead of
in the handler.

**Declined because:** Requirement 8's controls already bound spend — 2 requests
per second with a burst of 5 (AC1), 500 requests per day (AC2), reserved
concurrency of 5 (AC6, AC7), and a billing alarm at 10 USD (AC4). WAF adds a web
ACL, rule tuning, and its own per-request charge on top of that, and a WAF size
constraint returns 403 rather than the 413 Requirement 8 AC3 names, so the
handler guard would have to stay anyway.

**What would change the call:** a non-contest deployment. The same note records
Cognito or a signed-link authorizer in front of the API and per-identity quotas
instead of one shared key, all for the same reason: this endpoint is public and
unauthenticated by requirement (Requirement 7 AC1, Requirement 12 AC2), and the
compensating controls are sized for a weekend demo rather than production.

**Design reference:** Key Decisions → Public Endpoint With No Authentication.
## 5. Amazon CloudFront in front of a private Amazon S3 bucket

**Proposed:** host the three Chat_UI files in a private S3 bucket, put one
CloudFront distribution in front of it with Origin Access Control, and add a
second behavior routing `/api/*` to API Gateway so the API stays same-origin.
This is Requirement 7 AC1 and AC2 as they were originally written.

**Declined because:** a Lambda Function URL gives the same public HTTPS endpoint
with no distribution, no bucket, no bucket policy, no OAC, and no cache
invalidation step — and the function can return the three static files itself,
since they total a few tens of kilobytes against a 6 MB response payload
ceiling. The same-origin property that justified a single distribution with two
origins comes for free when one origin serves both halves, so the CORS work the
distribution was avoiding is still avoided. What the distribution bought beyond
that was edge caching and a `*.cloudfront.net` certificate: a demo whose own
Lambda is the bottleneck does not need the first, and
`*.lambda-url.us-east-1.on.aws` covers the second. Removing it deleted roughly
eighty lines of template, the `publish-ui.sh` script, and three of the five
cases `smoke.sh` existed to check.

**What would change the call:** real traffic, where edge caching and request
absorption start to matter; a custom domain, which needs an ACM certificate and
a distribution to attach it to; or any need for AWS WAF, which attaches to
CloudFront and not to a Function URL. A UI iteration loop that a full redeploy
makes too slow would also change it — with CloudFront the loop was `s3 sync`
plus an invalidation.

**Design reference:** Lean Deployment Revision → Revised Architecture, which
supersedes Key Decisions → Single CloudFront Distribution, Two Origins.

## 6. Amazon API Gateway REST with a usage plan, API key, and daily quota

**Proposed:** a REST API in front of the Lambda, throttled at 2 requests per
second with a burst of 5, carrying a usage plan with a 500-requests-per-day
quota, a generated API key injected by CloudFront as an origin custom header,
and `GatewayResponses.THROTTLED` and `.QUOTA_EXCEEDED` returning 429 copy with a
`Retry-After` header. This is Requirement 8 AC1, AC2, and AC9, plus Requirement
6 AC4, as they were originally written.

**Declined because:** every piece of it existed to carry the quota, and the
quota pulled in a chain of consequences. It forced a REST API rather than the
simpler HTTP API, because v2 supports neither usage plans nor keys. It forced
the key value through a `NoEcho` stack parameter and an `openssl rand` step in
the deploy script, because CloudFormation does not return the key value it
generates. It forced gateway response templates to make a 429 read like the rest
of the error copy. And it forced two of the five `smoke.sh` cases, which existed
only to prove that the CloudFront origin header overrode a viewer-supplied one.
That is most of an afternoon of configuration, plus a second afternoon of
debugging the first one, for a control a hackathon demo gets at by capping
concurrency instead. Requirement 6 AC4's retry-wait message goes with it, since
nothing in the system can now return a 429, and the `RATE_LIMITED` error code
comes out of the taxonomy for the same reason.

Two knock-on effects worth naming rather than leaving implicit. The residual
exposure — unbounded request volume on a public endpoint — is entry 9, and it is
the one entry in this log that is a real accepted risk rather than a deferred
nicety. And entry 1's reasoning leaned partly on "a demo whose own usage plan
caps traffic at 500 requests per day"; that cap is gone, so the case against a
GitHub token now rests on the Lambda concurrency ceiling and on there being only
two metered GitHub calls per request. The conclusion still holds for a demo, but
it is a weaker argument than it was — and weaker again since the reservation
turned out to be undeployable and that ceiling became 10 rather than 2 (entry
9).

**What would change the call:** any deployment that outlives the judging window.
Putting this back is the cheapest way to get a real rate limit and a real daily
cap, and it is fully specified already — Component 7 of `design.md` names every
resource and value, so re-adding it is transcription rather than design.

**Design reference:** Lean Deployment Revision → Cost_Guard After the Revision,
which supersedes Key Decisions → The API Key Is a Quota Bucket, Not a
Credential.

## 7. Fifteen of the nineteen correctness properties

**Proposed:** nineteen `hypothesis` properties covering 38 acceptance criteria,
one per invariant, as enumerated in the design's Correctness Properties section.

**Declined because:** four were kept and fifteen dropped, on one test: does the
property's input space contain cases a human would not write by hand? Properties
1, 2, 4, and 6 do — URL surface forms and malformed near-misses, branch names
needing URL escaping, arbitrary tree shapes, content of every length around an
8 000-character cut. Those four are written and passing. The other fifteen
mostly assert a bound on a single function, where two or three hand-written
cases pin the behavior just as firmly: a 349/350/351-word cap, an 8 193-byte
body, an empty model output, a retry count of three. Each dropped property is
replaced by named unit tests in `tasks.md`, so the behavior is still checked;
what is given up is a generator finding the input nobody thought of.

Which is a real loss on two of them, and it should be said plainly. Property 13
(user-facing errors never leak internals) and Property 9 (input guards
short-circuit before any outbound call) have the worst failure modes in the
list — an ARN or an account id in a message shown to a stranger, or a guard that
fires after the Bedrock call rather than before it. Both now rest on example
tests over adversarial strings someone had to think of. If there is time to buy
properties back, the order is 13, then 9, then 7 (the token budget), then 11
(the Orientation_Message repair).

**What would change the call:** time, simply. Or a bug found in the area of a
dropped property, which is direct evidence that the generator would have earned
its keep there.

**Design reference:** Lean Deployment Revision → Correctness Properties,
Reduced.

## 8. Post-deploy and submission verification scaffolding

**Proposed:** four pieces of verification tooling — static assertions over the
rendered SAM template, `scripts/smoke.sh` run against the live URL, a formal
accessibility audit including a screen-reader pass with VoiceOver or NVDA, and
`scripts/check_submission.py` to mechanically validate the Proof_Bundle and the
article.

**Declined because:** each was sized for an architecture that is gone or for a
submission larger than this one. The template assertions existed mainly to pin
Cost_Guard numbers, and most of the values they checked — 2 rps, burst 5,
500 per day, the 29 000 ms integration timeout — no longer exist; the ones that
remain (timeout 60, retention 7, and the two alarm thresholds) sit in a short
template where a reader verifies them by looking. The concurrency reservation
that was on that list is not there to assert any more, which entry 9 covers. `smoke.sh` had five cases and three were about
CloudFront origin headers and S3 bucket privacy. The formal accessibility audit
is most of a day once tooling and a screen reader are set up, and the criteria it
would verify (Requirement 7 AC3 through AC6) are built into the markup from the
first commit — a real `<label for>`, a live region present before the first
message, a skip link, keyboard-only operation — with task 9.1 keeping the cheap
version: a keyboard walkthrough and a layout check at 390 px and 1280 px.
`check_submission.py` automates checks against artifacts that are produced once,
by one person, who can count five sections and 500 words.

**What would change the call:** a second deployment or a second submission —
anything done more than once earns a script. And any claim of WCAG conformance
would require the formal audit plus assistive-technology testing; this
submission claims working accessibility basics, which is a different and smaller
claim.

**Design reference:** Lean Deployment Revision → the removal table; Testing
Strategy → What Can Only Be Verified Against Live AWS.

## 9. A rate limit on the public Function URL (an accepted risk, not a deferral)

**Proposed:** keep some bound on request volume in front of the agent — API
Gateway with the original usage plan, AWS WAF rate-based rules, or a counter in
the handler — rather than relying on the Lambda concurrency ceiling alone.

**Declined because:** the usage plan left with API Gateway (entry 6), WAF has
nothing to attach to without CloudFront (entry 5), and a container-local counter
resets with the container. For a demo judged on one link opened by a handful of
people, the remaining controls were accepted as enough.

Then one of those remaining controls turned out not to exist. The first deploy
failed its stack create on `ReservedConcurrentExecutions: 2`: AWS holds 10
concurrent executions back as unreserved, and this account's entire
`ConcurrentExecutions` limit is 10 — the new-account value, where the default is
1000. No reservation of any size fits under that floor, including 1. The
reservation was attempted, refused by the account limit, and removed from the
template; a limit increase needs an AWS Support request and will not arrive
before judging. The compensating control added in its place is a second
CloudWatch alarm, on `AWS/Lambda` `Invocations`, at 175 in a 5-minute window.

Here is exactly what that leaves, stated plainly so nobody has to infer it:

- The Function URL is public and unauthenticated by requirement (Requirement 7
  AC1, Requirement 12 AC2). There is no rate limit and no daily quota.
- The only controls are the account's 10-execution concurrency ceiling, the
  invocation alarm at 175 per 5 minutes, the 8 192-byte body cap, a 600-token
  output cap, and the 10 USD billing alarm. The ceiling is account-wide rather
  than a per-function guarantee: any other function deployed into this account
  draws from the same 10.
- Nova Lite at roughly 27 000 input tokens and 600 output tokens per chat turn
  costs about $0.0016 in input and a fraction of a cent in output — call it
  $0.0018 a call, input-dominated.
- Concurrency 10 at roughly 7-second turns is about 85 calls a minute, or
  roughly $0.15 a minute and $9 an hour. Sustained abuse reaches the 10 USD
  alarm threshold in a little over an hour. Those are five times the figures
  this entry carried when it assumed a reservation of 2, which read 17 calls a
  minute, $1.80 an hour, and five to six hours.
- `AWS/Billing EstimatedCharges` publishes roughly every six hours, so that
  alarm can be most of a day behind the spend it is reporting — and at an hour
  to the threshold, the spend now outruns its own publish interval. That is
  what the invocation alarm is for: `Invocations` publishes every 60 seconds, so
  abuse is reported in minutes rather than after the fact. It notifies; it does
  not throttle. The concurrency ceiling bounds the *rate*; nothing in the system
  bounds the *total*. A weekend of undetected abuse is a two-figure bill, not a
  four-figure one, but it is not a bound — it is a fast alarm and a slow alarm
  on an unbounded meter.
- GitHub's anonymous allowance acts as an accidental second brake: two metered
  calls per request means sustained load starts returning
  `GITHUB_RATE_LIMITED`, which fails cheap instead of expensive. That is luck,
  not a control, and it should not be counted as one.

So the honest version: this is bounded enough for a weekend demo and not bounded
enough for anything else. It is five times less bounded than this entry claimed
before the deploy, and the alarm that now covers the gap is a notification
rather than a limit.

One more thing got worse after the deploy, and it was a deliberate choice rather
than a surprise. The source repository is now public, and it carries the
deployed Function URL in its README — so the endpoint is no longer a link shared
with judges, it is a link anyone reading the repository can find and replay. The
submission requires both to be public (Requirement 9 AC1 and AC2), so this is
the cost of the requirement rather than an oversight, but it moves this entry
from "an unauthenticated endpoint a few people know about" to "an
unauthenticated endpoint with no rate limit, published". The exposure priced
above is the same arithmetic; the population that can reach it is not. The
intended end state is therefore to take the stack down once judging is over —
`aws cloudformation delete-stack --stack-name codebase-welcome-guide --region
us-east-1` — not to leave it running indefinitely because nothing has gone wrong
yet. A published URL on an unbounded meter is a bill waiting for someone bored
enough to write a loop.

**What would change the call:** any deployment that is not this demo — a link
that stays up after judging, a link posted somewhere public, or any sign in the
logs of traffic that is not a person reading a repository. The second of those
has now happened by design, which is why the delete-stack step above is part of
the plan rather than a contingency. A raised account
`ConcurrentExecutions` limit would also change it, in the narrow sense that it
makes `ReservedConcurrentExecutions: 2` deployable again: one line back in
`infra/template.yaml`, and the rate ceiling returns to 17 calls a minute. That
is the first thing to do if the Support request lands. The cheapest fix without
waiting on AWS is to put the original API Gateway usage plan back: the REST API, the 2 rps
throttle with a burst of 5, the 500-per-day quota, and the key that meters it
are all specified in Component 7 of `design.md`, so it is an afternoon of
transcription rather than a redesign. Cognito or a signed-link authorizer is the
next step up and defeats the open-link property the submission depends on, which
is why it is not the first move.

**Design reference:** Lean Deployment Revision → What This Costs Us; Key
Decisions → Public Endpoint With No Authentication.
