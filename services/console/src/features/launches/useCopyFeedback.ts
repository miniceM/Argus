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

export function useCopyFeedback(durationMs = 2000): UseCopyFeedbackResult {
  const [status, setStatus] = useState<CopyFeedbackStatus>("idle");
  const tokenRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    tokenRef.current += 1;
    clearTimer();
    setStatus("idle");
  }, [clearTimer]);

  useEffect(() => {
    return () => {
      clearTimer();
    };
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
