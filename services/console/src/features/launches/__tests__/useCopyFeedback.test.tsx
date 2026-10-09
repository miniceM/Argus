import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useCopyFeedback } from "../useCopyFeedback";

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
