# Codebase Welcome Guide: an agent that reads a repository so you don't have to start cold

#agents

> **DRAFT — not publishable yet.** The two URLs are now real. What remains
> `<PLACEHOLDER>` in the text below is the Proof_Bundle: captured artifacts that
> genuinely do not exist yet.
>
> | Placeholder | Owner | Status |
> | --- | --- | --- |
> | Deployed URL | task 9.1 | **filled in** — stack deployed to us-east-1 |
> | Source repository URL | task 9.1 | **filled in** — repository is public |
> | Orientation screenshot, failure screenshot, clip, three transcripts | task 11.1 | outstanding — the Proof_Bundle has not been captured |
> | Proof repository names and their languages | task 11.1 | outstanding — carried by the transcript filenames |
>
> Only the two artifact rows are left, and both belong to task 11.1. Task 11.1
> also writes `docs/proof/manifest.json`, which must list the same two URLs this
> article now carries. Nothing in the Proof section should be published while it
> still reads `<PLACEHOLDER>`.

## What it does

You have been handed a repository you did not write. The README is either a
marketing page or a stub, the directory listing is forty entries deep, and the
question you actually have — *where do I start?* — is not one a file tree
answers.

The Codebase Welcome Guide answers it. You paste a public GitHub repository URL
into a chat box. About seven seconds later you get an Orientation_Message: what
the project is, what language and framework it is built in, one sentence on what
it does, at least three specific files worth opening first, and two follow-up
questions you could ask next. Then you can ask them, and anything else, against
the same ingested context.

What happens in between is fixed, and that is deliberate. The agent validates
the URL, fetches repository metadata, lists the default branch's tree, selects
up to twelve files by a five-tier priority order, reads them from
`raw.githubusercontent.com`, truncates each to 8,000 characters, fits the whole
thing inside a 30,000-token budget, and asks the model. No tools, no agent
loop choosing its own next move, no database, no server-side session. One HTTP
request does the entire job.

It reads public repositories anonymously. There is no sign-in, no token to
paste, and nothing stored between requests.

## How it was built

Python 3.12 on **AWS Lambda** — one function, 1024 MB, a 60-second timeout. It
sits behind an **AWS Lambda Function URL** with
`AuthType: NONE`, and that one function serves both halves of the product:
`GET /` returns the chat UI straight out of the deployment package, and
`POST /api/chat` runs the agent. Because one origin serves the page and the API,
every request is same-origin and there is no CORS configuration anywhere in the
project.

Inference is **Amazon Bedrock Nova Lite** (`amazon.nova-lite-v1:0`) through the
Converse API, at temperature 0.3 with a 600-token output cap. Nova Lite is the
right size for this: the job is summarizing text that is already in the prompt,
not reasoning hard, and the orientation has a 20-second budget to land in.

**Amazon CloudWatch Logs** holds the structured log line each request emits —
correlation id, per-step durations, token estimate against actual, and the
orientation repair count — with 7-day retention, set explicitly in the template
so the function cannot fall back to "never expire".

**Amazon SNS** carries the one alert the system has: a CloudWatch billing alarm
on `EstimatedCharges` at 10 USD, publishing to a topic with an email
subscription. The alarm and the topic live in the same stack as the function,
which works only because the region is us-east-1 — that metric publishes there
and nowhere else.

All of it is one AWS SAM template. No agent framework: the flow has no decision
to delegate, so a direct Converse call is the whole model layer.

## The build experience

Five things are worth writing down, because four of them were not in the plan.

**The preflight gate was deliverable zero.** Before any infrastructure existed,
I wrote a script that checks four things in order: that a region was explicitly
configured, that credentials resolve, that CloudFormation answers a read-only
probe, and that Nova Lite returns a real inference. It writes a marker file, and
the deploy script refuses to run without one newer than the template. Writing it
first paid for itself immediately — it is where I found that botocore's
`retries.max_attempts` counts *retries*, not attempts, and that the default
retry mode would have multiplied my three-attempt loop into fifteen real calls
against a model I was being billed for. "Up to 2 retries" is a claim in my
requirements. It would have been false.

**The Orientation_Message is a validate-and-repair step, not a prompt and a
hope.** You cannot promise that a small fast model will return five specific
things inside 20 seconds. So `orientation.finalize` inspects what came back,
appends whatever is missing from the manifest the agent already ingested, and
returns the list of repairs it had to make so the handler can log it as a
metric. Repairs are appended, never substituted, so a good answer is left
exactly as the model wrote it. The word cap is paid for *before* the repairs are
written, because capping first and appending second would either breach the
350-word limit or have the final cut remove the follow-up questions the repair
just added. A rising repair rate is the signal that the prompt needs work, and
in production it is the only way to know.

**The error taxonomy is one table.** Error code to HTTP status to the exact
user-facing sentence, cause first and next action last, one row per code, no
second place where error copy is written. The useful constraint is what the copy
is allowed to contain: static text plus whitelisted *numbers*. A string is
refused even under a whitelisted key, because a string is the one type that
could carry an ARN, an account id, a file path, or a token. That makes "never
leak internals" a property of the structure rather than a filter someone has to
remember to maintain.

**Repository content is untrusted input going into a prompt.** A README can be
written to address a model. The boundary has two halves: the context builder
rewrites any file line that opens with `=== ` so a file cannot forge a block
delimiter and close the context early, and the system prompt states that
everything in the context is data rather than instruction. Residual risk stays
low because the model has no tools, performs no writes, and its output returns
only to the caller who supplied the URL.

**The scope cut is the part I would tell another builder about.** The design
started with a CloudFront distribution over a private S3 bucket with Origin
Access Control, an API Gateway REST API with a usage plan, a generated API key,
a 500-request daily quota, and nineteen property-based tests. What shipped is one
Lambda behind a Function URL with four properties. Each removal is a numbered
entry in `docs/out-of-scope.md` with what was proposed, why it was declined, and
what would change the call — because a declined proposal with its reasoning is
more useful later than a silent omission. Two of those entries are
uncomfortable and say so. Entry 7 admits that dropping the property test for
"errors never leak internals" was a real loss and names the order to buy
properties back in. Entry 9 is not a deferred nicety at all: it is an accepted
*risk*. The endpoint is public with no rate limit, the account's concurrency
ceiling bounds the rate but nothing bounds the total, and the billing alarm can
lag the spend it reports by most of a day. That entry got worse during the
build, which is the useful part: the reserved concurrency of 2 it leaned on
would not deploy — this account's whole limit is 10 executions and AWS holds 10
back unreserved, so no reservation fits — and the real ceiling turned out to be
five times the rate the entry had priced. The fix was an alarm on invocations
that fires in minutes instead of hours, and a corrected set of numbers in the
entry. Bounded enough for a weekend demo, not bounded enough for anything else.
Writing that down was more useful than pretending the quota was still there.

## The proof

- **Live agent:** <https://ciy6fwtatvaifvve5xadk6fnvy0xelpz.lambda-url.us-east-1.on.aws/>
- **Source and infrastructure-as-code:** <https://github.com/Binod231/Agent-people-hackathon>

Two pieces of the Proof_Bundle are the evidence that the two design decisions
above actually changed the experience rather than just the code.

The first is the Orientation_Message screenshot
(`<PLACEHOLDER — docs/proof/orientation-*.png>`), captured against a repository
I did not author. It is the case for the repair step: the message names the
project, its language, what it does, specific files to open, and questions to
ask next — every time, including the runs where the model left something out and
`finalize` put it back. Compare it against the transcripts
(`<PLACEHOLDER — three transcripts in docs/proof/, differing primary
languages>`), which show the same five-part shape holding across three projects
in three different languages. A free-form summary would have been shorter to
build and would not have told you where to start.

The second is the failure-message screenshot
(`<PLACEHOLDER — docs/proof/failure-*.png>`), captured by pointing the agent at
a private repository. It reads as a plain sentence about what went wrong and
what to try next, rendered in the transcript like any other chat message rather
than as an error banner or a stack trace. That is the error table doing its job
in the place a user actually sees it: a dead end became a next step.

A clip of 90 seconds or less (`<PLACEHOLDER — docs/proof/walkthrough.mp4>`)
shows the whole path end to end: URL in, orientation out, one follow-up question
answered.

#agents
