# Installation and permission findings

Reviewed against `main` revision `56a0d858cc60ff5e3f644edb9e538502f7232efe`
and its SonarQube Cloud analysis of 2026-09-17. The eight open findings below
are security issues, not Security Hotspots. Existing reviewed HTTP/cookie
hotspots are outside this change.

## Decisions

| Sonar issue | Rule and location | Decision and evidence |
|---|---|---|
| `AZ-Ew1_j8PcgpjNh6X1E` | `githubactions:S6505`, setup-node-env | Fix: install the committed npm lock with `--ignore-scripts`. The same flag belongs in `tests/run-all.sh`, which independently installs dependencies in CI and release validation. |
| `AZ-Ew2B58PcgpjNh6X1G` | `docker:S6505`, Dockerfile frontend stage | Fix: use `npm ci --ignore-scripts`. The lock marks only optional, Darwin-only `fsevents` as having an install script. Explicit repository build/test commands and Playwright browser installation remain enabled. |
| `AaCqshWCCYJ15EFuBTqi` | `docker:S8541`, runtime requirements install | Fix: require wheels as well as hashes. Hashing a source archive does not prevent its build hooks from executing. |
| `AZ-Ew1_08PcgpjNh6X1F` | `githubactions:S8544`, setup-python-env | Fix: install generated, hashed `requirements-dev.txt` and `requirements-build.txt` before the editable project. Dev resolution is constrained by the runtime and build locks. Cache keys include the locks. |
| `AaCqshP5CYJ15EFuBTqg` | `githubactions:S8541`, setup-python-env | Fix third-party installs with `--only-binary=:all:`. Building the checked-out project is intentional; its separate install uses `--no-deps --no-build-isolation`, so it cannot fetch additional dependencies. The Docker source install uses the same boundary. |
| `AaB8AqP4OrpnBdyVTZ7r` | `githubactions:S8264`, CI permissions | Fix: move `pull-requests: read` to `changes`, where paths-filter needs it. Other CI jobs retain only the workflow's `contents: read` default. |
| `AZ-Ew2B58PcgpjNh6X1H` | `docker:S8544`, optional TrueNAS client | Accept the residual dependency-locking risk for this opt-in compatibility build. Operators select a client release compatible with their appliance; there is no single dependency lock for every supported ref. The default published image leaves the ref empty. See the limits below. |
| `AaCqshWCCYJ15EFuBTqh` | `docker:S8541`, optional TrueNAS client | Accept required source execution. This explicitly installs the official `truenas/api_client` Git source and invokes its setuptools backend. Adding a binary-only flag would neither authenticate that source nor remove the VCS build. |

The previously resolved `text:S8565` finding on `pyproject.toml`
(`AZ6uKgdNlZga0k7Gje__`) remains a justified scanner-format exception:
pip-tools' hashed requirements files are the project's locks. Introducing a
second package manager solely to satisfy a filename heuristic is unnecessary.
The closed runtime `docker:S8544` finding (`AZ-Ew2B58PcgpjNh6X1I`) remains
covered by the hashed runtime lock. No source-wide rule exclusions or `NOSONAR`
comments are used.

## Exception limits

Both open exception records above have Sonar security impact `MEDIUM`, not
`HIGH` or `CRITICAL`. Owner: repository maintainer `@magrhino`. The existing
Sonar issue IDs track the residual risk and any follow-up remediation; maintainer
review of the consolidated PR is the approval record. Assessed on 2026-09-17;
re-review or expire by 2026-12-16 (90 days), or earlier on the changes below.
They cannot waive any higher-severity release blocker in
[`SECURITY.md`](../SECURITY.md#dismissals-and-exceptions).

The TrueNAS ref is trusted build input, not an API parameter. Preserve compatible
tags, but prefer a reviewed full commit SHA when preparing a deployment.
Tags can move; a SHA pins the client source but does **not** lock its dependencies.
The example `TS-26.0.0-BETA.1` metadata requests `websocket-client` plus
setuptools/wheel build tooling without exact versions. These downloads and
source execution are a conscious residual risk, not a reproducible or
script-free installation. Build this optional variant without credentials and
review the upstream ref before using it. Revisit both exceptions if the client
enters the default image, the upstream repository changes, or the project
chooses one supported client version that can have its own complete lock.

Local WUDup source builds likewise execute reviewed repository code. Locking
the backend and disabling build isolation/dependency resolution prevents those
builds from silently fetching new tools; it does not sandbox project code.
The host installer and ad hoc developer installs are outside the CI/image
locking guarantee.

## License evidence

The first consolidated PR analysis found two license decisions in the newly
recorded development lock. Owner: `@magrhino`; maintainer acceptance of
[PR #688](https://github.com/magrhino/wudup/pull/688) records approval of these
specific uses. Neither decision waives vulnerability scanning or adds a license
to the routine allowlist. The workflow limits them to the exact package versions,
`requirements-dev.txt`, development scope, and the recorded license metadata.
Changed versions, distribution scope, or metadata require renewed review.

- `pkg:pypi/certifi@2026.7.22`: the installed wheel and
  [upstream license](https://github.com/certifi/python-certifi/blob/2026.07.22/LICENSE)
  identify MPL-2.0. This is the unchanged CA bundle used by HTTPX in development
  and tests, absent from the runtime/build locks and default image. Preserve the
  upstream license and source reference; do not modify or redistribute the bundle
  under this development-only decision. Mozilla's
  [MPL guidance](https://www.mozilla.org/en-US/MPL/2.0/FAQ/#q5-i-want-to-use-software-which-is-available-under-the-mpl-what-do-i-have-to-do)
  distinguishes private use from distribution obligations.
- `pkg:pypi/typing-extensions@4.16.0`: the installed wheel's
  `License-Expression` and
  [versioned project metadata](https://github.com/python/typing_extensions/blob/4.16.0/pyproject.toml)
  identify PSF-2.0. The
  [license text](https://github.com/python/typing_extensions/blob/4.16.0/LICENSE)
  distinguishes GPL compatibility from GPL licensing; GitHub's compound
  expression containing `GPL-1.0-or-later` is a metadata misclassification.
  Preserve the shipped license/notices. This version already exists in the
  runtime lock; this decision covers its additional development-lock entry.

## Gate and enforcement

Keep the project's current new-code gate: security, reliability, and
maintainability ratings A; duplication at most 3%; reviewed Security Hotspots
100%. At review time security was C and the gate failed. Do not lower these
thresholds to make the installation findings disappear. Coverage remains under
the existing reporting policy; this change does not add a coverage exclusion.

The existing **SonarCloud Code Analysis** check from the SonarQube Cloud
GitHub App (integration ID `12526`) was added to the active `protect main` ruleset
on 2026-09-17 and verified through the ruleset API and `gh pr checks --required`.
Binding the required check to its app prevents another integration from
satisfying it with the same name. Preserve existing checks, strict up-to-date
policy, and administrator bypass policy. This also makes the dependency
auto-merge workflow's existing `gh pr checks --required` wait for Sonar.
Although administrator rights can bypass repository rules, dependency auto-merge
also requires successful complete CI/security/CodeQL runs and a fresh clean merge
state. It does not use administrator bypass to override a red or pending gate.
See [the common auto-merge policy](../SECURITY.md#routine-dependency-auto-merge).

Keep genuine-fix issues open until the changed code is analyzed remotely.
Only the two TrueNAS exceptions should be accepted during this triage; a green
local build is not evidence that the remote analysis has seen a patch.
The next PR analysis must confirm both the issue decisions and the required
check. Repository settings are remote state and cannot be enforced by
`sonar-project.properties` alone.

The first consolidated PR analysis also flagged the intentional editable source
install (`AaCxGW2ed_v-xkWRipEA`). Its command now specifies binary-only index
candidates as well as no dependency resolution/build isolation. Pip still builds
the explicitly named local project; this flag does not claim to sandbox source
or eliminate the reviewed local build.

## Validation and maintenance

`make lock` regenerates all three pip-tools locks; review and commit them
together. CI and lock generation use Python 3.14; older interpreters can require
additional marker-dependent packages and must not consume this CI lock. The
development lock includes the lock-generation tools themselves.
All three locks remain at the root under the existing Dependabot pip entry.
Dependabot discovers these requirements files and can update matching pins and
hashes together. Do not split them into independent update directories or
append a CI-authored lock-refresh commit to a verified bot PR. An incomplete
update with conflicting runtime/development pins must stay red; regenerate the
locks together when repairing it. Green minor/patch PRs retain the same automatic
merge path, including the required Sonar check.
Use the locked toolchain when regenerating: older pip-tools 7.5.3 fails with
pip 26.2.1 (`stdlib_pkgs` import); the recorded pip 25.3 / pip-tools 7.6.1 pair
is tested. Lock generation is an intentional maintainer operation that can run
build metadata hooks; normal CI installs consume the locks.

Validate with the exact setup-python-env install commands, `python -m pip check`,
`tests/run-all.sh`, `tests/container-build.sh`, `actionlint`, ShellCheck on
`scripts/lock.sh` and `tests/run-all.sh`, and `git diff --check`. Container checks
exercise Linux wheel availability, the local source build, WebUI startup, and
the optional Trivy target. Compose validation of the TrueNAS example is not
proof that every operator-selected upstream ref builds.

Validation recorded on 2026-09-17:

- `make lock`: passed with the locked toolchain.
- The documented hashed install, editable install, and `python -m pip check`:
  passed in a clean Python 3.14 venv. The dependency lock also installs in a
  fresh Linux amd64 venv. A conflicting Pydantic constraint was rejected with
  `ResolutionImpossible` using `pip install --dry-run --no-index`.
- `tests/run-all.sh`: the consolidated change passed all sections, including
  1,677 Python tests (one skipped), 458 frontend tests, shell checks, and the
  dependency auto-merge regression tests. Listener tests require socket access.
- `actionlint`, `sh -n scripts/lock.sh`, `bash -n tests/run-all.sh`,
  `shellcheck scripts/lock.sh tests/run-all.sh`, `git diff --check`, and
  `zizmor --config .github/zizmor.yml --min-severity high --min-confidence medium .github/workflows`:
  passed (zizmor used its default offline mode).
- During the original triage, Linux amd64 Docker builds passed for the default image, `--target wudup-trivy`,
  and `--build-arg TRUENAS_API_CLIENT_REF=TS-26.0.0-BETA.1`. Trivy version,
  `midclt --help`, WebUI health on Linux-native storage, and updater dry-run/help
  checks passed.
- `tests/container-build.sh`: passed in a container with a disposable Linux
  volume for temporary directories. The initial macOS bind-mount run built the
  image but failed database-owner verification; the unmodified harness passed
  with Linux-native storage, including permission repair and script syncing.
- The consolidated change also passed `tests/container-build.sh` on Linux arm64
  with disposable Linux-native storage, including the default and Trivy images.
- SonarQube MCP `analyze_file_list` failed to request analysis; automatic
  analysis was restored. The six source fixes still require remote PR analysis.

References: [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/),
[npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/), and
[TrueNAS client build metadata](https://github.com/truenas/api_client/blob/TS-26.0.0-BETA.1/pyproject.toml).
