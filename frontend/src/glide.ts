import { useEffect, type RefObject } from "react";

// Gliding along a strip wider than its frame: speed (px per second) and the rest at each end (ms).
const SPEED = 36;
const REST = 1400;

/** A strip of print wider than its frame glides slowly from its start to its end and back,
 *  like a loupe moved along a line. Touching it (or asking for less motion) hands control to
 *  the reader. `dir` is the text's direction: an RTL strip starts at its right-hand end. */
export function useGlide(ref: RefObject<HTMLElement | null>, dir: "ltr" | "rtl", key: unknown) {
  useEffect(() => {
    const el = ref.current;
    if (!el || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const sign = dir === "rtl" ? -1 : 1; // RTL scroll positions run negative
    let frame = 0;
    let at = 0;
    let towards = 1;
    let restUntil = performance.now() + REST;
    let last = performance.now();
    const tick = (now: number) => {
      const room = el.scrollWidth - el.clientWidth;
      const dt = now - last;
      last = now;
      if (room > 1 && now >= restUntil) {
        at += (towards * SPEED * dt) / 1000;
        if (at >= room || at <= 0) {
          at = Math.max(0, Math.min(room, at));
          towards = -towards;
          restUntil = now + REST;
        }
        el.scrollLeft = sign * at;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    const stop = () => cancelAnimationFrame(frame);
    el.addEventListener("pointerdown", stop);
    el.addEventListener("wheel", stop, { passive: true });
    el.addEventListener("touchstart", stop, { passive: true });
    return () => {
      stop();
      el.removeEventListener("pointerdown", stop);
      el.removeEventListener("wheel", stop);
      el.removeEventListener("touchstart", stop);
    };
  }, [ref, dir, key]);
}
