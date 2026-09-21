# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

WUDup is for self-hosted Docker Compose operators, especially homelab administrators who use What's Up Docker (WUD). They need to understand available image updates, decide when those updates are safe to apply, and diagnose the result without surrendering control of their services.

## Product Purpose

WUDup turns WUD image-update notices into a reviewable Docker Compose workflow. It helps operators inspect pending updates, check readiness, preview changes, apply them manually or through explicit policies, and review run history, logs, and diagnostics afterward. Success means an operator can keep services current while understanding what will change and retaining control over when mutations occur.

## Positioning

WUDup is the plan-first control layer between WUD notifications and Docker Compose changes. Its defining mechanism combines a pending-update review queue with read-only defaults, explicit mutation controls, operational history, and diagnostics instead of treating update detection as permission to change running services.

## Operating Context

- WUD detects image updates and supplies current update metadata.
- Operators use the local browser WebUI to review the queue, inspect readiness, apply eligible updates, configure update policies, and investigate outcomes.
- Docker Compose stacks and the Docker daemon remain the systems WUDup observes and, when intentionally enabled, changes.
- The recommended deployment is a long-running self-hosted WebUI container. A fixture-backed public demo lets prospective users inspect the interface without a mutation backend.
- Host and helper-container CLI paths remain available for operators who prefer file-based or command-runner workflows.

## Capabilities and Constraints

- The WebUI and API are the primary supported workflow. CLI and file-mode paths are supported legacy conveniences; feature parity with the WebUI is not a product goal.
- New deployments start in read-only mode. Browser-initiated Docker mutations and scheduled automatic updates require explicit enablement.
- Mutating update flows remain plan-first and require explicit confirmation or an intentionally configured policy.
- WUDup is self-hosted and focused on Docker Compose environments.
- The product is in public beta and is maintainer-tested with Docker Compose.
- The WebUI includes pending-update review, readiness and Doctor checks, update policies, snoozes, tag exclusions, run history, logs, diagnostics, and controlled self-update preparation.
- Secrets and host-specific configuration remain environment- or deployment-driven and must not be exposed in the interface, logs, demo fixtures, or documentation.

## Brand Commitments

- Preserve the WUDup name and existing mark.
- Use plain, operator-focused language that explains what happened, why when known, and what the operator can do next.
- Keep safety state visible: operators should be able to distinguish read-only observation, planning, and mutation-enabled actions.

## Evidence on Hand

- Product overview, deployment positioning, and public-beta status: `README.md`.
- Existing brand assets: `docs/assets/wudup-mark.png` and `webui/src/assets/brand/wudup-mark.svg`.
- Existing product screenshot: `docs/assets/wudup-dashboard.jpg`.
- Fixture-backed public demo and representative operational data: `webui/src/api/demo/`.
- Deployment and safety details: `docs/DEPLOYMENT.md` and `docs/wiki/webui-container.md`.
- There are no confirmed customer testimonials, adoption metrics, benchmark claims, or third-party endorsements to use as product proof.

## Product Principles

1. Make the pending change understandable before asking for action.
2. Default to observation and require deliberate authorization for mutation.
3. Preserve operator control while supporting intentional automation.
4. Explain failures in operational language and provide a useful next step.
5. Keep the WebUI coherent around the complete review-to-verification workflow.
