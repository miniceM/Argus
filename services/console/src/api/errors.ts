export function formatApiError(error: unknown): string {
  if (!error) return "未知错误";
  if (typeof error === "string") return error;

  if (typeof error === "object" && error !== null) {
    const errObj = error as Record<string, unknown>;
    if (typeof errObj.detail === "string") {
      return errObj.detail;
    }
    if (typeof errObj.detail === "object" && errObj.detail !== null) {
      const detailObj = errObj.detail as Record<string, unknown>;
      if (typeof detailObj.detail === "string") return detailObj.detail;
      if (typeof detailObj.message === "string") return detailObj.message;
    }
    if (Array.isArray(errObj.detail)) {
      return errObj.detail
        .map((d: Record<string, unknown>) => {
          const loc = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : "";
          const msg = typeof d.msg === "string" ? d.msg : JSON.stringify(d);
          return loc ? `${loc}: ${msg}` : msg;
        })
        .join("; ");
    }
    if (typeof errObj.message === "string") {
      return errObj.message;
    }
  }

  return String(error);
}

export function getApiErrorCode(error: unknown): string | null {
  if (typeof error === "object" && error !== null) {
    const errObj = error as Record<string, unknown>;
    if (typeof errObj.code === "string") return errObj.code;
    if (typeof errObj.detail === "object" && errObj.detail !== null) {
      const detailObj = errObj.detail as Record<string, unknown>;
      if (typeof detailObj.code === "string") return detailObj.code;
    }
  }
  return null;
}
