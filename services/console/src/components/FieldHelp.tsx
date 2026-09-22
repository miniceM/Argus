import React, { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
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
  const [isMobile, setIsMobile] = useState(false);
  const [desktopStyle, setDesktopStyle] = useState<React.CSSProperties>({});

  const triggerRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  const updatePosition = useCallback(() => {
    const mobile = typeof window !== "undefined" && window.innerWidth < 640;
    setIsMobile(mobile);

    if (mobile || !triggerRef.current) {
      setDesktopStyle({});
      return;
    }

    const rect = triggerRef.current.getBoundingClientRect();
    const popoverWidth = 320; // 对应 w-80 基础宽度
    const margin = 12;

    let top = rect.bottom + 6;
    let left: number | undefined = rect.left;
    let right: number | undefined = undefined;

    if (placement === "bottom-right") {
      // 若向左延伸会挤出屏幕左侧，则改用靠左对齐并留出安全边距
      if (rect.right - popoverWidth < margin) {
        left = margin;
        right = undefined;
      } else {
        right = Math.max(margin, window.innerWidth - rect.right);
        left = undefined;
      }
    } else if (placement === "top-left") {
      // 向上展开时预留高度
      top = Math.max(margin, rect.top - 240);
      left = Math.max(margin, Math.min(rect.left, window.innerWidth - popoverWidth - margin));
    } else {
      // bottom-left: 默认向下靠左，防止超出右侧视口
      left = Math.max(margin, Math.min(rect.left, window.innerWidth - popoverWidth - margin));
    }

    setDesktopStyle({
      position: "fixed",
      top: `${top}px`,
      ...(left !== undefined ? { left: `${left}px` } : {}),
      ...(right !== undefined ? { right: `${right}px` } : {}),
    });
  }, [placement]);

  useEffect(() => {
    if (!isOpen) return;

    updatePosition();

    const handleResize = () => updatePosition();
    const handleScroll = () => updatePosition();

    window.addEventListener("resize", handleResize);
    window.addEventListener("scroll", handleScroll, true);

    const handleOutsideClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        triggerRef.current &&
        !triggerRef.current.contains(target) &&
        cardRef.current &&
        !cardRef.current.contains(target)
      ) {
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
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("scroll", handleScroll, true);
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen, updatePosition]);

  const renderContent = () => (
    <>
      {/* 头部 */}
      <div className="flex items-center justify-between pb-1.5 mb-2 border-b border-slate-100">
        <span className="font-bold text-slate-900 flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-indigo-600"></span>
          {title}
        </span>
        <button
          type="button"
          onClick={() => setIsOpen(false)}
          aria-label="关闭说明"
          className="text-slate-400 hover:text-slate-600 p-1 rounded-md hover:bg-slate-100 transition-colors cursor-pointer"
        >
          <X className="w-4 h-4 sm:w-3.5 sm:h-3.5" />
        </button>
      </div>

      {/* 内容主体 */}
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
    </>
  );

  return (
    <div className={`inline-flex items-center ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-label={`查看「${title}」说明`}
        aria-expanded={isOpen}
        className="text-slate-400 hover:text-indigo-600 transition-colors p-0.5 rounded-full hover:bg-indigo-50 focus:outline-none cursor-pointer"
        title="查看字段说明"
      >
        <CircleHelp className="w-3.5 h-3.5" />
      </button>

      {isOpen &&
        createPortal(
          isMobile ? (
            // 移动端：底部居中模态卡片 + 半透明遮罩，彻底杜绝被 dialog overflow 裁剪
            <div
              className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 p-4 animate-in fade-in duration-150"
              role="dialog"
              aria-modal="true"
              aria-label={`${title} 帮助说明`}
              onClick={(e) => {
                if (e.target === e.currentTarget) setIsOpen(false);
              }}
            >
              <div
                ref={cardRef}
                className="w-full max-w-md bg-white border border-slate-200 rounded-2xl shadow-2xl p-4 text-xs text-left animate-in slide-in-from-bottom-4 duration-200 max-h-[85vh] overflow-y-auto"
              >
                {renderContent()}
              </div>
            </div>
          ) : (
            // 桌面端：Portal 定位在按钮附近的 Popover，具备边缘安全碰撞检测
            <div
              ref={cardRef}
              style={desktopStyle}
              className="fixed z-50 w-80 sm:w-88 bg-white border border-slate-200 rounded-xl shadow-xl p-3.5 text-xs text-left animate-in fade-in zoom-in-95 duration-150"
              role="dialog"
              aria-label={`${title} 帮助说明`}
            >
              {renderContent()}
            </div>
          ),
          document.body
        )}
    </div>
  );
};
