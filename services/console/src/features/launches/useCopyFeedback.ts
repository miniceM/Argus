import { useState, useRef, useEffect, useCallback } from "react";

export type CopyFeedbackStatus = "idle" | "pending" | "success" | "error";

export interface UseCopyFeedbackResult {
  status: CopyFeedbackStatus;
  isSuccess: boolean;
  isError: boolean;
  isPending: boolean;
  copy: (textProducer: string | (() => string | Promise<string>)) => Promise<boolean>;
  reset: () => void;
}

/**
 * Clipboard feedback scoped to a report identity.
 *
 * `identity` is the (launch, snapshot) combination the copied content belongs to. When it
 * changes, any in-flight attempt and its feedback timer are invalidated, so a late
 * success/failure from the previous identity can never be displayed as the current one —
 * and content produced for a superseded identity is never written to the clipboard at all.
 */
export function useCopyFeedback(
  durationMs = 2000,
  identity?: string | null,
): UseCopyFeedbackResult {
  const [status, setStatus] = useState<CopyFeedbackStatus>("idle");
  const tokenRef = useRef(0);
  const identityRef = useRef<string | null>(identity ?? null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Identity change invalidates the previous attempt: newest token, no timer, back to idle.
  useEffect(() => {
    identityRef.current = identity ?? null;
    tokenRef.current += 1;
    clearTimer();
    setStatus("idle");
  }, [identity, clearTimer]);

  // Unmount only releases bookkeeping; it never touches state afterwards.
  useEffect(() => {
    return () => {
      tokenRef.current += 1;
      clearTimer();
    };
  }, [clearTimer]);

  const reset = useCallback(() => {
    tokenRef.current += 1;
    clearTimer();
    setStatus("idle");
  }, [clearTimer]);

  const copy = useCallback(
    async (textProducer: string | (() => string | Promise<string>)): Promise<boolean> => {
      clearTimer();
      const currentToken = ++tokenRef.current;
      setStatus("pending");

      try {
        if (!navigator?.clipboard?.writeText) {
          throw new Error("Clipboard API unavailable");
        }

        const text = typeof textProducer === "function" ? await textProducer() : textProducer;
        // Re-check ownership after a possibly async producer: the attempt may have been
        // superseded while producing, and writing that content would be wrong.
        if (tokenRef.current !== currentToken) {
          return false;
        }
        await navigator.clipboard.writeText(text);

        if (tokenRef.current === currentToken) {
          setStatus("success");
          timerRef.current = setTimeout(() => {
            if (tokenRef.current === currentToken) {
              setStatus("idle");
            }
          }, durationMs);
          return true;
        }
        return false;
      } catch {
        if (tokenRef.current === currentToken) {
          setStatus("error");
          timerRef.current = setTimeout(() => {
            if (tokenRef.current === currentToken) {
              setStatus("idle");
            }
          }, durationMs);
        }
        return false;
      }
    },
    [clearTimer, durationMs],
  );

  return {
    status,
    isSuccess: status === "success",
    isError: status === "error",
    isPending: status === "pending",
    copy,
    reset,
  };
}
