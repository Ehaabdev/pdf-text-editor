// Glass catches the light where the mouse is. Each lit surface gets the pointer's position
// in its own coordinates (--lx, --ly), at most once a frame. The values are set on these few
// elements only, never on the page root, and don't inherit (see styles.css), so the hundreds
// of line boxes on a page aren't restyled on every move.
const LIT = ".bar, .dock > .panel, .landing-bar, .dropzone, .canvas-zoom .zoom-group, .tool, .facts li";

export function startPointerLight(): () => void {
  if (!window.matchMedia?.("(hover: hover) and (pointer: fine)").matches) return () => {};
  let x = -9999;
  let y = -9999;
  let queued = false;

  const paint = () => {
    queued = false;
    for (const el of document.querySelectorAll<HTMLElement>(LIT)) {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--lx", `${x - r.left}px`);
      el.style.setProperty("--ly", `${y - r.top}px`);
    }
    // On the start screen the background's glow follows the mouse too.
    const glow = document.querySelector<HTMLElement>(".aurora--live");
    glow?.style.setProperty("--cx", `${x}px`);
    glow?.style.setProperty("--cy", `${y}px`);
  };
  const queue = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(paint);
  };
  const move = (e: PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    x = e.clientX;
    y = e.clientY;
    queue();
  };
  const leave = () => {
    x = y = -9999;
    queue();
  };

  window.addEventListener("pointermove", move, { passive: true });
  document.documentElement.addEventListener("pointerleave", leave);
  return () => {
    window.removeEventListener("pointermove", move);
    document.documentElement.removeEventListener("pointerleave", leave);
  };
}
