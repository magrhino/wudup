# Security Policy

## Supported Versions

Security fixes target the current `main` branch and the next stable release.
Please reproduce issues on the latest tagged release, GHCR image tag, or commit
available to you before reporting when practical.

Older release tags are immutable deployment references. Upgrade to a current
release before reporting unless the older version is needed to explain impact
or regression history.

## Reporting A Vulnerability

Report security vulnerabilities privately through GitHub Security Advisories:

<https://github.com/magrhino/wudup/security/advisories/new>

Do not open public issues, discussions, or pull requests for suspected
vulnerabilities until a fix or disclosure plan is agreed.

Include enough detail to reproduce and triage the issue:

- The WUDup version, image tag, or commit.
- The deployment method: host install, Docker Compose image, local build, or
  source checkout.
- Relevant configuration, Compose snippets, command output, logs, and minimal
  reproduction steps.
- Whether the issue requires Docker socket access, WebUI exposure, WUD callback
  inputs, release-note webhooks, or TrueNAS status checks.

Redact secrets and machine-specific details. Do not include real Discord
webhook URLs, GitHub tokens, browser session cookies, setup or reset claims,
private service URLs, private hostnames, absolute home-directory paths, or
other credentials. Use placeholders when a value shape matters.

## Security Scope

Reports are in scope when they show that WUDup does something outside
the documented trust boundaries, including:

- WebUI authentication, session, CSRF, Origin, Host, trusted proxy, or secure
  cookie bypasses.
- Browser-triggered Docker mutations when `WUD_WEB_MUTATIONS_ENABLED` is not
  enabled, or mutation requests that bypass authentication, CSRF checks, or the
  plan-first apply flow.
- Mutating Docker operations that occur during `--dry-run`, without
  interactive confirmation, or without `--yes`.
- Secret exposure in logs, generated files, API responses, release-note helper
  output, issue templates, workflow logs, or public documentation.
- Command injection, path traversal, unsafe deserialization, unsafe log access,
  or unsafe file writes from WUD callback fields, image names, Compose metadata,
  environment variables, or WebUI inputs.
- Unsafe managed WUD script sync behavior, Compose tag rewrites, tag exclusion
  writes, or update-file locking that can write outside the intended mounted
  directories.
- GitHub Actions, release, dependency, or container-publishing weaknesses that
  could let untrusted code publish artifacts or exfiltrate repository secrets.

## Documented Trust Boundaries

The following behaviors are documented operational risks, not vulnerabilities
by themselves:

- Mounting `/var/run/docker.sock` gives WUDup root-equivalent control of
  the host Docker daemon. Only run trusted images with that socket.
- The hardened socket-proxy example reduces direct socket exposure, but Docker
  Compose pull, stop, and recreate operations still require proxy `POST=1`.
- The TrueNAS status helper does not use a TrueNAS API key, but enabling
  `TRUENAS_STATUS_CHECK=true` gives a short-lived helper trusted-host access to
  the local TrueNAS middleware socket for read status methods.
- `updates --yes`, `docker-update-from-wud --yes`, and
  `WUD_WEB_MUTATIONS_ENABLED=true` intentionally allow update mutations within
  the documented controls.
- Optional GitHub and Discord release-note integrations use tokens and webhooks
  supplied through environment variables or host-local secret stores.

## Security Defaults And Expectations

WUDup defaults to non-mutating operation where practical:

- The container image starts the WebUI by default, with browser mutations
  disabled unless `WUD_WEB_MUTATIONS_ENABLED=true` is set.
- Explicit helper and updater `--dry-run` commands must not pull images,
  recreate containers, remove WUD lines, or otherwise mutate host state.
- Mutating Docker operations require interactive confirmation or `--yes`.
- Browser sessions use HttpOnly cookies and CSRF protection. `WUD_WEB_TOKEN` is
  only an optional API bearer token; it is not accepted by the browser login form
  and does not bypass first-run setup.
- `WUD_WEB_DEV_NO_AUTH=true` is for local development and tests only.

Deployments should keep Docker socket, stack, script, output, log, and database
mounts scoped to the directories WUDup needs. When exposing the WebUI
outside loopback, set the browser-visible public origin, allowed hosts, trusted
proxy addresses, and secure-cookie behavior intentionally.

Secrets such as Discord webhooks and GitHub tokens must come from environment
variables, Compose secrets, or host-local configuration. Do not commit secrets
to this repository. The callback scripts redact webhook values in helper logs
where those commands are printed.

## Automated Checks

The repository keeps security checks high-signal and cost-conscious:

- Dependabot checks Python, npm, and GitHub Actions dependencies on a weekly
  schedule.
- Renovate checks Dockerfile image tags on a weekly schedule.
- CodeQL scans Actions, Python, and WebUI JavaScript/TypeScript.
- The security workflow runs workflow auditing with zizmor, failing on high
  severity findings with medium or higher confidence.
- Dependency Review blocks high and critical severity dependency changes on
  public pull requests, including runtime, development, and unknown scopes.
  It checks license expressions against the routine-approval list below;
  missing or unresolved license metadata also fails the check.
- OSSF Scorecard runs as an advisory signal on non-PR events when the
  repository is public.
- SonarCloud Code Analysis is a required merge check. Its new-code quality gate
  and scoped installation/build exceptions are documented in
  [SonarQube triage](docs/SONAR_TRIAGE.md). Successful analysis alone is not a
  passing quality gate.

These checks do not replace private vulnerability reporting.

## SAST And SCA Thresholds

Static application security testing (SAST) covers application code and GitHub
Actions. Software composition analysis (SCA) covers direct and transitive Python
and npm dependencies, container OS packages and bundled tools, and build/CI
dependencies. A development-only dependency still matters if it can compromise
the build, credentials, or published artifacts.

| Finding | Required disposition |
|---|---|
| SAST high or critical security severity | Block merge and release until fixed or dismissed with the evidence below. |
| SCA high or critical advisory severity | Block merge of the dependency change and release of affected artifacts until fixed, removed, or covered by an approved, applicable `not_affected` VEX record. |
| zizmor high severity, medium or higher confidence | Block merge and release until fixed or dismissed with evidence. |
| Medium or low security severity | Record an owner, impact assessment, and remediation target; normally non-blocking unless the conditions below apply. |
| Unknown severity or conflicting assessments | Triage before release; absence of a score is not evidence of safety. Use the higher reported severity until a maintainer records a supported reassessment. |

Use security severity, not a tool's generic warning/error level. Review
reachability and impact against supported deployments, including opt-in features.
Active exploitation or a demonstrated bypass of authentication, mutation
confirmation, secret protection, or release integrity blocks release regardless
of the scanner's severity. A missing upstream fix, lack of a public exploit, or
passing tests does not make a finding non-exploitable.

## Dependency License Rules

Before adding or updating a dependency, retain its version, upstream license
reference, SPDX license identifier or expression, and whether it is redistributed
in Python packages, browser assets, or either container image variant. Include
transitive dependencies and base-image packages in release review.

- MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, ISC, Apache-2.0, 0BSD, CC0-1.0,
  Unlicense, BlueOak-1.0.0, PSF-2.0, Python-2.0, BSL-1.0, and Zlib are eligible
  for automated routine approval when their applicable notices and other
  obligations are satisfied. Dependency Review's retained JSON inventory and job
  summary serve as the version and license review record; existing manifest and
  packaging boundaries establish the distribution scope for routine updates.
- Copyleft licenses, including GPL, LGPL, MPL, and AGPL, require explicit
  maintainer review of compatibility with this repository's GPLv3 license and
  the actual linking, bundling, modification, and distribution model. Copyleft
  is not automatically prohibited; required source, notices, and other terms
  must be satisfied before distribution.
- Unknown, missing, custom, proprietary, noncommercial, or source-available
  licenses require review and documented permission compatible with the intended
  use and redistribution. Block inclusion until that evidence exists; public
  source availability alone is not permission.
- For dual licensing, record the selected permitted license. For combined
  license obligations, satisfy all applicable terms. Preserve required notices
  and attribution in shipped artifacts and provide required corresponding
  source or source offers.

An incompatible license or unmet obligation blocks distribution. A security risk
exception cannot waive license terms. Build-only tools need their own usage
review even when they are not redistributed. Dependency Review enforces the
routine license list for detected changes; it does not prove that notices or
source obligations are satisfied. New distribution models and licenses outside
that list require maintainer review. License exceptions must identify the exact
package version, license text, obligations, and approving review; do not add a
package-wide license exclusion just to make an update green.

## Routine Dependency Auto-Merge

Verified Dependabot minor/patch updates and eligible Renovate Docker updates can
merge without a separate human sign-off when all of these conditions hold:

- The current PR contains only the verified dependency bot commit and is not a
  draft. Major Dependabot updates are not eligible.
- The latest `ci`, `security`, and `CodeQL Advanced` PR runs for that exact head
  commit have completed successfully. Core Python, shell, WebUI, dependency
  review, workflow auditing, and all three CodeQL analysis jobs must actually
  pass; missing or skipped core jobs do not qualify. Optional checks may be
  skipped by their normal path/label conditions, but any failure in a selected
  workflow prevents auto-merge.
- All repository-required checks pass, including CodeQL's findings check and
  SonarCloud Code Analysis, and
  GitHub reports the PR as cleanly mergeable. The final merge is conditional on
  the verified head commit still being current.

For routine updates within existing usage and distribution boundaries, successful
check summaries are the automated review evidence authorized by this policy.
There is no per-update manual approval form or VEX requirement for dependencies
with no finding. Non-blocking lower-severity alerts remain subject to the response
targets below and do not require pre-merge approval. Changes needing a license
exception, vulnerability dismissal,
or VEX assessment remain open for maintainer review; automation must not invent
evidence or suppress a finding to make them eligible. A VEX record alone does
not clear GitHub's Dependency Review check: fix/remove the dependency or have a
maintainer handle the specific exception using the evidence requirements below.

GitHub Dependency Review does not inspect container OS packages or every bundled
tool. Renovate's green build and dependency checks do not establish that an image
has no vulnerabilities; the artifact review requirements below still apply.

## Release-Blocking Conditions

Before creating a release tag or starting publication, the releasing maintainer
must retain a security sign-off in the release PR or private security tracking
record, with links to checks, findings, and their dispositions. Routine dependency
PR check results may be reused as evidence for unchanged decisions; they do not
require repeated manual license approval. Retain relevant dependency-review JSON
with the release evidence before its 90-day Actions artifact retention expires.
Review the full candidate and its
dependencies, not just newly introduced alerts. Do not publish
when any of the following remains unresolved:

- A finding meets a blocking threshold above, including an inherited finding.
- Required validation fails, security analysis is incomplete, or scan results
  do not cover the release commit and the dependencies/artifacts being shipped.
- A dependency's license permission or distribution obligations are unresolved.
- A dismissal lacks evidence, an exception has expired, or a VEX record does
  not apply to the exact artifact and configuration under review.
- There is evidence of compromised credentials, dependencies, build tooling,
  or release artifacts that has not been contained and investigated.

These are maintainer release requirements, not a claim of complete CI
enforcement. CodeQL result review and branch protection depend on repository
settings and service availability. The repository's CodeQL merge rule must block
high/critical security alerts and code-scanning errors, and its `CodeQL`,
`dependency review`, `workflow security`, and `SonarCloud Code Analysis` checks
must remain required, bound to their publishing GitHub Apps.
Dependency Review only checks public PR dependency changes. The release workflow
does not currently enforce a complete SAST/SCA, license, or VEX gate. Where a
hosted scan is unavailable or skipped,
retain equivalent analysis and maintainer review before publication; a skipped
job or successful upload is not a clean security assessment. Scorecard remains
advisory unless its underlying finding meets a blocking condition.

## Dismissals And Exceptions

Retain a reviewable record in the alert, PR, or private security advisory before
dismissing or suppressing a finding. The record must include:

- The alert/rule or CVE/GHSA identifier, tool/version and scan date, affected
  code or package versions, and release commit or image digest.
- The disposition and technical rationale, including reachable inputs, required
  privileges/configuration, and impact in supported deployments.
- Reproducible evidence: relevant source locations and call paths, build/package
  inventory, focused test or reproduction results, and upstream advisory links
  where applicable. A failed reproduction alone is insufficient.
- The approving maintainer, approval date, scope, and re-review date or trigger.
  Recheck whenever the relevant code, dependency, build, configuration, or
  advisory changes, and before carrying the decision into another release.

Distinguish false positives and proven non-applicability from accepted risk.
Accepted-risk exceptions are limited to non-blocking findings, require an owner,
compensating controls, a remediation issue, and an expiry within 90 days, and
must be reconsidered before renewal. They cannot waive a blocking condition.
Do not suppress a whole rule or package to silence one alert. Keep sensitive
evidence private and publish only a sanitized rationale when disclosure is safe.
The two MEDIUM optional TrueNAS build findings and their review deadline are
tracked in [SonarQube triage](docs/SONAR_TRIAGE.md#exception-limits). These are
scoped source-build decisions, not dependency CVE dismissals or VEX claims.

## VEX For Non-Exploitable Dependency Findings

A maintainer must approve a Vulnerability Exploitability eXchange (VEX) record
before treating a dependency alert as non-exploitable. Use machine-readable
[OpenVEX](https://github.com/openvex/spec/blob/main/OPENVEX-SPEC.md) JSON with its
required document identity, author, timestamp, and version metadata. Include the
CVE/GHSA, exact WUDup release and commit, affected component name/version and
package URL where available, and image digest/platform for each assessed image.
Assess default and Trivy variants separately; do not infer coverage across
platforms or mutable tags.

Set `not_affected` only with a supported justification such as
`component_not_present`, `vulnerable_code_not_present`, or
`vulnerable_code_not_in_execute_path`, plus an `impact_statement` explaining the
evidence and configuration scope. Attach or link the dismissal evidence above.
Do not assert non-exploitability solely because a feature is off by default or
an operator could apply a workaround. Uncertain cases remain
`under_investigation`; known exploitable cases are `affected`; releases
containing a verified remediation can be marked `fixed`. Neither investigation
nor accepted risk clears the release gate.

Retain the approved JSON and supporting review in the release PR or private
advisory. Publish sanitized records as `wudup-vX.Y.Z.openvex.json` assets on the
corresponding GitHub Release and link them from its notes so downstream users
can inspect them. Complete approval before release sign-off and publish the
records alongside the release. This is a manual process; no VEX generation or
scanner ingestion is currently implied.

Reassess before reuse in each release and whenever dependency versions, build
inputs, reachable code, supported configurations, or advisory details change.
When evidence changes, increment the document version, update its timestamp and
status, retain prior records, and link the superseding record. Reopen affected
alerts and notify users through an advisory if an earlier non-exploitability
claim is invalidated. Never apply a VEX dismissal beyond its assessed products.

## Disclosure And Fixes

This is a maintainer-run project without a staffed incident-response service.
Response targets are best effort, not a guaranteed SLA:

- Acknowledge a private report within 7 calendar days of receipt and provide an
  initial severity/impact assessment within 14 calendar days of receipt.
- After confirmation, target a fix or actionable mitigation within 7 calendar
  days for critical findings or active exploitation, 30 days for high severity,
  90 days for medium severity, and 180 days for low severity.
- Prioritize active exploitation as soon as it becomes known. While a report
  remains open, aim to update the reporter at least every 14 calendar days and
  whenever the severity, mitigation, or expected delivery date changes.

If a target cannot be met, explain the blocker, available mitigation, and revised
target privately. A response deadline or mitigation recommendation does not
waive release-blocking conditions. Reporters who have not received a response
after 7 days should follow up on the private advisory.

Fixes normally land on `main`, receive focused validation, and ship in the next
stable release; urgent fixes should use an expedited stable patch release.
Coordinate disclosure timing with the reporter and affected upstreams. Publish
affected versions, impact, fixed versions or mitigation, and upgrade guidance
when ready; if a fix is delayed or exploitation is occurring, an advisory with
mitigation may need to precede the fix. Credit reporters with their consent.
