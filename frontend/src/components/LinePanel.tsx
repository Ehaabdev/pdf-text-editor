import type { TextLine, TextSpan } from "../api";
import { fontLabel, pointSize, styleLabel } from "../fonts";

type Look = Pick<TextSpan, "font" | "size" | "color" | "bold" | "italic">;

function sameLook(a: Look, b: Look) {
  return a.font === b.font && Math.abs(a.size - b.size) < 0.25 && a.color === b.color && a.bold === b.bold && a.italic === b.italic;
}

function Swatch({ color }: { color: string }) {
  return <span className="swatch" style={{ background: `#${color}` }} aria-hidden="true" />;
}

export default function LinePanel({ line, page, onClose }: { line: TextLine; page: number; onClose: () => void }) {
  // Other styles used in the line, besides the main one: a bold word, a colored word.
  const others: TextSpan[] = [];
  for (const s of line.spans) {
    if (s.text.trim() && !sameLook(s, line) && !others.some((o) => sameLook(o, s))) others.push(s);
  }

  return (
    <section className="line-panel" aria-label="Selected line">
      <div className="line-panel-head">
        <h2>Line on page {page}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <p className="line-text" dir={line.dir} lang={line.dir === "rtl" ? "ar" : undefined}>
        {line.display}
      </p>

      {line.uncertain && (
        <p className="line-warning">
          Some letters on this line are stored wrongly in the PDF itself, so the text above may not match the page. You'll
          be able to retype the whole line.
        </p>
      )}

      <dl className="line-style">
        <div>
          <dt>Font</dt>
          <dd>{fontLabel(line.font)}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>{pointSize(line.size)}</dd>
        </div>
        <div>
          <dt>Color</dt>
          <dd>
            <Swatch color={line.color} />#{line.color.toUpperCase()}
          </dd>
        </div>
        <div>
          <dt>Style</dt>
          <dd>{styleLabel(line.bold, line.italic)}</dd>
        </div>
      </dl>

      {others.length > 0 && (
        <div className="line-others">
          <h3>Also on this line</h3>
          <ul>
            {others.map((s, i) => (
              <li key={i}>
                <span dir="auto">“{s.text.trim()}”</span> in {fontLabel(s.font)}, {pointSize(s.size)},{" "}
                <Swatch color={s.color} />#{s.color.toUpperCase()}
                {s.bold || s.italic ? `, ${styleLabel(s.bold, s.italic).toLowerCase()}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
