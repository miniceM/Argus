# 共享 codec 修复后的真实 Reader 再校准

日期：2026-10-14。运行源码固定为 `e01bbf335320e68d5925a986ba63a39d79c684c7`；运行后 worktree 干净。共享 JSON codec 修复 Decimal 实现溢出后，Reader 制品摘要变化，旧来源批准记录被正确拒绝。此记录只批准隔离 banking fixture，不批准真实 Judge 模型或生产来源。

## 实测与批准

`test_reader_qualification_repeated_real_samples` 的 9 个参数组全部通过，**9 passed / 0 skipped / 58 deselected**，452.50 秒。Native OTel 与 Pydantic AI 各覆盖 serial、blocked、parallel、late_all，HTTP-only 覆盖 serial；每组 10 个不同 Attempt，共 90 个独立真实样本。保留原 deadline、制品往返、闭合父链与字段断言，迟到组验证真实延迟。每组全部样本通过后，真实 QualifiedReaderFactory 才执行 read/close 准入断言。

新的 Reader 制品摘要为 `sha256:8e17811ca31ab2fd85640e7369cb0a327cfecb530070847fc636860bb071b5d3`。现有 `summarize_calibration.py` 验证 9 组完整、每组 10 个唯一 Attempt、READY、原 deadline、Artifact roundtrip、parent closure、字段覆盖和真实 factory admission 后生成摘要；再与当前 reader.py/codec.py 的实际源码摘要核对。`docs/validation/s4-1/reader-calibration.json` 使用该生成物更新，未手工替换批准哈希。旧批准记录保留在 Git 历史，原版本实测仍按原 SHA 解释。

HTTP-only 最大 response_to_ready 为 12022.775ms；native_otel/pydantic_ai 的 late_all 最大分别为 10041.798/9918.604ms。原 12 秒预算与 250ms 收尾要求未放宽。

## 可复现证据

- [再校准摘要](reader-requalification-summary.json)：9 组身份、字段、完整度、容量和延迟分布。
- [稳定回执](reader-requalification-receipt.json)：运行源码、计数、命令、Reader 摘要与归档 SHA-256。
- [逐样本元数据](reader-requalification-field-matrix.json.gz)：原 2,307,595 字节 metadata-only 报告的 gzip 归档，不含原始业务 input/output 或凭据；含真实 Trace/Item/Attempt/Observation ID、父边、因果与字段计数。解压后的 SHA 与摘要中 raw_metadata_report_sha256 一致。

重现入口如下；使用新临时目录保留旧报告：

```bash
TASK_READER_DIR="$(mktemp -d)"
python -m pytest -vv -x tests/test_trajectory_observation_integration.py -k test_reader_qualification_repeated_real_samples -m trajectory_local --basetemp="$TASK_READER_DIR/pytest" --junitxml="$TASK_READER_DIR/junit.xml" --tb=short
python tests/fixtures/trajectory_agents/summarize_calibration.py "$TASK_READER_DIR/pytest/s400/field-matrix.json" "$TASK_READER_DIR/reader-calibration.json"
make trajectory-test
```

完整真实门禁仍需读取新的批准记录验证全部 86 项。本记录不将此前因旧身份拒绝的 17 项计作通过，也不代替真实 Judge 推理、独立 holdout 或跨 Worker 预算验收。
