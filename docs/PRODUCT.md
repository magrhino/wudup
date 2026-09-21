# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

WUDup is used by self-hosted Docker Compose operators, especially homelab
administrators, who review update signals from What's Up Docker (WUD) and
decide when to snooze, preview, or apply container updates. They usually work
in an admin context where speed matters, but where a mistaken update can
affect running services.

## Product Purpose

WUDup helps operators see update status, understand risk, review affected
stacks and images, manage snoozes or exclusions, capture diagnostics, and
confirm readiness before applying updates. Success means moving from “what
needs attention” to a safe decision with minimal hunting, clear evidence, and
explicit confirmation before mutation.

## Positioning

WUDup turns WUD signals into a local, reviewable, plan-first Docker Compose
workflow. Unlike a notification-only or direct-update path, it combines
read-only defaults, a pending review queue, operational evidence and history,
and optional policy automation while keeping the operator in control.

## Operating Context

The recommended deployment is a long-running WebUI container used from a local
browser, private network, or operator-managed reverse proxy. WUD detects image
updates, WUDup queues them for review, and the operator either applies an
explicit plan or lets an eligible policy handle the update. Results remain
available through run history, audit records, logs, Doctor checks, and
diagnostics.

WUD's API is the primary pending-update source for new WebUI deployments. The
callback todo file remains a fallback/import source, and the retained host and
helper-container CLI workflows remain file-based. Deployments interact with a
Docker daemon and mounted Compose stack roots; optional integrations include
Discord release-note notifications, GitHub release metadata, and Trivy-backed
candidate security scans.

## Capabilities and Constraints

- The WebUI/API is the primary supported workflow. Host and helper-container
  CLI paths are legacy conveniences; API and CLI feature parity is not a goal.
- The WebUI starts read-only. Browser mutations must be enabled intentionally,
  and update operations require an explicit plan and confirmation. Dry-run
  paths must remain non-mutating.
- Operators can review pending updates and retags, configure service policies,
  snoozes, and tag exclusions, inspect release-note and security signals, and
  use history, logs, Doctor, and diagnostic support data.
- Automatic updates are optional and apply only to policy-eligible work on an
  operator-defined schedule.
- Candidate security scanning provides advisory evidence; it does not gate
  updates, snooze updates, or claim that an image is safe.
- The public demo is fixture-backed, browser-only, and non-mutating.
- Docker Compose is the maintainer-tested deployment environment. TrueNAS
  support is experimental.
- WUDup is a public beta. Secrets and deployment-specific values must remain in
  environment variables, secret files, or operator-managed configuration.

## Brand Commitments

The product name is WUDup. Its voice is calm, reliable, compact, quiet,
precise, and operationally trustworthy. Language should be direct and useful
to a self-hosted operator without requiring knowledge of repository internals.
Marketing-heavy or trend-driven presentation must not undermine the product's
role as a dependable admin appliance.

Established brand assets include `docs/assets/wudup-mark.png` and
`webui/src/assets/brand/wudup-mark.svg`.

## Evidence on Hand

- A public fixture-backed demo is available at
  `https://magrhino.github.io/wudup/`.
- Product imagery is available in `docs/assets/wudup-dashboard.jpg` and the
  established brand-mark files.
- The repository contains deployment, configuration, security, feature, and
  development documentation under `README.md`, `SECURITY.md`, and `docs/`.
- `CHANGELOG.md` records versioned product changes, and automated shell,
  Python, WebUI, browser, container, and workflow checks provide implementation
  evidence.
- No customer testimonials, customer logos, adoption counts, performance
  benchmarks, or certification claims are on hand. Future work must not
  fabricate them.

## Product Principles

- Lead with operational clarity: show current status, pending work, and
  blockers before secondary detail.
- Make mutation risk explicit: preview plans, label read-only state, and
  require confirmation before changing services.
- Prefer evidence over reassurance: expose relevant update, security, history,
  and diagnostic information without overstating certainty.
- Keep operator workflows compact and predictable so important comparisons and
  decisions require minimal hunting.
- Preserve operator confidence with consistent terminology, visible state,
  plain-language errors, and clear recovery actions.

## Accessibility & Inclusion

Target WCAG AA. Support reduced motion, keyboard navigation, visible focus
states, sufficient contrast, and color-blind-safe status cues. Status must
never rely on color alone; pair color with labels, icons, or text. Full WCAG
AAA is not required unless a specific component can reach it without
compromising the product workflow.

For recent additions and versioned changes, see
[`CHANGELOG.md`](../CHANGELOG.md).
