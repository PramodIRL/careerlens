import { afterEach, describe, expect, it, vi } from "vitest";

import { refresh } from "@/lib/api-client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("refresh", () => {
  it("deduplicates concurrent calls into a single request", async () => {
    let resolveFetch: (value: Response) => void = () => {};
    const fetchMock = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    // Two callers racing (e.g. React Strict Mode's double effect-invoke)
    // before the first request has resolved.
    const first = refresh();
    const second = refresh();

    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveFetch(
      new Response(
        JSON.stringify({
          access_token: "tok",
          token_type: "bearer",
          expires_in: 900,
        }),
        {
          status: 200,
        },
      ),
    );

    const [firstResult, secondResult] = await Promise.all([first, second]);
    expect(firstResult).toEqual(secondResult);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("allows a fresh request once the previous one has settled", async () => {
    const okResponse = () =>
      new Response(
        JSON.stringify({
          access_token: "tok",
          token_type: "bearer",
          expires_in: 900,
        }),
        {
          status: 200,
        },
      );
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(okResponse()));
    vi.stubGlobal("fetch", fetchMock);

    await refresh();
    await refresh();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
