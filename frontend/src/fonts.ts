/** A readable font name from a PDF font name: "TimesNewRomanPSMT" -> "Times New Roman". */
export function fontLabel(pdfName: string): string {
  let name = pdfName.split("+").pop() ?? pdfName;
  name = name.split(/[-,]/)[0]; // drop the style part: "-BoldItalic", ",Bold"
  name = name.replace(/(PSMT|PS|MT)$/, "");
  // Split CamelCase but keep runs of capitals together: "SegoeUI" -> "Segoe UI".
  name = name.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
  return name.trim() || pdfName;
}

export function styleLabel(bold: boolean, italic: boolean): string {
  if (bold && italic) return "Bold italic";
  if (bold) return "Bold";
  if (italic) return "Italic";
  return "Regular";
}

/** Points with at most one decimal: 12, 10.5. */
export function pointSize(size: number): string {
  const rounded = Math.round(size * 2) / 2;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)} pt`;
}
