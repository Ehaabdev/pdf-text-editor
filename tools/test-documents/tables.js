// Generates a Word document with merged table cells, for adding and deleting rows and
// columns: a tall first-column cell over five rows with a last row whose second and third
// cells are one; a full-height first column with three cells two rows tall; a header cell
// two columns wide; and tall cells with their text in the middle. All made up.
//   node tables.js          -> ../../backend/tests/fixtures/tables/tables.docx
//   then save it as PDF with Word -> tables-word.pdf next to it
//   python print_chrome.py tables -> tables-chrome.pdf (from tables.html)

const fs = require("fs");
const path = require("path");
const {
  AlignmentType, BorderStyle, Document, Packer, Paragraph, ShadingType, Table, TableCell, TableRow, TextRun, VerticalAlign, WidthType,
} = require("docx");

const OUT = path.resolve(__dirname, "../../backend/tests/fixtures/tables");
const font = (f) => ({ ascii: f, hAnsi: f, cs: f, eastAsia: f });
const run = (text, { font: f = "Calibri", size = 11, ...rest } = {}) =>
  new TextRun({ text, font: font(f), size: size * 2, sizeComplexScript: size * 2, ...rest });
const line = { style: BorderStyle.SINGLE, size: 6, color: "000000" };
const borders = { top: line, bottom: line, left: line, right: line };

function cell(text, width, o = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    borders,
    rowSpan: o.rows,
    columnSpan: o.cols,
    shading: o.fill ? { type: ShadingType.CLEAR, fill: o.fill, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    verticalAlign: o.middle ? VerticalAlign.CENTER : undefined,
    children: [new Paragraph({ alignment: o.align, children: text ? [run(text, { bold: o.bold })] : [] })],
  });
}

const para = (text, o = {}) => new Paragraph({ spacing: { before: 240, after: 120 }, children: [run(text, { size: 14, bold: true, color: "1F4E79", ...o })] });
const C = AlignmentType.CENTER;

// 1. A tall cell over five rows; the last row's second and third cells are one.
const schedule = new Table({
  width: { size: 9000, type: WidthType.DXA },
  columnWidths: [2400, 4200, 2400],
  rows: [
    new TableRow({ children: [cell("Phase one", 2400, { rows: 5, bold: true, fill: "DEEAF6" }), cell("Site survey", 4200), cell("2 days", 2400, { align: C })] }),
    new TableRow({ children: [cell("Lighting design", 4200), cell("5 days", 2400, { align: C })] }),
    new TableRow({ children: [cell("Cabling", 4200), cell("4 days", 2400, { align: C })] }),
    new TableRow({ children: [cell("Fitting the lights", 4200), cell("3 days", 2400, { align: C })] }),
    new TableRow({ children: [cell("Testing", 4200), cell("1 day", 2400, { align: C })] }),
    new TableRow({ children: [cell("", 2400), cell("Total: 15 working days", 6600, { cols: 2, align: C, bold: true })] }),
  ],
});

// 2. A full-height first column, and three cells two rows tall.
const quarters = new Table({
  width: { size: 9000, type: WidthType.DXA },
  columnWidths: [1800, 2400, 1600, 1600, 1600],
  rows: [
    new TableRow({ children: [
      cell("North", 1800, { rows: 3, bold: true }), cell("Orders", 2400),
      cell("120", 1600, { rows: 2, align: C }), cell("135", 1600, { rows: 2, align: C }), cell("150", 1600, { rows: 2, align: C }),
    ] }),
    new TableRow({ children: [cell("Repeat orders", 2400)] }),
    new TableRow({ children: [cell("Returns", 2400), cell("4", 1600, { align: C }), cell("6", 1600, { align: C }), cell("5", 1600, { align: C })] }),
  ],
});

// 3. A header cell two columns wide.
const contacts = new Table({
  width: { size: 9000, type: WidthType.DXA },
  columnWidths: [3000, 2400, 3600],
  rows: [
    new TableRow({ children: [cell("Name", 3000, { bold: true, fill: "E2EFDA" }), cell("Contact", 6000, { cols: 2, bold: true, align: C, fill: "E2EFDA" })] }),
    new TableRow({ children: [cell("Lina Haddad", 3000), cell("0555 010 203", 2400), cell("lina@example.com", 3600)] }),
    new TableRow({ children: [cell("Omar Saleh", 3000), cell("0555 010 204", 2400), cell("omar@example.com", 3600)] }),
  ],
});

// 4. Tall cells with their text in the middle: a shaded one over three rows, one over two.
const rooms = new Table({
  width: { size: 9000, type: WidthType.DXA },
  columnWidths: [2700, 4050, 2250],
  rows: [
    new TableRow({ children: [cell("Ground floor", 2700, { rows: 3, bold: true, middle: true, fill: "FBE5D6" }), cell("Lobby", 4050), cell("40 m²", 2250, { align: C })] }),
    new TableRow({ children: [cell("Office", 4050), cell("25 m²", 2250, { rows: 2, align: C, middle: true })] }),
    new TableRow({ children: [cell("Store", 4050)] }),
    new TableRow({ children: [cell("First floor", 2700, { bold: true }), cell("Meeting room", 4050), cell("30 m²", 2250, { align: C })] }),
  ],
});

const doc = new Document({
  sections: [
    {
      children: [
        para("Project schedule"),
        schedule,
        para("Orders by quarter"),
        quarters,
        para("Contacts"),
        contacts,
        para("Rooms"),
        rooms,
        new Paragraph({ spacing: { before: 240 }, children: [run("All names and numbers are made up.", { size: 9, color: "595959" })] }),
      ],
    },
  ],
});

fs.mkdirSync(OUT, { recursive: true });
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(path.join(OUT, "tables.docx"), buf);
  console.log("tables.docx");
});
