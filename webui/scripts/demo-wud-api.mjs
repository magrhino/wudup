import { createServer } from "node:http";
import { join } from "node:path";

// Keep these four running Compose services aligned with seed_demo_state.
const sampleServices = [
  { stack: "data", service: "postgres", image: "postgres", tag: "16" },
  { stack: "home", service: "home-assistant", image: "ghcr.io/home-assistant/home-assistant", tag: "2026.5.1", candidate: "2026.5.3" },
  { stack: "jarvis", service: "task-runner", image: "n8nio/runners", tag: "2.33.5-distroless", candidate: "2.34.4-distroless" },
  { stack: "media", service: "radarr", image: "lscr.io/linuxserver/radarr", tag: "5.21.1", candidate: "5.22.4" },
];

export function demoWudContainers(dockerBase) {
  return sampleServices.map(({ stack, service, image, tag, candidate }) => {
    const directory = join(dockerBase, stack);
    return {
      id: `demo.${stack}.${service}`,
      name: service,
      displayName: service,
      status: "running",
      watcher: "local",
      image: { name: image, tag: { value: tag } },
      result: { tag: candidate ?? tag },
      updateAvailable: Boolean(candidate),
      updateKind: candidate ? { kind: "tag", semverDiff: "minor" } : {},
      labels: {
        "com.docker.compose.project": stack,
        "com.docker.compose.service": service,
        "com.docker.compose.project.working_dir": directory,
        "com.docker.compose.project.config_files": join(directory, "docker-compose.yml"),
      },
    };
  });
}

export function createDemoWudApiServer(dockerBase) {
  return createServer((request, response) => {
    const path = request.url?.split("?", 1)[0];
    const payload = request.method === "GET" && path === "/health"
      ? { status: "ready" }
      : request.method === "GET" && path === "/api/containers"
        ? demoWudContainers(dockerBase)
        : null;
    response.writeHead(payload === null ? 404 : 200, { "content-type": "application/json" });
    response.end(JSON.stringify(payload === null ? { error: "not found" } : payload));
  });
}
