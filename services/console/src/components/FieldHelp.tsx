import React, { useState, useRef, useEffect } from "react";
import { CircleHelp, X } from "lucide-react";

export interface FieldHelpProps {
  title: string;
  meaning: string;
  rules: React.ReactNode;
  example: string;
  placement?: "bottom-left" | "bottom-right" | "top-left";
  className?: string;
}

export const FieldHelp: React.FC<FieldHelpProps> = ({
  title,
  meaning,
  rules,
  example,
  placement = "bottom-left",
  className = "",
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    const handleOutsideClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    };

    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  const placementClasses = {
    "bottom-left": "left-0 top-6",
    "bottom-right": "right-0 top-6",
    "top-left": "left-0 bottom-6",
  }[placement];

  return (
    <div ref={containerRef} className={`relative inline-flex items-center ${className}`}>
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-label={`查看「${title}」说明`}
        aria-expanded={isOpen}
        className="text-slate-400 hover:text-indigo-600 transition-colors p-0.5 rounded-full hover:bg-indigo-50 focus:outline-none cursor-pointer"
        title="查看字段说明"
      >
        <CircleHelp className="w-3.5 h-3.5" />
      </button>

      {isOpen && (
        <div
          className={`absolute z-40 w-80 sm:w-88 bg-white border border-slate-200 rounded-xl shadow-xl p-3.5 text-xs text-left animate-in fade-in zoom-in-95 duration-150 ${placementClasses}`}
          role="dialog"
          aria-label={`${title} 帮助说明`}
        >
          {/* Header */}
          <div className="flex items-center justify-between pb-1.5 mb-2 border-b border-slate-100">
            <span className="font-bold text-slate-900 flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-indigo-600"></span>
              {title}
            </span>
            <button
              type="button"
              onClick={() => setIsOpen(false)}
              aria-label="关闭说明"
              className="text-slate-400 hover:text-slate-600 p-0.5 rounded-md hover:bg-slate-100 transition-colors cursor-pointer"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Content */}
          <div className="space-y-2.5 text-slate-600">
            <div>
              <span className="font-semibold text-slate-700">📌 含义与作用：</span>
              <p className="mt-0.5 leading-relaxed text-slate-600">{meaning}</p>
            </div>

            <div>
              <span className="font-semibold text-slate-700">📝 配置规范：</span>
              <div className="mt-0.5 leading-relaxed text-slate-600">{rules}</div>
            </div>

            <div>
              <span className="font-semibold text-slate-700">💡 参考示例：</span>
              <div className="mt-1 bg-slate-50 p-2 rounded-lg border border-slate-200/80 font-mono text-[11px] text-indigo-700 break-all select-all">
                {example}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
