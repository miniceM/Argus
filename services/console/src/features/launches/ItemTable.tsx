import React, { useState } from "react";
import { Clock, Eye, Filter, Layers } from "lucide-react";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { AttemptDrawer } from "./AttemptDrawer";

type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];

interface ItemTableProps {
  items: ItemExecution[];
}

export const ItemTable: React.FC<ItemTableProps> = ({ items }) => {
  const [selectedItem, setSelectedItem] = useState<{ id: string; caseId: string } | null>(null);
  const [filterQuality, setFilterQuality] = useState<string>("ALL");

  const filteredItems = items.filter((item) => {
    const q = item.quality_conclusion?.toLowerCase();
    const st = item.execution_status?.toLowerCase();
    if (filterQuality === "PASS") return q === "pass";
    if (filterQuality === "FAIL") return q === "fail";
    if (filterQuality === "FAILED") return st === "failed" || st === "timed_out";
    if (filterQuality === "RETRY_WAIT") return st === "retry_wait";
    if (filterQuality === "CANCELLED") return st === "cancelled";
    return true;
  });

  const retryWaitCount = items.filter((i) => i.execution_status?.toLowerCase() === "retry_wait").length;
  const failedCount = items.filter((i) => ["failed", "timed_out"].includes(i.execution_status?.toLowerCase() || "")).length;
  const cancelledCount = items.filter((i) => i.execution_status?.toLowerCase() === "cancelled").length;

  return (
    <div className="space-y-4">
      {/* Table Sub-header & Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-indigo-600" />
          <h3 className="text-sm font-bold text-slate-900">
            评测用例明细 (Dataset Items & Evaluations)
          </h3>
          <span className="text-xs text-slate-400">
            共 {items.length} 个用例
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Filter className="w-3.5 h-3.5 text-slate-400" />
          <button
            onClick={() => setFilterQuality("ALL")}
            className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
              filterQuality === "ALL"
                ? "bg-slate-800 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            全部 ({items.length})
          </button>
          <button
            onClick={() => setFilterQuality("PASS")}
            className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
              filterQuality === "PASS"
                ? "bg-emerald-600 text-white"
                : "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
            }`}
          >
            质量通过 ({items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length})
          </button>
          <button
            onClick={() => setFilterQuality("FAIL")}
            className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
              filterQuality === "FAIL"
                ? "bg-rose-600 text-white"
                : "bg-rose-50 text-rose-700 hover:bg-rose-100"
            }`}
          >
            未通过 ({items.filter((i) => i.quality_conclusion?.toLowerCase() === "fail").length})
          </button>
          {failedCount > 0 && (
            <button
              onClick={() => setFilterQuality("FAILED")}
              className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
                filterQuality === "FAILED"
                  ? "bg-amber-600 text-white"
                  : "bg-amber-50 text-amber-700 hover:bg-amber-100"
              }`}
            >
              失败/超时 ({failedCount})
            </button>
          )}
          {retryWaitCount > 0 && (
            <button
              onClick={() => setFilterQuality("RETRY_WAIT")}
              className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
                filterQuality === "RETRY_WAIT"
                  ? "bg-yellow-600 text-white"
                  : "bg-yellow-50 text-yellow-700 hover:bg-yellow-100"
              }`}
            >
              等待重试 ({retryWaitCount})
            </button>
          )}
          {cancelledCount > 0 && (
            <button
              onClick={() => setFilterQuality("CANCELLED")}
              className={`px-2.5 py-1 rounded-md font-medium cursor-pointer transition-colors ${
                filterQuality === "CANCELLED"
                  ? "bg-gray-600 text-white"
                  : "bg-gray-100 text-gray-700 hover:bg-gray-200"
              }`}
            >
              已取消 ({cancelledCount})
            </button>
          )}
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-xs">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-slate-600">
            <thead className="bg-slate-50/75 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase tracking-wider">
              <tr>
                <th className="px-5 py-3.5">用例标识 (Dataset Item ID)</th>
                <th className="px-5 py-3.5">执行状态 (Execution)</th>
                <th className="px-5 py-3.5">质量门禁 (Quality)</th>
                <th className="px-5 py-3.5">评测得分 (Scores)</th>
                <th className="px-5 py-3.5">最终 HTTP</th>
                <th className="px-5 py-3.5">最终耗时</th>
                <th className="px-5 py-3.5">尝试次数 (Attempts)</th>
                <th className="px-5 py-3.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredItems.map((item) => {
                const errorText = item.execution_error || item.eval_error;

                return (
                  <tr
                    key={item.id}
                    className="hover:bg-slate-50/80 transition-colors"
                  >
                    <td className="px-5 py-3.5 font-mono text-xs font-bold text-slate-900">
                      <div className="flex items-center gap-1.5">
                        <span>{item.dataset_item_id}</span>
                        {item.dispatch_generation && item.dispatch_generation > 1 && (
                          <span className="px-1.5 py-0.2 rounded text-[10px] font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200">
                            gen #{item.dispatch_generation}
                          </span>
                        )}
                      </div>
                      {errorText && (
                        <p className="text-[11px] text-rose-600 font-normal truncate max-w-xs mt-0.5" title={errorText}>
                          {errorText}
                        </p>
                      )}
                    </td>

                    <td className="px-5 py-3.5">
                      <StatusBadge status={item.execution_status} />
                    </td>

                    <td className="px-5 py-3.5">
                      <QualityBadge quality={item.quality_conclusion} />
                    </td>

                    <td className="px-5 py-3.5">
                      {item.scores && Object.keys(item.scores).length > 0 ? (
                        <div className="flex flex-wrap gap-1.5">
                          {Object.entries(item.scores).map(([k, v]) => (
                            <span
                              key={k}
                              className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-mono bg-slate-100 text-slate-700 border border-slate-200"
                            >
                              <span className="text-slate-400 mr-1">{k}:</span>
                              <span
                                className={`font-semibold ${
                                  Number(v) >= 1
                                    ? "text-emerald-700"
                                    : Number(v) > 0
                                    ? "text-amber-600"
                                    : "text-rose-600"
                                }`}
                              >
                                {typeof v === "number" ? v.toFixed(2) : String(v)}
                              </span>
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400 font-mono">-</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5 font-mono text-xs">
                      {item.final_attempt_http_status ? (
                        <span
                          className={`font-semibold ${
                            item.final_attempt_http_status >= 200 &&
                            item.final_attempt_http_status < 300
                              ? "text-emerald-600"
                              : "text-rose-600"
                          }`}
                        >
                          {item.final_attempt_http_status}
                        </span>
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5 font-mono text-xs text-slate-500">
                      {item.final_attempt_latency_ms !== null &&
                      item.final_attempt_latency_ms !== undefined ? (
                        <span className="flex items-center gap-1">
                          <Clock className="w-3.5 h-3.5 text-slate-400" />
                          <span>{item.final_attempt_latency_ms} ms</span>
                        </span>
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5">
                      <button
                        onClick={() =>
                          setSelectedItem({ id: item.id, caseId: item.dataset_item_id })
                        }
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-mono font-medium bg-slate-100 text-slate-700 hover:bg-indigo-50 hover:text-indigo-600 transition-colors cursor-pointer"
                        title="查看 Attempt 调用历史"
                      >
                        <span>{item.attempt_count} 次尝试</span>
                        <Eye className="w-3.5 h-3.5" />
                      </button>
                    </td>

                    <td className="px-5 py-3.5 text-right">
                      <button
                        onClick={() =>
                          setSelectedItem({ id: item.id, caseId: item.dataset_item_id })
                        }
                        className="text-xs font-semibold text-indigo-600 hover:text-indigo-800 cursor-pointer"
                      >
                        明细
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Attempt Drawer Modal */}
      <AttemptDrawer
        isOpen={Boolean(selectedItem)}
        onClose={() => setSelectedItem(null)}
        itemExecutionId={selectedItem?.id || null}
        caseId={selectedItem?.caseId || null}
      />
    </div>
  );
};
