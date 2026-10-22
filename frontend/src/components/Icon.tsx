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

/** The product's mark: a line of text being retyped, with the caret in the editing blue. */
export function Mark({ size = 24 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" className="mark">
      <rect width="24" height="24" rx="6" className="mark-tile" />
      <path d="M6 8.5h12M6 12.5h6.5" className="mark-lines" strokeWidth="2" strokeLinecap="round" />
      <path d="M15.5 10v6.5" className="mark-caret" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
