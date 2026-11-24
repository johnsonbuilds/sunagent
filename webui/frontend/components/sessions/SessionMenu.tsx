"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { MoreVerticalIcon } from "../Icons";

export interface SessionMenuItem {
  key: string;
  label: string;
  danger?: boolean;
  onSelect: () => void;
}

interface SessionMenuProps {
  label: string;
  items: SessionMenuItem[];
}

const GAP = 4;
const VIEWPORT_MARGIN = 8;

interface MenuPosition {
  top: number;
  left: number;
}

/**
 * Vertical-dots trigger with a dropdown menu.
 *
 * The menu is portalled to <body> and positioned with fixed coordinates, so
 * the sidebar's scroll container cannot clip it. It follows the trigger on
 * scroll/resize; an outside press or Escape closes it.
 */
export default function SessionMenu({ label, items }: SessionMenuProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<MenuPosition | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const close = useCallback((returnFocus = false) => {
    setOpen(false);
    setPosition(null);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  // Follow the trigger instead of closing on scroll: focusing the first
  // menu item can itself scroll a container, and a scrolling sidebar is
  // not a reason to dismiss the menu.
  const place = useCallback(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;

    const anchor = trigger.getBoundingClientRect();
    const box = menu.getBoundingClientRect();
    const spaceBelow =
      window.innerHeight - anchor.bottom - GAP - VIEWPORT_MARGIN;
    const fitsAbove = anchor.top - GAP - VIEWPORT_MARGIN >= box.height;
    const top =
      spaceBelow >= box.height || !fitsAbove
        ? anchor.bottom + GAP
        : anchor.top - box.height - GAP;
    const left = Math.max(
      VIEWPORT_MARGIN,
      Math.min(
        anchor.right - box.width,
        window.innerWidth - box.width - VIEWPORT_MARGIN,
      ),
    );

    setPosition({ top, left });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    menuRef.current
      ?.querySelector<HTMLElement>('[role="menuitem"]')
      ?.focus();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (
        menuRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      close();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
        return;
      }
      if (event.key === "Tab") {
        close();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      const entries = Array.from(
        menuRef.current?.querySelectorAll<HTMLElement>(
          '[role="menuitem"]',
        ) ?? [],
      );
      if (!entries.length) return;
      const current = entries.indexOf(
        document.activeElement as HTMLElement,
      );
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next =
        current === -1
          ? 0
          : (current + step + entries.length) % entries.length;
      entries[next].focus();
    };

    const reposition = () => place();

    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [close, open, place]);

  const select = (item: SessionMenuItem) => {
    close();
    item.onSelect();
  };

  return (
    <>
      <button
        ref={triggerRef}
        className="session-actions"
        type="button"
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <MoreVerticalIcon />
      </button>
      {open
        ? createPortal(
            <div
              ref={menuRef}
              className="session-menu"
              role="menu"
              aria-label={label}
              style={{
                top: position ? position.top : -9999,
                left: position ? position.left : -9999,
                visibility: position ? "visible" : "hidden",
              }}
              // Portal events bubble through the React tree, not the DOM:
              // stop them here so the row does not also get selected.
              onClick={(event) => event.stopPropagation()}
            >
              {items.map((item) => (
                <button
                  key={item.key}
                  className={`session-menu-item${
                    item.danger ? " danger" : ""
                  }`}
                  type="button"
                  role="menuitem"
                  onClick={() => select(item)}
                >
                  {item.label}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
