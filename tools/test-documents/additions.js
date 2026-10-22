// Generates a Word document for adding text: lists (numbered and bulleted, English and
// Arabic), a centered title, a justified paragraph, a right-aligned line, a table with an
// empty cell, and free space after each block. All text is made up.
//   node additions.js       -> ../../backend/tests/fixtures/additions/additions.docx
//   powershell export.ps1   -> additions-word.pdf next to it, saved by Microsoft Word

const fs = require("fs");
const path = require("path");
const {
  AlignmentType, BorderStyle, Document, LevelFormat, Packer, Paragraph, Table, TableCell, TableRow, TextRun,
  WidthType,
} = require("docx");

const OUT = path.resolve(__dirname, "../../backend/tests/fixtures/additions");
const font = (f) => ({ ascii: f, hAnsi: f, cs: f, eastAsia: f });
const run = (text, { font: f = "Calibri", size = 11, ...rest } = {}) =>
  new TextRun({ text, font: font(f), size: size * 2, sizeComplexScript: size * 2, ...rest });
const ar = (text, o = {}) => run(text, { rightToLeft: true, font: "Arial", ...o });
// Free space after each block, so a line can be added under it.
const gap = (n = 1) => Array.from({ length: n }, () => new Paragraph({ children: [] }));

const cell = (text) =>
  new TableCell({
    width: { size: 3000, type: WidthType.DXA },
    borders: Object.fromEntries(["top", "bottom", "left", "right"].map((k) => [k, { style: BorderStyle.SINGLE, size: 6, color: "000000" }])),
    children: [new Paragraph({ children: text ? [run(text)] : [] })],
  });

const doc = new Document({
  numbering: {
    config: [
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "arabic", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.RIGHT, style: { paragraph: { indent: { right: 720, hanging: 360 } } } }] },
    ],
  },
  sections: [
    {
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, children: [run("Project Kickoff Notes", { size: 18, bold: true, color: "1F3864" })] }),
        ...gap(),
        new Paragraph({ numbering: { reference: "numbers", level: 0 }, children: [run("Confirm the delivery dates with the client")] }),
        new Paragraph({ numbering: { reference: "numbers", level: 0 }, children: [run("Share the draft budget with finance")] }),
        new Paragraph({ numbering: { reference: "numbers", level: 0 }, children: [run("Book the review meeting")] }),
        ...gap(2),
        new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [run("Laptops for the new team members")] }),
        new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [run("Access cards and parking")] }),
        ...gap(2),
        new Paragraph({
          alignment: AlignmentType.JUSTIFIED,
          children: [run("The team agreed to meet every Monday morning to review progress, share any blockers and plan the week ahead. Minutes will be sent to everyone by the end of the same day.")],
        }),
        ...gap(2),
        new Paragraph({ alignment: AlignmentType.RIGHT, children: [run("Prepared by the project office", { italics: true, color: "595959" })] }),
        ...gap(2),
        new Table({
          columnWidths: [3000, 3000],
          rows: [
            new TableRow({ children: [cell("Owner"), cell("Status")] }),
            new TableRow({ children: [cell("Planning"), cell("")] }),
          ],
        }),
        ...gap(2),
        new Paragraph({ bidirectional: true, numbering: { reference: "arabic", level: 0 }, children: [ar("مراجعة العقد مع الإدارة القانونية")] }),
        new Paragraph({ bidirectional: true, numbering: { reference: "arabic", level: 0 }, children: [ar("إرسال الفاتورة الأولى")] }),
        ...gap(2),
        new Paragraph({ bidirectional: true, children: [ar("ملاحظة: يرجى إرسال الملاحظات قبل نهاية الأسبوع.")] }),
        ...gap(2),
      ],
    },
  ],
});

fs.mkdirSync(OUT, { recursive: true });
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(path.join(OUT, "additions.docx"), buf);
  console.log("wrote additions.docx");
});
