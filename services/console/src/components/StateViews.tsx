import React from "react";
import { AlertCircle, FolderOpen, Loader2 } from "lucide-react";

export const LoadingState: React.FC<{ message?: string }> = ({ message = "正在加载数据..." }) => (
  <div data-testid="loading-state" className="flex flex-col items-center justify-center p-12 text-slate-500">
    <Loader2 className="w-8 h-8 animate-spin text-indigo-600 mb-3" />
    <p className="text-sm font-medium">{message}</p>
  </div>
);

export const EmptyState: React.FC<{ title: string; description?: string; action?: React.ReactNode }> = ({
  title,
  description,
  action,
}) => (
  <div data-testid="empty-state" className="flex flex-col items-center justify-center p-12 border-2 border-dashed border-slate-200 rounded-xl text-center">
    <FolderOpen className="w-10 h-10 text-slate-400 mb-3" />
    <h3 className="text-base font-semibold text-slate-800">{title}</h3>
    {description && <p className="text-sm text-slate-500 mt-1 max-w-sm">{description}</p>}
    {action && <div className="mt-4">{action}</div>}
  </div>
);

export const ErrorState: React.FC<{ message: string; onRetry?: () => void }> = ({ message, onRetry }) => (
  <div data-testid="error-state" className="flex flex-col items-center justify-center p-8 bg-rose-50 border border-rose-200 rounded-xl text-center">
    <AlertCircle className="w-8 h-8 text-rose-500 mb-2" />
    <h3 className="text-sm font-semibold text-rose-800">请求失败</h3>
    <p className="text-xs text-rose-600 mt-1 max-w-md">{message}</p>
    {onRetry && (
      <button
        onClick={onRetry}
        className="mt-3 px-3 py-1.5 text-xs font-medium text-rose-700 bg-white border border-rose-300 rounded-lg hover:bg-rose-50 cursor-pointer"
      >
        重新加载
      </button>
    )}
  </div>
);
