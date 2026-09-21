import React from "react";

export type QualityConclusion = "pass" | "fail" | "unknown" | string;

interface QualityBadgeProps {
  quality: QualityConclusion;
  className?: string;
}

export const QualityBadge: React.FC<QualityBadgeProps> = ({ quality, className = "" }) => {
  const normalized = (quality || "unknown").toLowerCase();

  let colorClasses = "bg-slate-100 text-slate-600 border-slate-200";
  let label = "UNKNOWN";

  if (normalized === "pass") {
    colorClasses = "bg-emerald-50 text-emerald-700 border-emerald-200";
    label = "PASS";
  } else if (normalized === "fail") {
    colorClasses = "bg-rose-50 text-rose-700 border-rose-200";
    label = "FAIL";
  }

  return (
    <span
      data-testid="quality-badge"
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-bold border ${colorClasses} ${className}`}
    >
      {label}
    </span>
  );
};
