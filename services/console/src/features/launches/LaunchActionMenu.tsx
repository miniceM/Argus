/**
 * Accessible "more actions" menu for the Launch header (issue 119 gap U4).
 *
 * The narrow layout keeps a compact primary action and an icon refresh, and moves
 * every remaining capability into this menu instead of spreading them across a
 * width they do not fit. The menu reuses the existing Button primitives and
 * semantic tokens — no new UI library, no parallel menu framework.
 *
 * Accessibility contract (matches the acceptance criteria):
 *   - trigger exposes aria-haspopup=menu / aria-expanded / aria-controls;
 *   - role=menu with menuitem buttons and real links (never href="#");
 *   - ArrowUp/ArrowDown/Home/End move between enabled items, disabled and
 *     pending items are skipped and never execute;
 *   - Escape closes and returns focus to the trigger; Tab closes and lets focus
 *     leave naturally; click-outside closes;
 *   - selecting an item closes the menu first and restores focus to the visible
 *     trigger before the existing handler runs, so a confirmation Modal owns
 *     focus while open and returns to the trigger when dismissed.
 */
import React, { useCallback, useEffect, useId, useRef } from "react";
import { ExternalLink, MoreHorizontal } from "lucide-react";
import { Button, buttonClassName } from "../../components/ui/Primitives";

export type LaunchActionMenuItem =
  | {
      kind: "action";
      id: string;
      label: string;
      pendingLabel?: string;
      pending?: boolean;
      icon?: React.ReactNode;
      variant?: "primary" | "secondary" | "danger" | "warning" | "warning-solid" | "quiet";
      title?: string;
      /** Test hook so E2E can address the same control in either layout. */
      testId?: string;
      onSelect: () => void;
    }
  | {
      kind: "link";
      id: string;
      label: string;
      href: string;
      detailLabel?: string;
      title?: string;
    }
  | {
      /** Read-only explanation (for example why no Langfuse link exists). */
      kind: "note";
      id: string;
      label: string;
      title?: string;
    };

const ITEM_ROW_CLASS =
  "w-full justify-start gap-2 text-xs font-semibold min-h-8 h-auto py-1.5 px-2.5 whitespace-normal";

const focusableSelector = '[role="menuitem"]:not([aria-disabled="true"]):not([tabindex="-1"])';

export const LaunchActionMenu: React.FC<{
  items: LaunchActionMenuItem[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  triggerLabel?: string;
  triggerClassName?: string;
}> = ({ items, open, onOpenChange, triggerLabel = "更多操作", triggerClassName }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const closeMenu = useCallback(
    (restoreFocus: boolean) => {
      onOpenChange(false);
      if (restoreFocus) triggerRef.current?.focus();
    },
    [onOpenChange],
  );

  // Close on outside pointer-down, so a stray click never leaves an orphan menu.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!containerRef.current?.contains(target)) onOpenChange(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open, onOpenChange]);

  // Land on the first executable item when the menu opens.
  useEffect(() => {
    if (!open) return;
    const first = menuRef.current?.querySelectorAll<HTMLElement>(focusableSelector)[0];
    first?.focus();
  }, [open]);

  const focusableItems = () =>
    Array.from(menuRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? []);

  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeMenu(true);
      return;
    }
    if (event.key === "Tab") {
      // Let focus leave the menu naturally; closing is enough.
      onOpenChange(false);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const menuItems = focusableItems();
      if (menuItems.length === 0) return;
      const current = document.activeElement as HTMLElement | null;
      const index = current ? menuItems.indexOf(current) : -1;
      let next: number;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = menuItems.length - 1;
      else if (event.key === "ArrowDown") next = index < 0 ? 0 : (index + 1) % menuItems.length;
      else next = index < 0 ? menuItems.length - 1 : (index - 1 + menuItems.length) % menuItems.length;
      menuItems[next]?.focus();
    }
  };

  const runItem = (item: Extract<LaunchActionMenuItem, { kind: "action" }>) => {
    // Close first and restore focus to the visible trigger, then let the existing
    // handler open its confirmation modal on top of a clean focus state.
    closeMenu(true);
    item.onSelect();
  };

  return (
    <div ref={containerRef} className="relative">
      <Button
        ref={triggerRef}
        variant="secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        data-testid="launch-more-actions"
        onClick={() => onOpenChange(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            onOpenChange(true);
          }
        }}
        className={buttonClassName("secondary", `min-h-8 h-auto py-1 px-2.5 text-xs whitespace-nowrap ${triggerClassName ?? ""}`)}
      >
        <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
        <span>{triggerLabel}</span>
      </Button>

      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label="当前 Launch 操作"
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 top-full z-50 mt-1 max-h-60 min-w-56 max-w-80 overflow-y-auto rounded-lg border border-border bg-surface p-1 shadow-lg"
        >
          {items.map((item) => {
            if (item.kind === "note") {
              return (
                <p
                  key={item.id}
                  className="px-2.5 py-1.5 text-micro text-muted-foreground"
                  title={item.title}
                >
                  {item.label}
                </p>
              );
            }
            if (item.kind === "link") {
              return (
                <a
                  key={item.id}
                  role="menuitem"
                  href={item.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={item.title}
                  className={buttonClassName("secondary", ITEM_ROW_CLASS)}
                >
                  <span>{item.label}</span>
                  <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                </a>
              );
            }
            const disabled = Boolean(item.pending);
            return (
              <Button
                key={item.id}
                variant={item.variant ?? "secondary"}
                role="menuitem"
                aria-disabled={disabled || undefined}
                disabled={disabled}
                data-testid={item.testId}
                title={item.title}
                onClick={() => runItem(item)}
                className={buttonClassName(item.variant ?? "secondary", ITEM_ROW_CLASS)}
              >
                {item.icon}
                <span>{item.pending && item.pendingLabel ? item.pendingLabel : item.label}</span>
              </Button>
            );
          })}
        </div>
      )}
    </div>
  );
};
