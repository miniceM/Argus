/**
 * Explicit state machine for SetBaselineModal (Unit B / M02 & M03).
 */

export type BaselineDialogStage =
  | "ready"
  | "submitting"
  | "refreshing"
  | "ready_for_reconfirmation"
  | "refresh_failed"
  | "submit_failed";

export interface BaselineDialogState {
  stage: BaselineDialogStage;
  errorMessage: string | null;
  conflictRevision: number | null;
  conflictNotice: string | null;
}

export type BaselineDialogAction =
  | { type: "SUBMIT_START" }
  | { type: "CONFLICT_409"; currentRevision?: number }
  | { type: "REFRESH_SUCCESS"; newRevision: number }
  | { type: "REFRESH_FAILURE"; error: string }
  | { type: "SUBMIT_FAILURE"; error: string }
  | { type: "RESET" };

export const initialBaselineDialogState: BaselineDialogState = {
  stage: "ready",
  errorMessage: null,
  conflictRevision: null,
  conflictNotice: null,
};

export function baselineDialogReducer(
  state: BaselineDialogState,
  action: BaselineDialogAction,
): BaselineDialogState {
  switch (action.type) {
    case "SUBMIT_START":
      return {
        ...state,
        stage: "submitting",
        errorMessage: null,
      };

    case "CONFLICT_409":
      return {
        ...state,
        stage: "refreshing",
        errorMessage: null,
        conflictNotice: "Baseline 绑定版本发生并发冲突 (HTTP 409)，请重新确认最新状态后再试。",
        conflictRevision: action.currentRevision ?? null,
      };

    case "REFRESH_SUCCESS":
      return {
        ...state,
        stage: "ready_for_reconfirmation",
        errorMessage: null,
        conflictRevision: action.newRevision,
      };

    case "REFRESH_FAILURE":
      return {
        ...state,
        stage: "refresh_failed",
        errorMessage: action.error,
      };

    case "SUBMIT_FAILURE":
      return {
        ...state,
        stage: "submit_failed",
        errorMessage: action.error,
      };

    case "RESET":
      return initialBaselineDialogState;

    default:
      return state;
  }
}

export function isConfirmDisabled(
  stage: BaselineDialogStage,
  hasActiveSnapshot: boolean,
  isFetching: boolean,
): boolean {
  if (!hasActiveSnapshot || isFetching) return true;
  switch (stage) {
    case "submitting":
    case "refreshing":
    case "refresh_failed":
      return true;
    case "ready":
    case "ready_for_reconfirmation":
    case "submit_failed":
      return false;
  }
}
