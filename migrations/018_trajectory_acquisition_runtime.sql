-- 既有 checkpoint 保持 NULL；预算和安全候选指针由采集边界显式初始化。
ALTER TABLE execution_checkpoints ADD COLUMN trajectory_runtime_json JSON;
