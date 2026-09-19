import { shallowRef, ref } from "vue";

export type PolledJobOptions = {
  intervalMs?: number;
};

export function usePolledJob<TJob>(
  start: (isCurrent: () => boolean) => Promise<TJob | null>,
  poll: (job: TJob) => Promise<TJob>,
  isTerminal: (job: TJob) => boolean,
  options: PolledJobOptions = {},
) {
  const job = shallowRef<TJob | null>(null);
  const polling = ref(false);
  const error = ref("");
  const intervalMs = options.intervalMs ?? 500;
  let runId = 0;

  // A reset or newer run cancels this caller as well as its visible state.
  async function run(): Promise<TJob | null> {
    const activeRunId = ++runId;
    polling.value = true;
    error.value = "";
    try {
      let current = await start(() => activeRunId === runId);
      if (activeRunId !== runId || current === null) {
        return null;
      }
      job.value = current;
      while (!isTerminal(current)) {
        await delay(intervalMs);
        if (activeRunId !== runId) {
          return null;
        }
        current = await poll(current);
        if (activeRunId !== runId) {
          return null;
        }
        job.value = current;
      }
      return current;
    } catch (caughtError) {
      if (activeRunId !== runId) {
        return null;
      }
      error.value =
        caughtError instanceof Error ? caughtError.message : String(caughtError);
      throw caughtError;
    } finally {
      if (activeRunId === runId) {
        polling.value = false;
      }
    }
  }

  function reset(): void {
    runId += 1;
    polling.value = false;
    error.value = "";
    job.value = null;
  }

  return {
    job,
    polling,
    error,
    run,
    reset,
  };
}

function delay(ms: number): Promise<void> {
  if (ms <= 0) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}
