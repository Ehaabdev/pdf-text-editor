import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { Icon, MOON, SUN } from "./Icon";

type Theme = "light" | "dark";

const KEY = "pdfedit.theme";
const BG: Record<Theme, string> = { light: "#e8edf5", dark: "#04060b" };
const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

function current(): Theme {
  const set = document.documentElement.dataset.theme;
  return set === "light" || set === "dark" ? set : systemDark() ? "dark" : "light";
}

function apply(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  // The browser's own bars follow the page.
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach((m) => (m.content = BG[theme]));
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Storage can be unavailable (private mode): the choice lasts until the page closes.
  }
}

/** Light or dark. The new look spreads in a circle from the button, where browsers can. */
export default function ThemeSwitch({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>(current);

  // Follow the system while nothing was chosen here.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const follow = () => setTheme(current());
    mq?.addEventListener("change", follow);
    return () => mq?.removeEventListener("change", follow);
  }, []);

  function toggle(e: React.MouseEvent<HTMLButtonElement>) {
    const next: Theme = theme === "dark" ? "light" : "dark";
    const change = () =>
      flushSync(() => {
        apply(next);
        setTheme(next);
      });
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!document.startViewTransition || still) return change();

    const r = e.currentTarget.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    const root = document.documentElement;
    root.classList.add("theme-switching");
    const t = document.startViewTransition(change);
    void t.ready.then(() =>
      root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 650, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)", pseudoElement: "::view-transition-new(root)" },
      ),
    );
    void t.finished.finally(() => root.classList.remove("theme-switching"));
  }

  const label = theme === "dark" ? "Switch to light" : "Switch to dark";
  return (
    <button className={`btn btn-ghost btn-icon theme-switch ${className}`} onClick={toggle} aria-label={label} title={label}>
      <Icon d={theme === "dark" ? SUN : MOON} />
    </button>
  );
}
