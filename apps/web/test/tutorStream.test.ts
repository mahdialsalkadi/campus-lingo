/**
 * Unit tests for `streamTutorHint` (lib/api.ts).
 *
 * Two behaviours are covered because both caused the "جارٍ التفكير..." hang:
 *   1. A non-SSE deployment/proxy that collapses the response into a single
 *      JSON body must still surface the hint immediately.
 *   2. The SSE reader must survive CRLF separators, frames split across chunk
 *      boundaries, and a final frame that arrives without its closing blank
 *      line.
 *
 * `fetch` is stubbed per test with real `Response` + `ReadableStream` objects,
 * so the production reader path is exercised end to end.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, streamTutorHint, type TutorHintEvent } from "@/lib/api";

/** A streaming response built from raw string chunks (partial frames welcome). */
function streamResponse(chunks: string[], contentType = "text/event-stream"): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": contentType } });
}

function stubFetch(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Run the stream and collect every emitted event, in order. */
async function collect(): Promise<TutorHintEvent[]> {
  const events: TutorHintEvent[] = [];
  await streamTutorHint({ question: "What is a TLB?" }, (event) => events.push(event));
  return events;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamTutorHint", () => {
  it("parses a single JSON `{ hint }` body when the response is not SSE", async () => {
    stubFetch(
      new Response(JSON.stringify({ hint: "ركّز على الكلمة المفتاحية في رأس السؤال" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(collect()).resolves.toEqual([
      { type: "delta", text: "ركّز على الكلمة المفتاحية في رأس السؤال" },
      { type: "done" },
    ]);
  });

  it("accepts `text` / `message` as the JSON hint field too", async () => {
    stubFetch(
      new Response(JSON.stringify({ message: "treat the cache like a phonebook" }), {
        status: 200,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      }),
    );

    await expect(collect()).resolves.toEqual([
      { type: "delta", text: "treat the cache like a phonebook" },
      { type: "done" },
    ]);
  });

  it("reads CRLF-separated SSE frames split across chunk boundaries", async () => {
    stubFetch(
      streamResponse([
        'data: {"type":"delta","text":"A"}\r\n\r\ndata: {"type":"delta","text":"B"}',
        "\r",
        '\n\r\ndata: [DONE]\r\n\r\n',
      ]),
    );

    // The `[DONE]` sentinel is swallowed; only the two deltas surface.
    await expect(collect()).resolves.toEqual([
      { type: "delta", text: "A" },
      { type: "delta", text: "B" },
    ]);
  });

  it("flushes a trailing frame that arrives without a closing blank line", async () => {
    stubFetch(
      streamResponse([
        'data: {"type":"delta","text":"first"}\n\ndata: {"type":"delta","text":"last"}',
      ]),
    );

    await expect(collect()).resolves.toEqual([
      { type: "delta", text: "first" },
      { type: "delta", text: "last" },
    ]);
  });

  it("ignores malformed frames instead of aborting the whole stream", async () => {
    stubFetch(
      streamResponse([
        "data: {not valid json}\n\n",
        'data: {"type":"delta","text":"still here"}\n\n',
      ]),
    );

    await expect(collect()).resolves.toEqual([{ type: "delta", text: "still here" }]);
  });

  it("throws ApiError when the endpoint answers with a non-OK status", async () => {
    stubFetch(new Response("insufficient gems", { status: 402 }));

    const error = await collect().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(402);
  });
});
