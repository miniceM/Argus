-- Issue #85: a frozen result snapshot must be able to state how complete its
-- own evidence is.
--
-- Before #84 an evaluation-only retry never moved the Launch out of a terminal
-- status, so "the Launch finished" stopped implying "the evaluation finished".
-- Without an explicit evidence state a snapshot frozen during a re-judgement
-- would look like complete release evidence. The two columns below record the
-- verdict computed from the snapshot's own frozen items, so eligibility and
-- display never depend on the Launch's later lifecycle.
--
-- Historical rows default to COMPLETE: they were only ever created from
-- settled executions, and #85 forbids inventing evidence that a legacy row
-- never carried.
ALTER TABLE run_result_snapshots ADD COLUMN evidence_state VARCHAR(32) NOT NULL DEFAULT 'COMPLETE';
ALTER TABLE run_result_snapshots ADD COLUMN evidence_reasons JSON;
