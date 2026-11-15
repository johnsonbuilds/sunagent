"use client";

import { usePathname } from "next/navigation";

import { matchRoute } from "@/lib/routes";

import IconRail from "./IconRail";
import SidebarPanel from "./shell/panels";

interface SidebarProps {
  collapsed: boolean;
  backdropVisible: boolean;
  onNavigate: () => void;
  onBackdropClick: () => void;
}

export default function Sidebar({
  collapsed,
  backdropVisible,
  onNavigate,
  onBackdropClick,
}: SidebarProps) {
  const pathname = usePathname();
  const panel = matchRoute(pathname)?.panel ?? null;
  const classes = [
    "sidebar",
    collapsed ? "collapsed" : "",
    panel ? "" : "bare",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <div
        className="sidebar-backdrop"
        id="sidebarBackdrop"
        hidden={!backdropVisible}
        onClick={onBackdropClick}
      />
      <aside className={classes} id="sidebar">
        <IconRail onNavigate={onNavigate} />
        {panel ? (
          <div className="sidebar-content">
            <SidebarPanel id={panel} />
          </div>
        ) : null}
      </aside>
    </>
  );
}
