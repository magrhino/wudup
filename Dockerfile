FROM docker:29.8.1-cli@sha256:9f36dfce2d1fd053d700a4eca00c358df79bf7d8cb69d4a9e8d9981af18834ea AS docker-cli

FROM aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969 AS trivy

FROM --platform=$BUILDPLATFORM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS trivy-patched

ARG TARGETOS
ARG TARGETARCH
RUN apk add --no-cache git \
    && git clone --depth 1 --branch v0.74.0 https://github.com/aquasecurity/trivy.git /trivy \
    && test "$(git -C /trivy rev-parse HEAD)" = e1fd17a0ea4a8cf24bc4b4dd7e2cfbf4bb31b994
WORKDIR /trivy
# Remove this rebuild when an upstream Trivy release includes fixed gRPC.
# Trivy v0.74.0 bundles gRPC v1.82.1; v1.83.2 fixes its release-blocking CVEs.
RUN go get google.golang.org/grpc@v1.83.2 \
    && mkdir -p /out \
    && CGO_ENABLED=0 GOEXPERIMENT=jsonv2 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath \
      -ldflags '-s -w -X github.com/aquasecurity/trivy/pkg/version/app.ver=0.74.0' \
      -o /out/trivy ./cmd/trivy

FROM node:26-bookworm-slim@sha256:79723b41edbedf595f62e943a9f8b0ba9af5b1e61045c5f8f59c2c02c1212a16 AS webui-build

WORKDIR /webui

COPY webui/package*.json /webui/
RUN npm ci --ignore-scripts

COPY webui/ /webui/
COPY src/wudup/discord_webhook_policy.json /src/wudup/discord_webhook_policy.json
RUN npm run build


FROM python:3.14.7-alpine3.24@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01 AS wudup-runtime

# Optional version-specific source build; reviewed exceptions: docs/SONAR_TRIAGE.md.
ARG TRUENAS_API_CLIENT_REF=""
# Keep the release workflow's cache-busting build arg name for compatibility.
ARG APT_REFRESH="local"

ENV DOCKER_BASE=/host/docker \
    WUD_OUT_FILE=/out/images.todo \
    WUD_LOG_DIR=/logs \
    WUD_WEB_HOST=0.0.0.0 \
    WUDUP_UPDATER=/app/bin/docker-update-from-wud \
    PATH=/app/bin:$PATH

RUN set -eux; \
    printf 'Package refresh key: %s\n' "$APT_REFRESH"; \
    apk upgrade --no-cache; \
    apk add --no-cache \
      bash \
      ca-certificates \
      coreutils \
      curl \
      findutils \
      gawk \
      grep \
      jq \
      sed \
      sudo \
      tini \
      tzdata \
      util-linux-misc; \
    if [ -n "$TRUENAS_API_CLIENT_REF" ]; then \
      apk add --no-cache --virtual .wudup-build-deps git; \
      python -m pip install --no-cache-dir "git+https://github.com/truenas/api_client.git@${TRUENAS_API_CLIENT_REF}"; \
      apk del .wudup-build-deps; \
    fi

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

COPY requirements.txt requirements-build.txt /app/
RUN python -m pip install --require-hashes --only-binary=:all: --no-cache-dir \
    -r requirements.txt -r requirements-build.txt

COPY pyproject.toml README.md /app/
COPY src/ /app/src/
COPY --from=webui-build /webui/dist/ /app/src/wudup/web_static/

# Build trusted repository source with the locked backend, without fetching dependencies.
RUN python -m pip install --no-deps --no-build-isolation --no-cache-dir .

# pip is only needed while building; its bundled libraries need not ship in the runtime.
RUN python -m pip uninstall --yes pip

COPY bin/ /app/bin/
COPY wud/ /app/wud/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/bin/updates /app/bin/docker-update-from-wud /app/wud/*.sh \
    && mkdir -p /host/docker /out /logs

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${WUD_WEB_PORT:-7417}/readyz" || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/app/entrypoint.sh"]
CMD ["web"]

FROM wudup-runtime AS wudup-trivy

COPY --from=trivy-patched /out/trivy /usr/local/bin/trivy

FROM wudup-runtime AS wudup
