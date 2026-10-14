-- 旧 Launch 保持 NULL。冻结限额在 Manifest，运行预留独立持久化。
ALTER TABLE experiment_launches ADD COLUMN judge_budget_state_json JSON;
