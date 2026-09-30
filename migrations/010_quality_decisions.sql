-- Issue #83: the per-item quality decision is a domain fact, not a UI detail.
-- It records which frozen policy judged the item, the resulting conclusion and
-- the reason behind every rule, so a reviewer can always answer "why pass /
-- fail / unknown" without re-running anything. Historical rows stay NULL and
-- keep the verdict they were given under the policy of their time.
ALTER TABLE experiment_item_executions ADD COLUMN quality_evaluation JSON;
