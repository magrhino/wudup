import assert from "node:assert/strict";
import { test } from "node:test";

import { createDemoWudApiServer, demoWudContainers } from "../scripts/demo-wud-api.mjs";

test("local demo WUD observations identify Compose services and candidate tags", () => {
  const containers = demoWudContainers("/demo/docker");
  assert.equal(containers.length, 4);
  assert.equal(containers.filter((item) => item.updateAvailable).length, 3);
  const postgres = containers.find((item) => item.name === "postgres");
  assert.equal(postgres?.image.tag.value, "16");
  assert.equal(postgres?.updateAvailable, false);
  assert.equal(
    containers.find((item) => item.name === "radarr")?.labels["com.docker.compose.project.config_files"],
    "/demo/docker/media/docker-compose.yml",
  );
  assert.equal(
    containers.find((item) => item.name === "task-runner")?.result.tag,
    "2.34.4-distroless",
  );
});

test("local demo WUD API serves only read-only health and container reads", async () => {
  const server = createDemoWudApiServer("/demo/docker");
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  try {
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    const base = `http://127.0.0.1:${address.port}`;
    const health = await fetch(`${base}/health`);
    assert.equal(health.status, 200);
    const containers = await fetch(`${base}/api/containers`);
    assert.equal(containers.status, 200);
    assert.equal((await containers.json()).length, 4);
    assert.equal((await fetch(`${base}/api/watch`, { method: "POST" })).status, 404);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
