import { describe, expect, it, vi } from "vitest";

import { fetchReleaseChangelog } from "../src/utils/releaseChangelog";

function textResponse(body: string, init: ResponseInit = {}): Response {
  return new Response(body, init);
}

function streamlessTextResponse(body: string, init: ResponseInit = {}): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(init.headers),
    body: null,
    text: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("release changelog fetching", () => {
  it.each([false, true])("preserves the fetch receiver (injected: %s)", async (injected) => {
    const responses = [
      textResponse(JSON.stringify({ body: "[changelog](CHANGELOG.md)" })),
      textResponse("## [v0.5.0]\n\n- Loaded notes"),
    ];
    const fetchMock = vi.fn(function (this: unknown) {
      // Web IDL accepts a standalone call, but rejects an options object as this.
      if (this !== undefined && this !== globalThis) {
        throw new TypeError("Illegal invocation");
      }
      return Promise.resolve(responses.shift()!);
    });
    if (!injected) {
      vi.stubGlobal("fetch", fetchMock);
    }

    await expect(fetchReleaseChangelog(
      "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
      "v0.5.0",
      injected ? { fetch: fetchMock } : {},
    )).resolves.toMatchObject({
      status: "ready",
      body: expect.stringContaining("Loaded notes"),
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts a stalled fetch at the timeout and clears the timer", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | undefined;
      const fetchMock = vi.fn((_input: string, init?: RequestInit) => {
        signal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(signal?.reason));
        });
      });
      const request = fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: fetchMock, timeoutMs: 50 },
      );
      const rejection = expect(request).rejects.toMatchObject({ name: "AbortError" });
      await vi.advanceTimersByTimeAsync(50);
      await rejection;
      expect(signal?.aborted).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("returns unavailable when the release body has no changelog link", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      textResponse(JSON.stringify({ body: "No changelog here." })),
    );

    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: fetchMock },
      ),
    ).resolves.toMatchObject({
      status: "unavailable",
      error: "This release does not link to a changelog. Open the GitHub release for notes.",
    });
  });

  it("returns unavailable when the changelog has no matching tag section", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        textResponse(
          JSON.stringify({
            body: "[changelog](https://github.com/t-mart/mousehole/blob/master/CHANGELOG.md)",
          }),
        ),
      )
      .mockResolvedValueOnce(textResponse("# Changelog\n\n## [v0.4.0]\n\n- Old"));

    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: fetchMock },
      ),
    ).resolves.toMatchObject({
      status: "unavailable",
      error: "No changelog notes found for v0.5.0. Open the GitHub release for details.",
    });
  });

  it("falls back to text responses when streams are unavailable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        streamlessTextResponse(
          JSON.stringify({
            body: "[changelog](https://github.com/t-mart/mousehole/blob/master/CHANGELOG.md)",
          }),
        ),
      )
      .mockResolvedValueOnce(
        streamlessTextResponse(
          [
            "# Changelog",
            "",
            "## [v0.5.0](https://github.com/t-mart/mousehole/releases/tag/v0.5.0)",
            "",
            "- Streamless response body",
            "",
            "## [v0.4.0]",
            "",
            "- Older release",
          ].join("\n"),
        ),
      );

    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: fetchMock },
      ),
    ).resolves.toMatchObject({
      status: "ready",
      body: expect.stringContaining("Streamless response body"),
    });
  });

  it("rejects oversized and failed fetches", async () => {
    const oversizedFetch = vi.fn().mockResolvedValueOnce(
      textResponse("{}", { headers: { "content-length": "20" } }),
    );
    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: oversizedFetch, maxBytes: 10 },
      ),
    ).rejects.toThrow("GitHub release response is too large.");

    const oversizedBodyFetch = vi.fn().mockResolvedValueOnce(textResponse("{}"));
    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: oversizedBodyFetch, maxBytes: 1 },
      ),
    ).rejects.toThrow("GitHub release response is too large.");

    const oversizedStreamlessFetch = vi
      .fn()
      .mockResolvedValueOnce(streamlessTextResponse("{}"));
    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: oversizedStreamlessFetch, maxBytes: 1 },
      ),
    ).rejects.toThrow("GitHub release response is too large.");

    const failedStatusFetch = vi
      .fn()
      .mockResolvedValueOnce(textResponse("Not found", { status: 404 }));
    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: failedStatusFetch },
      ),
    ).rejects.toThrow("Could not fetch GitHub release (404).");

    const failedFetch = vi.fn().mockRejectedValueOnce(new Error("network failed"));
    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: failedFetch },
      ),
    ).rejects.toThrow("network failed");
  });

  it("aborts streamed fetches when the cumulative body limit is exceeded", async () => {
    let signal: AbortSignal | undefined;
    const fetchMock = vi.fn(async (_input: string, init?: RequestInit) => {
      signal = init?.signal ?? undefined;
      const chunks = [Uint8Array.of(123), Uint8Array.of(125)];
      return new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            const chunk = chunks.shift();
            if (chunk) {
              controller.enqueue(chunk);
              return;
            }
            controller.close();
          },
        }),
      );
    });

    await expect(
      fetchReleaseChangelog(
        "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
        "v0.5.0",
        { fetch: fetchMock, maxBytes: 1 },
      ),
    ).rejects.toThrow("GitHub release response is too large.");

    expect(signal?.aborted).toBe(true);
  });
});
