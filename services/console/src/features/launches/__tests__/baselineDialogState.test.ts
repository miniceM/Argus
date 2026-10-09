import { describe, it, expect } from "vitest";
import {
  baselineDialogReducer,
  initialBaselineDialogState,
  isConfirmDisabled,
} from "../baselineDialogState";

describe("baselineDialogReducer state machine", () => {
  it("transitions correctly through CAS 409 conflict and recovery sequence", () => {
    let state = initialBaselineDialogState;
    expect(state.stage).toBe("ready");
    expect(isConfirmDisabled(state.stage, true, false)).toBe(false);

    // 1. User confirms submission
    state = baselineDialogReducer(state, { type: "SUBMIT_START" });
    expect(state.stage).toBe("submitting");
    expect(isConfirmDisabled(state.stage, true, false)).toBe(true);

    // 2. HTTP 409 received
    state = baselineDialogReducer(state, { type: "CONFLICT_409", currentRevision: 4 });
    expect(state.stage).toBe("refreshing");
    expect(isConfirmDisabled(state.stage, true, false)).toBe(true);

    // 3. Fresh GET succeeds with revision 5
    state = baselineDialogReducer(state, { type: "REFRESH_SUCCESS", newRevision: 5 });
    expect(state.stage).toBe("ready_for_reconfirmation");
    expect(state.conflictRevision).toBe(5);
    expect(isConfirmDisabled(state.stage, true, false)).toBe(false);
  });

  it("handles transient non-409 POST failure and keeps retry enabled", () => {
    let state = initialBaselineDialogState;
    state = baselineDialogReducer(state, { type: "SUBMIT_START" });
    expect(state.stage).toBe("submitting");

    state = baselineDialogReducer(state, {
      type: "SUBMIT_FAILURE",
      error: "baseline temporarily unavailable",
    });
    expect(state.stage).toBe("submit_failed");
    expect(state.errorMessage).toBe("baseline temporarily unavailable");
    // Retry must be enabled
    expect(isConfirmDisabled(state.stage, true, false)).toBe(false);
  });

  it("handles refresh failure after 409 and disables confirm until recovered", () => {
    let state = initialBaselineDialogState;
    state = baselineDialogReducer(state, { type: "CONFLICT_409", currentRevision: 4 });
    expect(state.stage).toBe("refreshing");

    state = baselineDialogReducer(state, {
      type: "REFRESH_FAILURE",
      error: "GET failed with 503",
    });
    expect(state.stage).toBe("refresh_failed");
    expect(isConfirmDisabled(state.stage, true, false)).toBe(true);
  });
});
