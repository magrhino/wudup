FROM docker:29.6.1-cli@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b AS docker-cli

FROM aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f AS trivy

FROM node:26-bookworm-slim@sha256:79723b41edbedf595f62e943a9f8b0ba9af5b1e61045c5f8f59c2c02c1212a16 AS webui-build

WORKDIR /webui

COPY webui/package*.json /webui/
RUN npm ci --ignore-scripts

COPY webui/ /webui/
COPY src/wudup/discord_webhook_policy.json /src/wudup/discord_webhook_policy.json
RUN npm run build


FROM python:3.14.6-slim-bookworm@sha256:4ff4b92a68355dbdb52584ab3391dff8d371a61d4e063468bfd0130e3189c6d9 AS wudup-runtime

# Optional version-specific source build; reviewed exceptions: docs/SONAR_TRIAGE.md.
ARG TRUENAS_API_CLIENT_REF=""
ARG APT_REFRESH="local"

ENV DEBIAN_FRONTEND=noninteractive \
    DOCKER_BASE=/host/docker \
    WUD_OUT_FILE=/out/images.todo \
    WUD_LOG_DIR=/logs \
    WUD_WEB_HOST=0.0.0.0 \
    WUDUP_UPDATER=/app/bin/docker-update-from-wud \
    PATH=/app/bin:$PATH

RUN set -eux; \
    printf 'APT refresh key: %s\n' "$APT_REFRESH"; \
    apt-get update; \
    apt-get upgrade -y; \
    apt-get install -y --no-install-recommends \
      bash \
      bsdextrautils \
      bsdutils \
      ca-certificates \
      coreutils \
      curl \
      findutils \
      gawk \
      grep \
      jq \
      perl \
      sed \
      sudo \
      tini \
      tzdata \
      util-linux; \
    if [ -n "$TRUENAS_API_CLIENT_REF" ]; then \
      apt-get install -y --no-install-recommends git; \
      python -m pip install --no-cache-dir "git+https://github.com/truenas/api_client.git@${TRUENAS_API_CLIENT_REF}"; \
      apt-get purge -y --auto-remove git; \
    fi; \
    rm -rf /var/lib/apt/lists/*

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

COPY bin/ /app/bin/
COPY wud/ /app/wud/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/bin/updates /app/bin/docker-update-from-wud /app/wud/*.sh \
    && mkdir -p /host/docker /out /logs

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${WUD_WEB_PORT:-7417}/readyz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["web"]

FROM wudup-runtime AS wudup-trivy

COPY --from=trivy /usr/local/bin/trivy /usr/local/bin/trivy

FROM wudup-runtime AS wudup
