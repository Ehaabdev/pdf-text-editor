// Generates a Word document full of text decorations: underlines (single, double,
// colored, under part of a word, under a link), strikethroughs, highlights, and things
// that look similar but belong to the page, not the text (a paragraph border, a table
// border right under cell text, the footnote separator). All text is made up.
//   node decorations.js     -> ../../backend/tests/fixtures/decorations/decorations.docx
//   powershell export.ps1   -> decorations-word.pdf next to it, saved by Microsoft Word

const fs = require("fs");
const path = require("path");
const {
  BorderStyle, Document, ExternalHyperlink, FootnoteReferenceRun, Packer, Paragraph, Table, TableCell,
  TableRow, TextRun, UnderlineType, WidthType,
} = require("docx");

const OUT = path.resolve(__dirname, "../../backend/tests/fixtures/decorations");
const font = (f) => ({ ascii: f, hAnsi: f, cs: f, eastAsia: f });
const run = (text, o = {}) => new TextRun({ text, font: font(o.font || "Arial"), size: 24, sizeComplexScript: 24, ...o });
const ar = (text, o = {}) => run(text, { rightToLeft: true, ...o });
const p = (children, o = {}) => new Paragraph({ children, spacing: { after: 160 }, ...o });
const rtl = (children, o = {}) => p(children, { bidirectional: true, ...o });

const cellBorder = { style: BorderStyle.SINGLE, size: 8, color: "000000" };
const borders = { top: cellBorder, bottom: cellBorder, left: cellBorder, right: cellBorder };
// No cell padding and no paragraph spacing: the bottom border sits right under the text.
const tightCell = (text) =>
  new TableCell({
    borders,
    margins: { top: 0, bottom: 0, left: 80, right: 80 },
    width: { size: 3000, type: WidthType.DXA },
    children: [new Paragraph({ children: [run(text)], spacing: { before: 0, after: 0, line: 240 } })],
  });

const doc = new Document({
  footnotes: { 1: { children: [new Paragraph({ children: [run("A footnote under the separator line.", { size: 20 })] })] } },
  sections: [
    {
      children: [
        p([run("Plain line with an "), run("underlined phrase", { underline: {} }), run(" in the middle of it.")]),
        p([run("This line has a "), run("struck-out", { strike: true }), run(" word in it.")]),
        p([run("Twice struck: "), run("old price 450", { doubleStrike: true }), run(" new price 400.")]),
        p([run("Double underline here for the "), run("total amount", { underline: { type: UnderlineType.DOUBLE } }), run(".")]),
        p([run("Part of a word: un"), run("believ", { underline: {} }), run("able, and a "), run("colored underline", { underline: { color: "C00000" } }), run(".")]),
        p([run("A "), run("highlighted phrase", { highlight: "yellow" }), run(" sits in this sentence.")]),
        p([run("Write to "), new ExternalHyperlink({ link: "mailto:help@example.com", children: [run("help@example.com", { style: "Hyperlink", color: "0563C1", underline: {} })] }), run(" for help.")]),
        p([run("Fully underlined line of text", { underline: {} })]),
        p([run("A paragraph with a bottom border under it.")], { border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "000000", space: 1 } } }),
        new Table({
          columnWidths: [3000, 3000],
          rows: [
            new TableRow({ children: [tightCell("Cell text above a border"), tightCell("Second cell")] }),
            new TableRow({ children: [tightCell("Lower row"), tightCell("Last cell")] }),
          ],
        }),
        p([]),
        rtl([ar("هذا سطر فيه "), ar("كلمة مسطرة", { underline: {} }), ar(" في الوسط.")]),
        rtl([ar("وهنا "), ar("عبارة مظللة", { highlight: "yellow" }), ar(" داخل الجملة.")]),
        rtl([ar("السعر "), ar("القديم", { strike: true }), ar(" لم يعد صالحاً.")]),
        p([run("Ligne française avec un mot "), run("souligné", { underline: {} }), run(" et un renvoi."), new FootnoteReferenceRun(1)]),
      ],
    },
  ],
});

fs.mkdirSync(OUT, { recursive: true });
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(path.join(OUT, "decorations.docx"), buf);
  console.log("wrote decorations.docx");
});
