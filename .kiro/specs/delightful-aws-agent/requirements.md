# Requirements Document

## Introduction

This feature is a submission for the AWS Builder Center "Build an Agent Weekend Challenge" (October 9–12, 2026). The feature delivers a codebase welcome guide agent: a newcomer pastes a public GitHub repository URL, asks "where do I start?", and receives a guided tour of the unfamiliar repository. The target audience is developers joining a codebase cold — new hires and open-source contributors.

The submission has two deliverables: a publicly reachable deployed agent (a web chat interface backed by Amazon Bedrock, AWS Lambda behind a Lambda Function URL, Amazon CloudWatch, and Amazon SNS) and a Builder Center article of at least 500 words covering the four required sections. The deployed link is the primary proof artifact for judges.

Scope is deliberately bounded to roughly two and a half days of build time. The requirements below favor a small, reliable, delightful experience over breadth. Cost and abuse controls on the public unauthenticated endpoint are in scope, not optional.

## Glossary

- **Welcome_Guide_Agent**: The complete deployed system, comprising the Chat_UI, the Agent_API, the Agent_Loop, and the Repo_Ingestor.
- **Chat_UI**: The static web chat interface bundled into the AWS Lambda deployment package and served by the Agent_API.
- **Agent_API**: The HTTP interface exposed by the AWS Lambda Function URL, which serves the Chat_UI static assets and forwards chat requests to the Agent_Loop.
- **Agent_Loop**: The Python agent implementation running on AWS Lambda that calls Amazon Bedrock and returns assistant messages.
- **Repo_Ingestor**: The component of the Agent_Loop that reads a public GitHub repository and assembles repository context for the model.
- **Repo_Context**: The assembled set of selected repository file contents, directory tree summary, and metadata passed to Amazon Bedrock within the Token_Budget.
- **Token_Budget**: The maximum number of input tokens the Agent_Loop may send to Amazon Bedrock in a single inference request.
- **Orientation_Message**: The first assistant message of a session, which summarizes what the Repo_Ingestor found in the submitted repository.
- **Preflight_Check**: A runnable verification step, executed before infrastructure deployment, that confirms AWS account access, confirmed target AWS Region, and Amazon Bedrock Nova Lite model access.
- **Target_Region**: The AWS Region, explicitly confirmed by the Builder, in which the Welcome_Guide_Agent is deployed.
- **Cost_Guard**: The combined AWS Lambda reserved concurrency limit, AWS Lambda timeout, request body size limit, scoped AWS IAM role, and Amazon CloudWatch billing alarm with Amazon SNS notification that bound spend and request volume.
- **Proof_Bundle**: The collected submission evidence, comprising the public deployed URL, screenshots, a screen recording, and the public source repository URL.
- **Article_Draft**: The Builder Center article text prepared for publication.
- **Builder_Profile**: The author's AWS Builder Center profile.
- **Builder**: The person building and submitting the Welcome_Guide_Agent.

## Requirements

### Requirement 1: AWS Preflight Verification

**User Story:** As the Builder, I want AWS account and model access verified before any dependent work begins, so that I do not lose weekend hours discovering a blocked account or an unavailable model.

#### Acceptance Criteria

1. THE Preflight_Check SHALL verify that an AWS account is active and that credentials with deployment permissions resolve successfully.
2. THE Preflight_Check SHALL verify Amazon Bedrock Nova Lite model access in the Target_Region by issuing one inference request and receiving a successful response.
3. THE Preflight_Check SHALL read the Target_Region from explicit Builder-supplied configuration.
4. IF the Target_Region is absent from configuration, THEN THE Preflight_Check SHALL exit with a non-zero status and report that the region requires explicit confirmation.
5. IF Amazon Bedrock Nova Lite model access in the Target_Region is denied, THEN THE Preflight_Check SHALL exit with a non-zero status and report the steps to request model access.
6. THE Preflight_Check SHALL complete and report pass or fail status within 60 seconds.
7. WHEN the Preflight_Check reports pass status, THE Builder SHALL proceed to infrastructure deployment.

### Requirement 2: Repository URL Intake and Validation

**User Story:** As a developer joining a codebase cold, I want to paste a repository URL and get started immediately, so that I spend my attention on the code instead of on setup.

#### Acceptance Criteria

1. WHEN a user submits a repository URL through the Chat_UI, THE Agent_API SHALL return an assistant message in the HTTP response.
2. THE Repo_Ingestor SHALL accept repository URLs matching the GitHub repository form `https://github.com/{owner}/{repo}`, with an optional trailing `.git` suffix or trailing slash.
3. THE Repo_Ingestor SHALL access GitHub through unauthenticated public API requests only.
4. THE Repo_Ingestor SHALL read repository content from the repository default branch.
5. IF a submitted URL fails the GitHub repository form check in Acceptance Criterion 2, THEN THE Chat_UI SHALL display a message naming the expected URL form and showing one valid example.
6. IF GitHub returns status 404 for a submitted repository, THEN THE Chat_UI SHALL display a message stating that the repository is private or does not exist, and that only public repositories are supported.
7. IF GitHub returns status 403 for a rate-limited request, THEN THE Chat_UI SHALL display a message stating that GitHub rate limits were reached and inviting the user to retry.

### Requirement 3: Bounded Repository Ingestion

**User Story:** As the Builder, I want repository ingestion bounded by explicit caps, so that a large repository produces a useful answer instead of a timeout or an oversized model bill.

#### Acceptance Criteria

1. THE Repo_Ingestor SHALL select repository files for Repo_Context using the following priority order: README files, dependency and build manifest files, configuration files, program entry point files, then top-level source directory representatives.
2. THE Repo_Ingestor SHALL include in Repo_Context a directory tree summary limited to the first two directory levels and to a maximum of 200 entries.
3. THE Repo_Ingestor SHALL select a maximum of 12 files for Repo_Context.
4. THE Repo_Ingestor SHALL truncate each selected file to the first 8,000 characters.
5. WHERE a selected file is truncated, THE Repo_Ingestor SHALL label the file as truncated in Repo_Context.
6. THE Agent_Loop SHALL cap the Token_Budget for a single Amazon Bedrock request at 30,000 input tokens.
7. THE Agent_Loop SHALL include a maximum of the 10 most recent conversation turns in each Amazon Bedrock request.
8. IF the GitHub-reported repository size exceeds 100 MB, THEN THE Chat_UI SHALL display a message stating that the repository exceeds the supported size and naming the 100 MB limit.
9. IF the repository default branch tree contains more than 5,000 files, THEN THE Chat_UI SHALL display a message stating that the repository exceeds the supported file count and naming the 5,000 file limit.
10. WHILE assembling Repo_Context, THE Repo_Ingestor SHALL skip files whose individual size exceeds 500 KB.
11. WHILE assembling Repo_Context, THE Repo_Ingestor SHALL skip files with binary content types.

### Requirement 4: Guided Tour Responses

**User Story:** As a developer joining a codebase cold, I want to ask where to start and what matters, so that I can orient myself in an unfamiliar repository without reading every file.

#### Acceptance Criteria

1. WHEN a user asks a question about an ingested repository, THE Agent_Loop SHALL return an assistant message grounded in Repo_Context.
2. THE Agent_Loop SHALL cite the repository file paths that support each answer.
3. WHEN a user asks where to start, THE Agent_Loop SHALL return a suggested reading order naming at least three repository file paths present in Repo_Context.
4. THE Agent_Loop SHALL return assistant messages of 350 words or fewer.
5. IF a user question concerns repository content absent from Repo_Context, THEN THE Agent_Loop SHALL state that the content was outside the ingested selection and name the files that were ingested.
6. WHEN the Agent_API receives a chat request, THE Agent_API SHALL return an HTTP response within 30 seconds.
7. WHERE the Strands Agents SDK is used for the Agent_Loop, THE Agent_Loop SHALL expose repository reading as an agent tool.
8. WHERE the Strands Agents SDK is used for the Agent_Loop, THE Agent_Loop SHALL satisfy Requirements 3 and 4 without modification.

### Requirement 5: Delightful First-Run Orientation

**User Story:** As a developer joining a codebase cold, I want the agent to greet me with what it actually found in my repository, so that my first moment is a warm orientation instead of an empty chat box.

#### Acceptance Criteria

1. WHEN a session receives a valid repository URL, THE Welcome_Guide_Agent SHALL return an Orientation_Message as the first assistant message, before any user question.
2. THE Orientation_Message SHALL name the repository and the primary language or framework detected in Repo_Context.
3. THE Orientation_Message SHALL describe the repository purpose in one sentence drawn from Repo_Context.
4. THE Orientation_Message SHALL name at least three ingested repository file paths as suggested starting points.
5. THE Orientation_Message SHALL offer at least two follow-up questions the user may ask next.
6. THE Welcome_Guide_Agent SHALL deliver the Orientation_Message within 20 seconds of repository URL submission.
7. WHILE the Orientation_Message is being generated, THE Chat_UI SHALL display a progress indicator naming the current ingestion step.

### Requirement 6: Helpful Failure Messages

**User Story:** As a user hitting a limit or an error, I want a message that tells me what happened and what to try next, so that a failure still feels like part of a considered experience.

#### Acceptance Criteria

1. THE Chat_UI SHALL render every error response as a chat message that states the cause in plain language and names one next action the user may take.
2. IF Amazon Bedrock returns a throttling error, THEN THE Agent_Loop SHALL retry the request up to 2 times with exponential backoff before returning an error message.
3. IF Amazon Bedrock returns a throttling error after the retries in Acceptance Criterion 2, THEN THE Chat_UI SHALL display a message stating that the model is busy and inviting the user to retry.
4. IF the Agent_Loop raises an unhandled exception, THEN THE Agent_API SHALL return HTTP status 500 with a generic user-facing message.
5. IF the Agent_Loop raises an unhandled exception, THEN THE Agent_Loop SHALL write the exception detail to Amazon CloudWatch Logs.
6. THE Agent_Loop SHALL restrict user-facing error messages to text that excludes AWS account identifiers, stack trace content, and credential values.

### Requirement 7: Public Web Deployment

**User Story:** As a judge reviewing the submission, I want to open one public link and use the agent immediately, so that I can evaluate the working agent without local setup.

#### Acceptance Criteria

1. THE Welcome_Guide_Agent SHALL be reachable at a public HTTPS URL served by an AWS Lambda Function URL configured without authentication.
2. WHEN the Agent_API receives an HTTP GET request for a Chat_UI asset path, THE Agent_API SHALL return that asset from the AWS Lambda deployment package with its corresponding content type.
3. THE Chat_UI SHALL render a usable chat layout at viewport widths of 390 pixels and 1280 pixels.
4. THE Chat_UI SHALL support keyboard-only operation for repository URL entry, message submission, and transcript navigation.
5. THE Chat_UI SHALL label the message input and the send control with accessible names.
6. WHEN an assistant message is rendered, THE Chat_UI SHALL announce the message through an ARIA live region.
7. THE Welcome_Guide_Agent SHALL hold conversation state in the Chat_UI client.
8. WHEN the Chat_UI sends a chat request, THE Chat_UI SHALL include the conversation history in the request body.
9. THE Welcome_Guide_Agent SHALL deploy through infrastructure-as-code committed to the public source repository.

### Requirement 8: Cost and Abuse Controls

**User Story:** As the Builder, I want hard limits on the public unauthenticated endpoint, so that traffic or abuse cannot produce a surprise bill.

#### Acceptance Criteria

1. THE Cost_Guard SHALL reject Agent_API chat requests whose body exceeds 8,192 bytes with HTTP status 413.
2. THE Cost_Guard SHALL configure an Amazon CloudWatch billing alarm that triggers when estimated monthly charges exceed 10 USD.
3. THE Cost_Guard SHALL deliver billing alarm notifications to a Builder-confirmed email address through an Amazon SNS topic.
4. THE Agent_Loop SHALL run with an AWS Lambda timeout of 60 seconds.
5. THE Agent_Loop SHALL run with an AWS Lambda reserved concurrency of 2.
6. THE Agent_Loop SHALL run under an AWS IAM role scoped to Amazon Bedrock Nova Lite model invocation in the Target_Region and Amazon CloudWatch Logs write access.

### Requirement 9: Proof Capture

**User Story:** As a judge reviewing the submission, I want evidence that the agent works and that the experience lands, so that I can verify the claims in the article.

#### Acceptance Criteria

1. THE Proof_Bundle SHALL contain the public deployed URL of the Welcome_Guide_Agent.
2. THE Proof_Bundle SHALL contain the public source repository URL holding the agent code and infrastructure-as-code.
3. THE Proof_Bundle SHALL contain a screenshot of an Orientation_Message produced for a repository the Builder did not author.
4. THE Proof_Bundle SHALL contain a screenshot of at least one failure message from Requirement 6.
5. THE Proof_Bundle SHALL contain a screen recording of 90 seconds or less showing repository URL submission, the Orientation_Message, and one follow-up question with the corresponding answer.
6. THE Proof_Bundle SHALL contain transcripts from at least 3 distinct public repositories of differing primary languages.

### Requirement 10: Builder Center Article

**User Story:** As the Builder, I want a complete article that meets every stated submission rule, so that the entry qualifies for judging.

#### Acceptance Criteria

1. THE Article_Draft SHALL contain at least 500 words.
2. THE Article_Draft SHALL contain a section describing what the Welcome_Guide_Agent does.
3. THE Article_Draft SHALL contain a section describing how the Welcome_Guide_Agent was built, naming Amazon Bedrock Nova Lite, AWS Lambda, AWS Lambda Function URLs, Amazon CloudWatch, and Amazon SNS.
4. THE Article_Draft SHALL contain a section describing the build experience.
5. THE Article_Draft SHALL contain a section presenting proof that the Welcome_Guide_Agent works, linking the public deployed URL and the public source repository URL from the Proof_Bundle.
6. THE Article_Draft SHALL reference at least 2 Proof_Bundle items as evidence that the Orientation_Message and the failure messages improved the user experience.
7. THE Article_Draft SHALL carry the `#agents` tag.

### Requirement 11: Builder Profile Completeness

**User Story:** As the Builder, I want my Builder Center profile complete before submission, so that an incomplete profile does not disqualify a working entry.

#### Acceptance Criteria

1. THE Builder_Profile SHALL contain a profile image.
2. THE Builder_Profile SHALL contain a selected country.
3. THE Builder_Profile SHALL contain an About section of at least 1 sentence.
4. WHEN the Article_Draft is published, THE Builder SHALL confirm that Acceptance Criteria 1 through 3 are satisfied.

### Requirement 12: Weekend Delivery Scope

**User Story:** As the Builder, I want the scope fixed to what fits one weekend, so that the submission is complete and working rather than broad and unfinished.

#### Acceptance Criteria

1. THE Welcome_Guide_Agent SHALL implement the behaviors defined in Requirements 2 through 8 as the complete functional scope of the Welcome_Guide_Agent.
2. THE Welcome_Guide_Agent SHALL serve anonymous sessions.
3. THE Welcome_Guide_Agent SHALL retain conversation content for an Amazon CloudWatch Logs retention period of 7 days.
4. THE Welcome_Guide_Agent SHALL read repository content from GitHub public endpoints at request time in each session.
5. WHEN Requirements 1 through 11 are satisfied, THE Builder SHALL submit the entry before the contest close on October 12, 2026.
6. IF a requirement outside Requirements 1 through 11 is proposed during the build, THEN THE Builder SHALL record the proposal as out of scope for this submission.
