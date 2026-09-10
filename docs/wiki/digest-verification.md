# Digest Verification

WUD can report digest-pinned updates in the shared todo file:

```text
repo/app:latest@sha256:abc123
repo/app@sha256:abc123
```

When the Python updater applies a digest-pinned line, it pulls the matched
Compose image first and then checks whether the local image can be tied back to
the digest WUD requested. Successful verification lets the updater recreate the
matched service or stack and remove the todo entry. A proven mismatch leaves the
entry pending and skips the recreate.

## Verification Sources

The fastest check uses Docker's local image metadata. If any local
`RepoDigests` value ends with the requested digest, the updater treats the image
as verified.

If `RepoDigests` does not contain the requested digest, the updater resolves the
current registry manifest for the image tag. It supports GHCR, Docker Hub
references such as `alpine:3.20`, and explicit registries such as
`quay.io/org/image:tag`.

For multi-platform images, Docker often records only the tag or index digest in
`RepoDigests`, not the platform-child manifest digest. In that case, the updater
fetches the child manifest from the registry and compares its config digest to
the local Docker image ID. If those match, the requested platform digest is
verified.

## Registry Authentication

The direct registry client requires TLS 1.2 or newer with certificate verification. Token
challenges may use the registry's own origin, or Docker Hub's `auth.docker.io`
service. For other separate token services, explicitly authorize the HTTPS
origin for that registry in the WUDup process environment (example only):

```bash
export WUD_REGISTRY_AUTH_ORIGINS='{"https://registry.example:5000":["https://auth.example:8443"]}'
```

Each origin includes its port when non-default. This setting trusts that token
server for the named registry only. Same-origin token requests reuse the
registry connection's resolved address. Explicit registries other than Docker Hub
and configured token services support RFC1918, loopback, and IPv6 ULA addresses.
Link-local, multicast, and unspecified destinations are refused. Docker Hub's
manifest endpoints and default token service must resolve to public addresses.

The client does not follow redirects or use environment HTTP proxies. Configure
a direct endpoint; Docker manifest fallback remains available for registries
that require other authentication or network arrangements. Each HTTPS request
has a total deadline of five seconds (20 seconds in the diagnostic probe),
including DNS and response reads. Token responses are limited to 64 KiB and
manifest responses to 4 MiB. A short-lived Python subprocess enforces the deadline.

## Failure Policy

GHCR digest verification is treated as trusted and fail-closed. If the updater
can prove the requested GHCR digest is stale, unavailable, or points to a
different platform image, the update fails and the todo entry remains pending.

Non-GHCR digest verification is also fail-closed when the registry proves a
stale or mismatched digest. If a non-GHCR registry lookup cannot be completed
after `RepoDigests` does not match, the result is logged as untrusted and the
update continues. This avoids blocking Docker Hub, Quay, or private-registry
updates solely because the registry API or credentials were unavailable to the
helper.

## Logs

Hard failures are logged as `ERROR` entries with reason
`expected-digest-not-reached`. The log includes the local image ID, any seen
`RepoDigests`, the current tag digest when known, the matched child digest when
known, and registry or Docker manifest errors.

Untrusted non-GHCR results are logged as `WARN` entries with the same diagnostic
fields. They do not mark the run failed by themselves.

## Digest-Pin Tag Updates

Set `WUD_DIGEST_PIN_UPDATES=true` to make approved tag updates write Compose
images as `repo/app@sha256:<tag-or-index-digest>` instead of `repo/app:<tag>`.
This mode is opt-in, still requires tag update approval, and only supports WUD
lines with a safe resolved tag from WUD's callback `tag=<new-tag>` token or a
manual tag override.

During planning, the updater resolves the remote tag/index digest for the
resolved tag and includes it in the plan ID. During apply, it temporarily writes
the resolved tag, pulls with Compose, re-resolves the tag digest, verifies the
pulled local image against the planned digest, and only then writes the final
digest-pinned Compose reference.

The final Compose edit also writes `# wudup.resolved-tag=<tag>`
immediately above `image:` and sets `wud.tag.include` to an exact regex for that
tag so WUD keeps watching the resolved tag. If the tag digest moves, cannot be
resolved, cannot be verified locally, or the Compose metadata cannot be written
safely, the update fails closed and the pending line is restored.

## WebUI Retag Digest Pins

WebUI retags write the tag approved in the preview as `repo/app:<tag>` by
default. To retain digest-pinned retags, enable **Retag digest pins** under
Settings → Preferences. The preview then identifies the change as a digest pin
and the apply writes `repo/app@sha256:<digest>` with the resolved-tag marker.

This preference only controls the Retags workflow. It is separate from
`WUD_DIGEST_PIN_UPDATES`, which controls approved tag updates in standard update
plans.

## Live Probe

The repository includes a Docker-gated live probe for checking registry behavior
against small public images:

```bash
tests/live-digest-verification.py alpine:3.20 quay.io/prometheus/busybox:latest
```

The probe pulls each image, reads Docker's local image ID and `RepoDigests`,
fetches registry manifests, and reports whether the tag digest and platform
child digest can be verified.
