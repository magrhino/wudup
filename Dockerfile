FROM docker:29.9.0-cli@sha256:1a4c7cb63513f349bdad01fcc6e0f3f2f67d37b9da86f14dc0d4a0942eecda00 AS docker-cli

FROM aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa AS trivy

FROM node:26-bookworm-slim@sha256:79723b41edbedf595f62e943a9f8b0ba9af5b1e61045c5f8f59c2c02c1212a16 AS webui-build

WORKDIR /webui

COPY webui/package*.json /webui/
RUN npm ci --ignore-scripts

COPY webui/ /webui/
COPY src/wudup/discord_webhook_policy.json /src/wudup/discord_webhook_policy.json
RUN npm run build


FROM python:3.14.7-alpine3.24@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01 AS wudup-runtime

# Keep the release workflow's cache-busting build arg name for compatibility.
ARG APT_REFRESH="local"

ENV DOCKER_BASE=/host/docker \
    WUD_OUT_FILE=/out/images.todo \
    WUD_LOG_DIR=/logs \
    WUD_WEB_HOST=0.0.0.0 \
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
      util-linux-misc

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

COPY requirements.txt requirements-build.txt /app/
RUN python -m pip install --require-hashes --only-binary=:all: --no-cache-dir \
    -r requirements.txt -r requirements-build.txt

COPY pyproject.toml README.md /app/
COPY src/ /app/src/
COPY --from=webui-build /webui/dist/ /app/src/wudup/web_static/

# Build trusted source with the locked backend, install only that wheel, then
# remove build-only pip.
RUN python -m pip wheel --no-deps --no-build-isolation --no-cache-dir \
      --wheel-dir /tmp/wudup-dist . \
    && PIP_ONLY_BINARY=:all: python -m pip install --no-deps --no-cache-dir \
      /tmp/wudup-dist/*.whl \
    && rm -rf /tmp/wudup-dist \
    && python -m pip uninstall --yes pip

COPY bin/ /app/bin/
COPY wud/ /app/wud/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/bin/docker-update-from-wud \
    && mkdir -p /host/docker /out /logs

# Edge builds record their edge-<sha> tag so the WebUI can show the running
# commit; release and local builds leave it empty.
ARG WUDUP_BUILD_VERSION=""
ENV WUDUP_BUILD_VERSION=$WUDUP_BUILD_VERSION

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${WUD_WEB_PORT:-7417}/readyz" || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/app/entrypoint.sh"]
CMD ["web"]

FROM wudup-runtime AS wudup-trivy

COPY --from=trivy /usr/local/bin/trivy /usr/local/bin/trivy

FROM wudup-runtime AS wudup
