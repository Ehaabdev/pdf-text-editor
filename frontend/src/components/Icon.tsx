import { useId } from "react";

/** One outline icon set: 20px grid, drawn with the current text color. */
export function Icon({ d, size = 18 }: { d: string; size?: number }) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} aria-hidden="true" className="icon">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export const CLOSE = "M5 5l10 10M15 5L5 15";
export const DOWNLOAD = "M10 3v9m0 0l-3.5-3.5M10 12l3.5-3.5M4 14.5V16h12v-1.5";
export const PLUS = "M10 4v12M4 10h12";
export const MINUS = "M4 10h12";
export const BOXES = "M3 4.5h14v4H3zM3 11.5h10v4H3z";
export const UNDO = "M7.5 5.5L4 9l3.5 3.5M4.5 9H12a4 4 0 010 8h-2";
export const REDO = "M12.5 5.5L16 9l-3.5 3.5M15.5 9H8a4 4 0 000 8h2";
export const ADD_TEXT = "M3 4.5h8M7 4.5V15M12.5 12.5h5M15 10v5";
export const CHANGES = "M7 5h10M7 10h10M7 15h10M3.5 5h.01M3.5 10h.01M3.5 15h.01";
export const COMPARE = "M10 3v14M3.5 5.5h4v9h-4zM12.5 5.5h4v9h-4z";

/** The product's mark: a line of text being retyped, the caret in it, on a glass-blue tile. */
export function Mark({ size = 24 }: { size?: number }) {
  const fill = useId();
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" className="mark">
      <defs>
        <linearGradient id={fill} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6fb3ff" />
          <stop offset="1" stopColor="#2b62f0" />
        </linearGradient>
      </defs>
      <rect width="24" height="24" rx="7" fill={`url(#${fill})`} />
      <rect x="0.5" y="0.5" width="23" height="23" rx="6.5" fill="none" stroke="#ffffff" strokeOpacity="0.35" />
      <path d="M6 8.5h12M6 13h6" stroke="#ffffff" strokeWidth="2" strokeLinecap="round" />
      <path d="M15 10.5v6.5" stroke="#bff3ff" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
export const UPLOAD = "M10 13V3.5m0 0L6.5 7M10 3.5L13.5 7M4 12.5V16h12v-3.5";
export const SUN = "M10 6.5a3.5 3.5 0 100 7 3.5 3.5 0 000-7zM10 2.5v1.5M10 16v1.5M2.5 10H4M16 10h1.5M4.7 4.7l1.06 1.06M14.24 14.24l1.06 1.06M4.7 15.3l1.06-1.06M14.24 5.76l1.06-1.06";
export const MOON = "M16 12.2A6.5 6.5 0 017.8 4a6.5 6.5 0 108.2 8.2z";
export const MAGNIFIER = "M8.5 3.5a5 5 0 100 10 5 5 0 000-10zM12.2 12.2l4.3 4.3";
export const HELP = "M10 17.5a7.5 7.5 0 100-15 7.5 7.5 0 000 15zM7.9 7.7a2.2 2.2 0 014.2.9c0 1.5-2.1 1.8-2.1 3.3M10 14.3h.01";
export const PAGES = "M7 3.5h6.5l3 3V15H7zM4 6.5v10h9";
export const PICTURE = "M3.5 4.5h13v11h-13zM3.5 13l4-4 3.5 3.5 2-2 4 4M12.5 7.5h.01";
export const CHEVRON_DOWN = "M6 8l4 4 4-4";
