/**
 * Tells the user why leaving is denied. A guard never grants the navigation:
 * the page stays where it is, so there is nothing to resolve afterwards.
 */
export type LeaveBlocker = () => void;

let blocker: LeaveBlocker | null = null;

export function setLeaveBlocker(next: LeaveBlocker | null): void {
  blocker = next;
}

/**
 * Denies a navigation the app initiates itself (route change, shortcut) while
 * unsaved work is pending. Returns true when a guard swallowed the attempt.
 */
export function blockLeave(): boolean {
  if (!blocker) return false;
  blocker();
  return true;
}
