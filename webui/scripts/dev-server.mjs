#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createDemoWudApiServer } from "./demo-wud-api.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webuiDir = resolve(scriptDir, "..");
const repoRoot = resolve(webuiDir, "..");
const localDevRoot = resolve(repoRoot, "local-dev");
const fakeDockerRoot = join(localDevRoot, "fake-docker");
const fakeDockerBin = join(repoRoot, "tests", "fakes");
const backendHost = "127.0.0.1";
const backendPort = process.env.WUD_WEB_DEV_BACKEND_PORT ?? "7417";
const demoWudPort = process.env.WUD_WEB_DEV_WUD_PORT ?? "7418";
const frontendHost = "127.0.0.1";
const frontendPort = process.env.WUD_WEB_DEV_FRONTEND_PORT ?? "5173";
const pythonBin = resolvePython();
const pathSeparator = process.platform === "win32" ? ";" : ":";
const pythonPath = process.env.PYTHONPATH
  ? `${join(repoRoot, "src")}${pathSeparator}${process.env.PYTHONPATH}`
  : join(repoRoot, "src");
let vite = null;
let demoWudServer = null;
let shuttingDown = false;

runSeeder();

if (!process.env.WUD_API_BASE_URL) {
  demoWudServer = createDemoWudApiServer(join(localDevRoot, "docker"));
  try {
    await new Promise((resolveListening, rejectListening) => {
      demoWudServer.once("error", rejectListening);
      demoWudServer.listen(Number(demoWudPort), backendHost, resolveListening);
    });
  } catch (error) {
    console.error(`Could not start the local demo WUD API on ${backendHost}:${demoWudPort}: ${error.message}. Choose a free WUD_WEB_DEV_WUD_PORT.`);
    process.exit(1);
  }
}

const backend = spawn(
  pythonBin,
  [
    "-m",
    "wudup.cli",
    "web",
    "--host",
    backendHost,
    "--port",
    backendPort,
    "--base",
    join(localDevRoot, "docker"),
    "--file",
    join(localDevRoot, "out", "images.todo"),
    "--log-dir",
    join(localDevRoot, "logs"),
    "--db-path",
    join(localDevRoot, "logs", "wudup.sqlite"),
  ],
  {
    cwd: repoRoot,
    stdio: "inherit",
    env: {
      ...process.env,
      PYTHONPATH: pythonPath,
      PATH: `${fakeDockerBin}${pathSeparator}${process.env.PATH ?? ""}`,
      FAKE_DOCKER_ROOT: fakeDockerRoot,
      WUD_PENDING_SOURCE: process.env.WUD_PENDING_SOURCE || "file",
      WUD_API_BASE_URL: process.env.WUD_API_BASE_URL || `http://${backendHost}:${demoWudPort}`,
      WUD_WEB_DEV_NO_AUTH: "true",
      WUD_WEB_MUTATIONS_ENABLED: "true",
      WUD_WEB_RESTART_CONTAINER: "demo-wudup",
      WUD_WEB_DEMO_SELF_UPDATE: "true",
      WUD_SECURITY_SCANNING_ENABLED: "true",
      WUD_WEB_ALLOWED_ORIGINS: `http://${frontendHost}:${frontendPort},http://localhost:${frontendPort}`,
    },
  },
);

if (!(await waitForBackend())) {
  process.exit(1);
}

const viteBin = join(
  webuiDir,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "vite.cmd" : "vite",
);
vite = spawn(viteBin, ["--host", frontendHost, "--port", frontendPort], {
  cwd: webuiDir,
  stdio: "inherit",
  env: {
    ...process.env,
    VITE_WUD_BACKEND_URL: `http://${backendHost}:${backendPort}`,
  },
});

backend.on("exit", (code, signal) => {
  if (!shuttingDown) {
    shutdown(code ?? signal ?? 1);
  }
});

vite.on("exit", (code, signal) => {
  shutdown(code ?? signal ?? 0);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => shutdown(signal));
}

function runSeeder() {
  const result = spawnSync(
    pythonBin,
    [join(scriptDir, "seed_demo_state.py"), "--root", localDevRoot],
    {
      cwd: repoRoot,
      stdio: "inherit",
      env: { ...process.env, PYTHONPATH: pythonPath },
    },
  );
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

async function waitForBackend() {
  const deadline = Date.now() + 10_000;
  const url = `http://${backendHost}:${backendPort}/api/v1/auth/session`;
  while (Date.now() < deadline) {
    if (backend.exitCode !== null) {
      process.exit(backend.exitCode ?? 1);
    }
    try {
      const response = await fetch(url);
      if (response.ok) {
        return true;
      }
    } catch {
      // Uvicorn is still starting.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 150));
  }
  console.error(`Timed out waiting for backend at ${url}`);
  shutdown(1);
  return false;
}

function resolvePython() {
  if (process.env.PYTHON_BIN) {
    return process.env.PYTHON_BIN;
  }
  const venvPython = join(repoRoot, ".venv", "bin", "python");
  if (existsSync(venvPython)) {
    return venvPython;
  }
  return "python3";
}

function shutdown(reason) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  if (backend.exitCode === null) {
    backend.kill("SIGTERM");
  }
  if (vite && vite.exitCode === null) {
    vite.kill("SIGTERM");
  }
  demoWudServer?.close();
  if (typeof reason === "number") {
    process.exitCode = reason;
  } else if (reason && reason !== "SIGINT" && reason !== "SIGTERM") {
    process.exitCode = 1;
  }
}
