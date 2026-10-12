import React, { useCallback, useEffect, useId, useRef } from "react";
import clsx from "clsx";
import { X } from "lucide-react";
import { IconButton } from "./Primitives";

/**
 * Modal / Drawer
 *
 * The console had eight hand-rolled overlays. Four declared no `role` or
 * `aria-modal`, three ignored Escape, and one used a raw literal black for
 * its backdrop instead of the `overlay` token — which is what happens when
 * the same component is written eight times.
 *
 * Everything a dialog owes its user lives here: the ARIA role, the labelled
 * title, Escape, backdrop dismissal, a Tab trap, focus restoration and the
 * scroll lock. Geometry is the only thing callers choose.
 */

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export interface OverlayProps {
  open: boolean;
  onClose: () => void;
  /** Plain text for the accessible name; also rendered as the visible title. */
  title: string;
  /** Rendered under the title, e.g. a record id. */
  subtitle?: React.ReactNode;
  /** Optional block to the left of the title. */
  icon?: React.ReactNode;
  /** Tints the header, e.g. `danger` for a destructive confirmation. */
  tone?: "neutral" | "danger";
  /**
   * When false, Escape and backdrop clicks do nothing. Use while a mutation is
   * in flight so a half-applied write cannot be abandoned mid-flight.
   */
  dismissable?: boolean;
  children: React.ReactNode;
  /** Rendered in a sticky footer, outside the scrollable body. */
  footer?: React.ReactNode;
  className?: string;
}

interface ShellProps extends OverlayProps {
  /**
   * `drawer` docks a full-height panel to the right; `modal` centres a card
   * that scrolls with the viewport.
   */
  variant: "modal" | "drawer";
  /** Drawer docking side; modal geometry ignores this value. */
  drawerSide?: "left" | "right";
  /** `sidebar` uses the shared navigation width; `default` uses the standard drawer width. */
  drawerSize?: "sidebar" | "default";
  /** Optional responsive visibility classes for the full-screen overlay layer. */
  overlayClassName?: string;
  /** Explicit focus-return target when opening changes/inerts the trigger's parent. */
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}

const Overlay: React.FC<ShellProps> = ({
  open,
  onClose,
  title,
  subtitle,
  icon,
  tone = "neutral",
  dismissable = true,
  children,
  footer,
  variant,
  drawerSide = "right",
  drawerSize = "default",
  overlayClassName,
  returnFocusRef,
  className,
}) => {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const returnFocusTo = useRef<HTMLElement | null>(null);
  const isDrawer = variant === "drawer";

  // Remember what had focus so closing returns the user where they were.
  // Without this, dismissing a dialog drops focus on <body> and keyboard
  // users restart at the top of the page.
  useEffect(() => {
    if (!open) return;
    returnFocusTo.current = returnFocusRef?.current ?? (document.activeElement as HTMLElement | null);
    return () => returnFocusTo.current?.focus?.();
  }, [open, returnFocusRef]);

  useEffect(() => {
    if (!open) return;
    // The page behind a modal must not scroll away underneath it.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  // Escape is handled on the window rather than the panel so it still works
  // when focus has not yet landed inside — or has escaped to <body>, which is
  // exactly when a user reaches for Escape.
  useEffect(() => {
    if (!open || !dismissable) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, dismissable, onClose]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== "Tab") return;

      // Trap Tab inside the panel. A modal that lets focus escape to the
      // inert page behind it strands keyboard users mid-task.
      const focusable = panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (!focusable || focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [],
  );

  // Move focus into the panel on open; on an empty panel, the panel itself.
  useEffect(() => {
    if (!open) return;
    // Prefer the first control in the body over the header's close button.
    // Opening a confirmation with focus on "close" puts a dismiss action
    // under the user's first Tab, which is where misfires happen.
    const inBody = bodyRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
    const inPanel = panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
    const focusable = inBody && inBody.length > 0 ? inBody : inPanel;
    (focusable?.[0] ?? panelRef.current)?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div
      className={clsx(
        "fixed inset-0 z-modal bg-overlay/50 backdrop-blur-xs",
        overlayClassName,
        isDrawer
          ? drawerSide === "left" ? "flex justify-start" : "flex justify-end"
          : "flex items-center justify-center overflow-y-auto p-4",
      )}
      onMouseDown={(event) => {
        if (dismissable && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={clsx(
          "relative flex flex-col border border-border bg-surface shadow-lg",
          isDrawer
            ? clsx(
              "h-full border-y-0",
              drawerSize === "sidebar" ? "w-sidebar max-w-sidebar" : "w-full max-w-drawer",
              drawerSide === "left" ? "border-l-0" : "border-r-0",
            )
            : "my-auto max-h-full w-full sm:max-w-modal",
          className,
        )}
      >
        <header
          className={clsx(
            "flex items-start justify-between gap-3 border-b px-6 py-4",
            tone === "danger" && "bg-fail-subtle/40",
          )}
        >
          <div className="flex min-w-0 items-center gap-3">
            {icon}
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-bold text-foreground">
                {title}
              </h2>
              {subtitle && (
                <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
                  {subtitle}
                </p>
              )}
            </div>
          </div>
          <IconButton label="关闭" onClick={onClose} disabled={!dismissable}>
            <X aria-hidden="true" className="w-4 h-4" />
          </IconButton>
        </header>

        <div ref={bodyRef} className="min-h-0 flex-1 overflow-y-auto">
          {children}
        </div>

        {footer && (
          <footer className="flex items-center justify-end gap-3 border-t border-border px-6 py-4">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
};

export const Modal = (props: Omit<ShellProps, "variant">) => (
  <Overlay {...props} variant="modal" />
);

export const SideDrawer = (props: Omit<ShellProps, "variant">) => (
  <Overlay {...props} variant="drawer" />
);
