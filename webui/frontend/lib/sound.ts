/**
 * Notification chime for board transitions.
 *
 * Browsers keep an `AudioContext` suspended until the page has seen a user
 * gesture, so the context is created lazily, unlocked on the first gesture,
 * and reused: a transition found by polling must not be dropped just because
 * it happened outside a click.
 */

let context: AudioContext | null = null;
let primed = false;

function audioContextClass(): typeof AudioContext | null {
  if (typeof window === "undefined") return null;
  return (
    window.AudioContext
    ?? (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext
    ?? null
  );
}

function ensureContext(): AudioContext | null {
  if (context) return context;
  const AudioContextClass = audioContextClass();
  if (!AudioContextClass) return null;
  try {
    context = new AudioContextClass();
  } catch {
    return null;
  }
  return context;
}

/** Unlock audio on the first user gesture; safe to call on every mount. */
export function primeNotificationSound(): void {
  if (primed || typeof window === "undefined") return;
  primed = true;
  const unlock = () => {
    const ctx = ensureContext();
    if (ctx?.state === "suspended") void ctx.resume();
  };
  window.addEventListener("pointerdown", unlock, { passive: true });
  window.addEventListener("keydown", unlock, { passive: true });
}

/** Play a short two-note chime. Best-effort: any failure stays silent. */
export function playNotificationChime(): void {
  const ctx = ensureContext();
  if (!ctx) return;
  if (ctx.state === "suspended") void ctx.resume();
  try {
    const start = ctx.currentTime;
    const gain = ctx.createGain();
    // Peaking near 0.08 keeps the chime audible without startling.
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.08, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.4);
    gain.connect(ctx.destination);
    // E5 -> B5: a rising, "something finished" interval.
    [659.25, 987.77].forEach((frequency, index) => {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.setValueAtTime(frequency, start);
      osc.connect(gain);
      osc.start(start + index * 0.12);
      osc.stop(start + 0.4);
    });
  } catch {
    /* Audio playback is best-effort; never surface errors to the user. */
  }
}
