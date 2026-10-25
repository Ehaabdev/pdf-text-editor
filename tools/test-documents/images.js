// Generates a Word document with pictures, for deleting, replacing, adding and moving
// images: a logo in the header (on every page), a photo with a caption, a small icon inside
// a line of text, a signature and a stamp in a table, and Arabic text. All made up; the
// pictures come from make_images.py.
//   python make_images.py   -> ../../backend/tests/fixtures/images/*.png, photo.jpg
//   node images.js          -> ../../backend/tests/fixtures/images/images.docx
//   powershell export.ps1   -> images-word.pdf next to it, saved by Microsoft Word

const fs = require("fs");
const path = require("path");
const {
  AlignmentType, BorderStyle, Document, Header, ImageRun, Packer, PageBreak, Paragraph, Table, TableCell, TableRow,
  TextRun, WidthType,
} = require("docx");

const OUT = path.resolve(__dirname, "../../backend/tests/fixtures/images");
const font = (f) => ({ ascii: f, hAnsi: f, cs: f, eastAsia: f });
const run = (text, { font: f = "Calibri", size = 11, ...rest } = {}) =>
  new TextRun({ text, font: font(f), size: size * 2, sizeComplexScript: size * 2, ...rest });
const ar = (text, o = {}) => run(text, { rightToLeft: true, font: "Arial", ...o });
const picture = (name, width, height) =>
  new ImageRun({
    type: name.endsWith(".jpg") ? "jpg" : "png",
    data: fs.readFileSync(path.join(OUT, name)),
    transformation: { width, height },
  });
const line = (border) => ({ style: BorderStyle.SINGLE, size: 6, color: "000000" });
const cell = (children, width = 4500) =>
  new TableCell({
    width: { size: width, type: WidthType.DXA },
    borders: Object.fromEntries(["top", "bottom", "left", "right"].map((k) => [k, line()])),
    children,
  });

const doc = new Document({
  sections: [
    {
      headers: {
        default: new Header({ children: [new Paragraph({ children: [picture("logo.png", 150, 50)] })] }),
      },
      children: [
        new Paragraph({ alignment: AlignmentType.CENTER, children: [run("Site Visit Report", { size: 20, bold: true })] }),
        new Paragraph({ children: [] }),
        new Paragraph({
          children: [run("The team visited the north yard on 12 October 2026 to check the new storage area and the loading bays.")],
        }),
        new Paragraph({ children: [] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [picture("photo.jpg", 360, 240)] }),
        new Paragraph({ alignment: AlignmentType.CENTER, children: [run("Figure 1: the north yard from the gate.", { italics: true, size: 10 })] }),
        new Paragraph({ children: [] }),
        new Paragraph({ children: [run("Status: "), picture("icon.png", 16, 16), run(" approved by the site manager.")] }),
        new Paragraph({ children: [] }),
        new Table({
          columnWidths: [4500, 4500],
          rows: [
            new TableRow({
              children: [
                cell([new Paragraph({ children: [run("Inspector")] })]),
                cell([new Paragraph({ children: [picture("signature.png", 160, 48)] })]),
              ],
            }),
            new TableRow({
              children: [
                cell([new Paragraph({ children: [run("Company stamp")] })]),
                cell([new Paragraph({ children: [picture("stamp.png", 80, 80)] })]),
              ],
            }),
          ],
        }),
        new Paragraph({ children: [] }),
        new Paragraph({
          bidirectional: true,
          children: [ar("تمت زيارة الموقع والتأكد من جاهزية منطقة التخزين الجديدة.")],
        }),
        new Paragraph({ children: [new PageBreak()] }),
        new Paragraph({ children: [run("Next steps", { size: 14, bold: true })] }),
        new Paragraph({ children: [run("The loading bays will be painted before the end of the month.")] }),
        new Paragraph({ children: [] }),
        new Paragraph({ children: [run("Signed:")] }),
        new Paragraph({ children: [picture("signature.png", 200, 60)] }),
        new Paragraph({ children: [] }),
        new Paragraph({ bidirectional: true, children: [ar("مع خالص الشكر لفريق العمل.")] }),
      ],
    },
  ],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(path.join(OUT, "images.docx"), buf);
  console.log("written", path.join(OUT, "images.docx"));
});
