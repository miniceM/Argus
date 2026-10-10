import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useCopyFeedback } from "../useCopyFeedback";

function writeSpy(result: { current: { isSuccess: boolean } }) {
  return result.current.isSuccess;
}

describe("useCopyFeedback hook", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("transitions to success on resolution and resets after duration", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    const { result } = renderHook(() => useCopyFeedback(2000));
    expect(result.current.status).toBe("idle");

    let successPromise: Promise<boolean>;
    act(() => {
      successPromise = result.current.copy("test-content");
    });
    expect(result.current.status).toBe("pending");

    await act(async () => {
      await successPromise;
    });
    expect(result.current.status).toBe("success");
    expect(result.current.isSuccess).toBe(true);

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.status).toBe("idle");
  });

  it("R09/P08: a pending copy superseded by an identity switch never reports success", async () => {
    let finish: (() => void) | null = null;
    const writeText = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    const { result, rerender } = renderHook(
      ({ identity }: { identity: string }) => useCopyFeedback(2000, identity),
      { initialProps: { identity: "S1" } },
    );

    let firstCopy: Promise<boolean>;
    await act(async () => {
      firstCopy = result.current.copy("snapshot-S1-payload");
    });
    expect(result.current.status).toBe("pending");
    expect(writeText).toHaveBeenCalledWith("snapshot-S1-payload");

    // Identity switches while the clipboard write is still in flight.
    rerender({ identity: "S2" });
    expect(result.current.status).toBe("idle");

    await act(async () => {
      finish?.();
      await firstCopy;
    });
    expect(result.current.status).toBe("idle");
    expect(result.current.isSuccess).toBe(false);
  });

  it("R09: an async producer superseded mid-flight must not write its stale content", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    const { result, rerender } = renderHook(
      ({ identity }: { identity: string }) => useCopyFeedback(2000, identity),
      { initialProps: { identity: "S1" } },
    );

    let releaseProducer: (() => void) | null = null;
    const slowProducer = () => new Promise<string>((resolve) => {
      releaseProducer = () => resolve("snapshot-S1-payload");
    });

    let firstCopy: Promise<boolean>;
    await act(async () => {
      firstCopy = result.current.copy(slowProducer);
    });
    expect(result.current.status).toBe("pending");

    // Switch identity before the producer resolves.
    rerender({ identity: "S2" });
    await act(async () => {
      releaseProducer?.();
      await firstCopy;
    });
    expect(writeText).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });

  it("R09: an identity switch cancels the success timer of the previous identity", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    const { result, rerender } = renderHook(
      ({ identity }: { identity: string }) => useCopyFeedback(2000, identity),
      { initialProps: { identity: "S1" } },
    );

    await act(async () => {
      await result.current.copy("payload-S1");
    });
    expect(result.current.status).toBe("success");

    rerender({ identity: "S2" });
    expect(result.current.status).toBe("idle");

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current.status).toBe("idle");
  });

  it("R09: unmount during a pending copy performs no state work and swallows late results", async () => {
    let finish: (() => void) | null = null;
    const writeText = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    const { result, unmount } = renderHook(() => useCopyFeedback(2000, "S1"));
    let firstCopy: Promise<boolean>;
    await act(async () => {
      firstCopy = result.current.copy("payload-S1");
    });
    unmount();
    await act(async () => {
      finish?.();
      await firstCopy;
    });
    expect(writeText).toHaveBeenCalledTimes(1);
  });

  it("R09: serialize failure and unavailable clipboard stay visible and retryable", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn() } });
    const { result } = renderHook(() => useCopyFeedback(2000, "S1"));

    await act(async () => {
      await result.current.copy(() => {
        throw new Error("serialize failed");
      });
    });
    expect(result.current.status).toBe("error");
    expect(writeSpy(result)).toBe(false);
  });

  it("immediately clears previous success feedback when subsequent copy fails within timer window", async () => {
    const writeText = vi
      .fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("permission denied"));

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    const { result } = renderHook(() => useCopyFeedback(2000));

    // First copy: success
    await act(async () => {
      await result.current.copy("first");
    });
    expect(result.current.status).toBe("success");

    // Advance 500ms (still within 2000ms timer window)
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(result.current.status).toBe("success");

    // Second copy: fails
    await act(async () => {
      await result.current.copy("second");
    });
    expect(result.current.status).toBe("error");
    expect(result.current.isSuccess).toBe(false);
    expect(result.current.isError).toBe(true);
  });
});
