import React from "react";

export type ExecutionStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | string;

interface StatusBadgeProps {
  status: ExecutionStatus;
  className?: string;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({ status, className = "" }) => {
  const normalized = status.toUpperCase();

  let colorClasses = "bg-slate-100 text-slate-700 border-slate-300";
  if (normalized === "SUCCEEDED") {
    colorClasses = "bg-emerald-50 text-emerald-700 border-emerald-300";
  } else if (normalized === "FAILED") {
    colorClasses = "bg-rose-50 text-rose-700 border-rose-300";
  } else if (normalized === "RUNNING") {
    colorClasses = "bg-sky-50 text-sky-700 border-sky-300 animate-pulse";
  } else if (normalized === "PENDING") {
    colorClasses = "bg-amber-50 text-amber-700 border-amber-300";
  }

  return (
    <span
      data-testid="status-badge"
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold border ${colorClasses} ${className}`}
    >
      {normalized}
    </span>
  );
};
