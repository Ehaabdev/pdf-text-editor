// Generates the Word test documents. All names, companies and numbers are made up.
//   node make.js            -> ../../backend/tests/fixtures/word/*.docx and expected.json
//   powershell export.ps1   -> the same documents saved as PDF by Microsoft Word
//
// expected.json records every paragraph's text in logical (typing) order, with its
// font, size and color, so text-detection tests know what each PDF should contain.

const fs = require("fs");
const path = require("path");
const {
  AlignmentType, BorderStyle, Document, ExternalHyperlink, Footer, FrameAnchorType, Header,
  HorizontalPositionAlign, LevelFormat, Packer, PageBreak, PageNumber, Paragraph, SectionType,
  ShadingType, Table, TableCell, TableRow, TextRun, VerticalPositionAlign, WidthType,
} = require("docx");

const OUT = path.resolve(__dirname, "../../backend/tests/fixtures/word");
const CONTENT_WIDTH = 11906 - 2 * 1440; // A4 width minus 1" margins, in twips

const AR = "؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿";
const ARABIC = new RegExp(`[${AR}]`);
// In an English paragraph only the Arabic words themselves are right-to-left.
const ARABIC_WORDS = new RegExp(`[${AR}]+(?:[ ـ]+[${AR}]+)*`, "g");
// In an Arabic paragraph, dates, phone numbers and Latin words (emails, URLs, codes)
// are left-to-right, the way Word types them.
const DATE = String.raw`\d{1,4}/\d{1,2}/\d{1,4}|[٠-٩]{1,4}/[٠-٩]{1,2}/[٠-٩]{1,4}`;
const PHONE = String.raw`\+?\d[\d ]{6,}\d`;
// A Latin chunk ends on a letter or digit: a full stop after it is typed in Arabic mode.
const WORD_LTR = String.raw`[A-Za-z](?:[A-Za-z0-9@._\-/:+&]*[A-Za-z0-9])?`;
const LATIN = `${WORD_LTR}(?: ${WORD_LTR})*`;
const LTR_CHUNK = new RegExp(`${DATE}|${PHONE}|${LATIN}`, "g");
const NUMERIC_CHUNK = new RegExp(`^(?:${DATE}|${PHONE})$`);
// Word otherwise shows "21/10/2026" as "2026/10/21" and reverses the groups of a phone
// number inside Arabic text. Invisible left-to-right marks keep them as typed.
const LRM = "‎";
const keepOrder = (t) => (NUMERIC_CHUNK.test(t) ? t.replace(/([/ ])/g, `${LRM}$1${LRM}`) : t);

let expected = [];

function runProps(s) {
  const f = s.font || "Arial";
  return {
    font: { ascii: f, hAnsi: f, cs: f, eastAsia: f },
    size: Math.round((s.size || 11) * 2),
    sizeComplexScript: Math.round((s.size || 11) * 2),
    bold: s.bold,
    boldComplexScript: s.bold,
    italics: s.italic,
    italicsComplexScript: s.italic,
    color: s.color,
    underline: s.underline ? {} : undefined,
    superScript: s.sup,
    subScript: s.sub,
    smallCaps: s.smallCaps,
    allCaps: s.allCaps,
    characterSpacing: s.spacing,
  };
}

/** Runs for one piece of text in a given style, split by writing direction like Word does. */
function runs(text, s = {}, paragraphRtl = false) {
  const lang = s.lang || "en-US";
  if (!ARABIC.test(text) && !paragraphRtl) {
    return [new TextRun({ text, ...runProps(s), language: { value: lang } })];
  }
  const out = [];
  let last = 0;
  const push = (t, rtl) => {
    if (!t) return;
    out.push(new TextRun({ text: rtl ? t : keepOrder(t), ...runProps(s), rightToLeft: rtl, language: { value: lang, bidirectional: "ar-SA" } }));
  };
  // Arabic paragraph: everything is RTL except the LTR chunks. English paragraph: the reverse.
  const pattern = paragraphRtl ? LTR_CHUNK : ARABIC_WORDS;
  for (const m of text.matchAll(pattern)) {
    push(text.slice(last, m.index), paragraphRtl);
    push(m[0], !paragraphRtl);
    last = m.index + m[0].length;
  }
  push(text.slice(last), paragraphRtl);
  return out;
}

/**
 * A paragraph from parts: strings, or [text, style] pairs for mixed styling.
 * opts: { rtl, align, style (default style for every part), list, shading, frame, border, spacingAfter, heading }
 */
function para(doc, parts, opts = {}) {
  const base = opts.style || {};
  const list = Array.isArray(parts) ? parts : [parts];
  const children = [];
  const recorded = [];
  for (const part of list) {
    if (part && part.hyperlink) {
      const s = { ...base, color: "0563C1", underline: true, ...(part.style || {}) };
      children.push(new ExternalHyperlink({ link: part.hyperlink, children: runs(part.text, s, opts.rtl) }));
      recorded.push({ text: part.text, ...s, link: part.hyperlink });
      continue;
    }
    const [text, style] = typeof part === "string" ? [part, {}] : part;
    const s = { ...base, ...style };
    children.push(...runs(text, s, opts.rtl));
    recorded.push({ text, ...s });
  }
  expected.push({
    doc,
    text: recorded.map((r) => r.text).join(""),
    rtl: !!opts.rtl,
    align: opts.align || "start",
    runs: recorded.map(({ text, font, size, color, bold, italic, link, smallCaps, allCaps, spacing }) => ({
      text, font: font || "Arial", size: size || 11, color: color || "000000", bold: !!bold, italic: !!italic, link,
      // Drawn differently from how it was typed: capitals, or letters spaced apart.
      changesLetters: !!(smallCaps || allCaps || spacing),
    })),
  });
  return new Paragraph({
    children,
    bidirectional: opts.rtl,
    alignment: opts.align,
    numbering: opts.list ? { reference: opts.list, level: 0 } : undefined,
    shading: opts.shading ? { type: ShadingType.CLEAR, fill: opts.shading, color: "auto" } : undefined,
    frame: opts.frame,
    border: opts.border,
    spacing: { after: opts.spacingAfter ?? 120, line: opts.line },
  });
}

const pageBreak = () => new Paragraph({ children: [new PageBreak()] });

function cell(content, width, opts = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: opts.fill ? { type: ShadingType.CLEAR, fill: opts.fill, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: Array.isArray(content) ? content : [content],
  });
}

function table(doc, rows, widths, opts = {}) {
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    visuallyRightToLeft: opts.rtl,
    rows: rows.map(
      (row, r) =>
        new TableRow({
          tableHeader: r === 0 && opts.header,
          children: row.map((text, c) =>
            cell(
              para(doc, [[text, { ...opts.style, bold: (r === 0 && opts.header) || (opts.boldLast && r === rows.length - 1) }]], {
                rtl: opts.rtl,
                align: c === 0 ? undefined : AlignmentType.CENTER,
                spacingAfter: 0,
              }),
              widths[c],
              { fill: r === 0 && opts.header ? opts.headerFill : undefined },
            ),
          ),
        }),
    ),
  });
}

const numbering = {
  config: [
    {
      reference: "bullets",
      levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } } }],
    },
    {
      reference: "numbers",
      levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } } }],
    },
  ],
};

function pageFooter(parts, rtl, style) {
  return new Footer({
    children: [
      new Paragraph({
        bidirectional: rtl,
        alignment: AlignmentType.CENTER,
        children: parts.map((p) =>
          typeof p === "string" && ARABIC.test(p)
            ? new TextRun({ text: p, ...runProps(style), rightToLeft: true })
            : new TextRun({ children: [p], ...runProps(style), rightToLeft: rtl }),
        ),
      }),
    ],
  });
}

function pageHeader(doc, text, rtl, style) {
  return new Header({ children: [para(doc, [[text, style]], { rtl, align: AlignmentType.CENTER, spacingAfter: 0 })] });
}

// 1. Arabic quotation ------------------------------------------------------------------

function arabicQuotation() {
  const D = "arabic-quotation";
  const body = { font: "Arial", size: 12 };
  const heading = { font: "Arial", size: 14, bold: true, color: "1F4E79" };
  const rtl = true;
  const R = AlignmentType.RIGHT;
  return new Document({
    numbering,
    sections: [
      {
        headers: { default: pageHeader(D, "شركة الأفق الرقمي لتقنية المعلومات - Digital Horizon IT", rtl, { font: "Arial", size: 9, color: "595959" }) },
        footers: { default: pageFooter(["صفحة ", PageNumber.CURRENT, " من ", PageNumber.TOTAL_PAGES], rtl, { font: "Arial", size: 9 }) },
        children: [
          para(D, [["عرض سعر خدمات الرسائل والمحادثات", { font: "Simplified Arabic", size: 20, bold: true, color: "1F4E79" }]], { rtl, align: AlignmentType.CENTER, spacingAfter: 240 }),
          para(D, "رقم العرض: Q-2026-0417", { rtl, style: body, spacingAfter: 0 }),
          para(D, "التاريخ: 21/10/2026", { rtl, style: body, spacingAfter: 0 }),
          para(D, "صالح حتى: ٢٠/١١/٢٠٢٦", { rtl, style: body, spacingAfter: 0 }),
          para(D, "إلى: مؤسسة الريادة التجارية", { rtl, style: body, spacingAfter: 0 }),
          para(D, "البريد الإلكتروني: sales@example.com", { rtl, style: body, spacingAfter: 0 }),
          para(D, "الهاتف: +966 55 123 4567", { rtl, style: body, spacingAfter: 0 }),
          para(D, "الموقع: www.example.com", { rtl, style: body, spacingAfter: 240 }),
          para(D, [["مقدمة", heading]], { rtl }),
          para(
            D,
            "يسعدنا في شركة الأفق الرقمي أن نقدم لكم هذا العرض لتقديم خدمات المحادثات عبر WhatsApp Business وخدمات الرسائل النصية القصيرة SMS، وذلك وفقاً للمتطلبات التي تم الاتفاق عليها خلال اجتماعنا الأخير. يتضمن العرض جميع الخدمات الفنية والدعم اللازم لضمان تشغيل الحلول بكفاءة عالية واستمرارية دون انقطاع.",
            { rtl, style: body, align: AlignmentType.BOTH },
          ),
          para(
            D,
            "نلتزم بتقديم خدمة متميزة تقوم على الشفافية والدقة والالتزام بالمواعيد، ونحرص على أن يحصل عملاؤنا على أفضل قيمة مقابل استثمارهم، مع فريق دعم متخصص متاح طوال أيام الأسبوع للإجابة على جميع الاستفسارات ومعالجة الطلبات في أسرع وقت ممكن.",
            { rtl, style: { font: "Traditional Arabic", size: 14 }, align: AlignmentType.MEDIUM_KASHIDA },
          ),
          para(D, "قَالَ الحُكَمَاءُ: الإِتْقَانُ فِي العَمَلِ طَرِيقُ النَّجَاحِ.", { rtl, style: { font: "Sakkal Majalla", size: 14 }, align: AlignmentType.CENTER }),
          para(D, [["تفاصيل الأسعار", heading]], { rtl }),
          table(
            D,
            [
              ["الخدمة", "الكمية", "سعر الوحدة (ريال)", "الإجمالي (ريال)"],
              ["رسائل تسويقية عبر WhatsApp", "10,000", "0.17", "1,700.00"],
              ["رسائل الخدمة", "5,000", "0.04", "200.00"],
              ["رسائل التحقق OTP", "20,000", "0.08", "1,600.00"],
              ["لوحة تحكم الموظفين (Agent Console)", "3", "150.00", "450.00"],
              ["الإجمالي قبل الضريبة", "", "", "3,950.00"],
            ],
            [3626, 1600, 1900, 1900],
            { rtl, header: true, headerFill: "DEEAF6", boldLast: true, style: { font: "Tahoma", size: 11 } },
          ),
          para(D, [["ملاحظة: لا تشمل الأسعار المذكورة أعلاه ضريبة القيمة المضافة بنسبة 15%.", { color: "C00000" }]], { rtl, style: body }),
          para(D, [["الإجمالي شامل الضريبة: 4,542.50 ريال", { bold: true, color: "00B050" }]], { rtl, style: body, align: R }),
          pageBreak(),
          para(D, [["نطاق العمل", heading]], { rtl }),
          para(D, "تفعيل حساب WhatsApp Business وربطه بالأنظمة الحالية.", { rtl, style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "إعداد قوالب الرسائل واعتمادها من Meta.", { rtl, style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "تدريب فريق العمل على استخدام لوحة التحكم.", { rtl, style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "تقديم تقارير شهرية عن أداء الحملات.", { rtl, style: body, list: "bullets" }),
          para(D, [["مراحل التنفيذ", heading]], { rtl }),
          para(D, "توقيع العقد واستلام الدفعة الأولى.", { rtl, style: body, list: "numbers", spacingAfter: 60 }),
          para(D, "إعداد البيئة الفنية خلال ٥ أيام عمل.", { rtl, style: body, list: "numbers", spacingAfter: 60 }),
          para(D, "الاختبار والتشغيل التجريبي.", { rtl, style: body, list: "numbers", spacingAfter: 60 }),
          para(D, "الإطلاق الرسمي والمتابعة.", { rtl, style: body, list: "numbers" }),
          para(D, [["الشروط والأحكام", heading]], { rtl }),
          para(
            D,
            ["مدة صلاحية هذا العرض ", ["ثلاثون (30) يوماً", { underline: true }], " من تاريخ إصداره. تُدفع قيمة العرض على دفعتين: ٥٠٪ عند توقيع العقد و٥٠٪ عند الإطلاق، ولا يحق لأي طرف إلغاء الاتفاق دون إشعار كتابي مسبق."],
            { rtl, style: body, align: AlignmentType.BOTH },
          ),
          para(D, "للاستفسار: أحمد المنصور - مدير المبيعات - ahmed.mansour@example.com", { rtl, style: body }),
        ],
      },
    ],
  });
}

// 2. French quote ----------------------------------------------------------------------

function frenchQuote() {
  const D = "french-quote";
  const fr = { lang: "fr-FR" };
  const body = { ...fr, font: "Calibri", size: 11 };
  const heading = { ...fr, font: "Cambria", size: 14, bold: true, color: "1F4E79" };
  const n = " "; // espace insécable, before ":" and inside « »
  const t = " "; // espace fine insécable, before ; ! ? and in numbers
  return new Document({
    numbering,
    sections: [
      {
        headers: { default: pageHeader(D, "Atelier Lumière SARL - 12, rue de la République, 69002 Lyon", false, { ...fr, font: "Calibri", size: 9, color: "595959" }) },
        footers: { default: pageFooter(["Page ", PageNumber.CURRENT, " sur ", PageNumber.TOTAL_PAGES], false, { font: "Calibri", size: 9 }) },
        children: [
          para(D, `Lyon, le 21${n}octobre 2026`, { style: body, align: AlignmentType.RIGHT }),
          para(D, [[`Devis n°${n}2026-047`, { ...fr, font: "Cambria", size: 20, bold: true, color: "1F4E79" }]], { align: AlignmentType.CENTER, spacingAfter: 240 }),
          para(D, "Mme Hélène Dupré", { style: body, spacingAfter: 0 }),
          para(D, "Société Bérénice & Fils", { style: body, spacingAfter: 0 }),
          para(D, "8, avenue des Châteaux, 94240 L’Haÿ-les-Roses", { style: body, spacingAfter: 240 }),
          para(D, [[`Objet${n}:`, { bold: true }], " rénovation de l’éclairage de vos bureaux"], { style: body }),
          para(
            D,
            `Madame, suite à notre rencontre du 14${n}octobre, nous avons le plaisir de vous adresser notre proposition pour la rénovation complète de l’éclairage de vos bureaux. Notre équipe s’engage à réaliser des travaux efficaces, durables et économes en énergie, tout en respectant le calendrier défini ensemble.`,
            { style: body, align: AlignmentType.BOTH },
          ),
          para(
            D,
            `«${n}Une lumière bien pensée change l’atmosphère d’un lieu${n}», affirme notre fondatrice. C’est pourquoi chaque projet commence par une étude précise de vos besoins${t}!`,
            { style: { ...fr, font: "Times New Roman", size: 12, italic: true } },
          ),
          para(
            D,
            `Les fleurs de l’affiche, l’œuvre d’art et le cœur du hall d’entrée seront mis en valeur grâce à un éclairage d’ambiance. Où${t}? Dans l’accueil, la salle de réunion et le bureau de la direction.`,
            { style: { ...fr, font: "Garamond", size: 12 }, align: AlignmentType.BOTH },
          ),
          para(D, [[`ÉCLAIRAGE DE LA FAÇADE, CÔTÉ RUE`, heading]]),
          para(
            D,
            [`La façade sera éclairée par des projecteurs basse consommation${t}; leur coût d’entretien est très faible. `, ["TRÈS IMPORTANT", { bold: true, underline: true }], `${n}: les fenêtres du rez-de-chaussée et du 1`, ["er", { sup: true }], ` étage resteront accessibles pendant les travaux. Une ambiance naïve et chaleureuse, digne d’un maître artisan, ex æquo avec les plus belles réalisations de la région.`],
            { style: body, align: AlignmentType.BOTH },
          ),
          para(D, [["Détail des prestations", heading]]),
          table(
            D,
            [
              ["Désignation", "Quantité", "Prix unitaire HT", "Total HT"],
              ["Luminaires LED encastrés", "24", `89,90${n}€`, `2${t}157,60${n}€`],
              ["Câblage et raccordement", "1", `1${t}450,00${n}€`, `1${t}450,00${n}€`],
              ["Main-d’œuvre (forfait)", `3${n}j`, `520,00${n}€`, `1${t}560,00${n}€`],
              ["Total HT", "", "", `5${t}167,60${n}€`],
            ],
            [3826, 1400, 1900, 1900],
            { header: true, headerFill: "DEEAF6", boldLast: true, style: { ...fr, font: "Calibri", size: 10.5 } },
          ),
          para(D, `TVA (20${t}%)${n}: 1${t}033,52${n}€`, { style: body, align: AlignmentType.RIGHT, spacingAfter: 0 }),
          para(D, [[`Total TTC${n}: 6${t}201,12${n}€`, { bold: true, color: "C00000" }]], { style: body, align: AlignmentType.RIGHT }),
          pageBreak(),
          para(D, [[`Conditions`, heading]]),
          para(D, `Validité du devis${n}: 30${n}jours`, { style: body, list: "bullets", spacingAfter: 60 }),
          para(D, `Acompte de 30${t}% à la commande`, { style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "Garantie de 2 ans sur les luminaires", { style: body, list: "bullets", spacingAfter: 60 }),
          para(D, `Délai d’exécution${n}: 3${n}semaines, sauf fêtes de Noël`, { style: body, list: "bullets" }),
          para(
            D,
            [`À noter${n}: les prix sont exprimés en euros (€) hors taxes. Pour toute question, n’hésitez pas à nous contacter au 04${n}72${n}00${n}00${n}00 ou par courriel à `, { hyperlink: "mailto:contact@example.com", text: "contact@example.com" }, "."],
            { style: body, align: AlignmentType.BOTH },
          ),
          para(D, "Veuillez agréer, Madame, l’expression de nos salutations distinguées.", { style: body, spacingAfter: 480 }),
          para(D, [["Jean-François Lefèvre", { bold: true }]], { style: body, align: AlignmentType.RIGHT, spacingAfter: 0 }),
          para(D, [["Gérant", { italic: true }]], { style: body, align: AlignmentType.RIGHT }),
        ],
      },
    ],
  });
}

// 3. Bilingual contract ----------------------------------------------------------------

function bilingualContract() {
  const D = "bilingual-contract";
  const en = { font: "Calibri", size: 11 };
  const ar = { font: "Arial", size: 12 };
  const half = CONTENT_WIDTH / 2;
  const row = (english, arabic) =>
    new TableRow({
      children: [
        cell(para(D, english, { style: en, align: AlignmentType.BOTH, spacingAfter: 0 }), half),
        cell(para(D, arabic, { rtl: true, style: ar, align: AlignmentType.BOTH, spacingAfter: 0 }), half),
      ],
    });
  const heading = (english, arabic) => [
    para(D, [[english, { font: "Calibri", size: 14, bold: true, color: "1F4E79" }]]),
    para(D, [[arabic, { font: "Simplified Arabic", size: 14, bold: true, color: "1F4E79" }]], { rtl: true }),
  ];
  return new Document({
    sections: [
      {
        footers: { default: pageFooter(["Page ", PageNumber.CURRENT, " / ", PageNumber.TOTAL_PAGES], false, { font: "Calibri", size: 9 }) },
        children: [
          para(D, [["Service Agreement", { font: "Calibri", size: 20, bold: true }]], { align: AlignmentType.CENTER, spacingAfter: 0 }),
          para(D, [["اتفاقية تقديم خدمات", { font: "Simplified Arabic", size: 20, bold: true }]], { rtl: true, align: AlignmentType.CENTER, spacingAfter: 240 }),
          new Table({
            width: { size: CONTENT_WIDTH, type: WidthType.DXA },
            columnWidths: [half, half],
            rows: [
              row(
                "This Agreement is made on 21 October 2026 between Elite Services Co. (the “Provider”) and Al Noor Trading Est. (the “Client”).",
                "أُبرمت هذه الاتفاقية بتاريخ 21 أكتوبر 2026 بين شركة النخبة للخدمات (\"مقدم الخدمة\") ومؤسسة النور التجارية (\"العميل\").",
              ),
              row("1. Scope: The Provider shall deliver customer messaging services as described in Annex A.", "١. النطاق: يلتزم مقدم الخدمة بتقديم خدمات المراسلة للعملاء كما هو موضح في الملحق (أ)."),
              row("2. Term: Twelve (12) months starting on 1 November 2026.", "٢. المدة: اثنا عشر (12) شهراً تبدأ من 1 نوفمبر 2026."),
              row("3. Fees: SAR 4,542.50 per month, including 15% VAT.", "٣. الرسوم: 4,542.50 ريال شهرياً شاملة ضريبة القيمة المضافة 15%."),
              row("4. Notices: All notices shall be sent to legal@example.com.", "٤. الإشعارات: تُرسل جميع الإشعارات إلى legal@example.com."),
              row("5. Governing law: This Agreement is governed by the laws of the Kingdom of Saudi Arabia.", "٥. القانون الواجب التطبيق: تخضع هذه الاتفاقية لأنظمة المملكة العربية السعودية."),
            ],
          }),
          pageBreak(),
          ...heading("Contact details", "بيانات التواصل"),
          para(D, "للتواصل مع فريق الدعم (Support Team) يرجى الاتصال على +966 11 234 5678 أو زيارة https://support.example.com/help خلال أوقات العمل من 9:00 إلى 17:00.", { rtl: true, style: ar, align: AlignmentType.BOTH }),
          para(D, "تم تحديث الأسعار بتاريخ 2026/10/21 مع خصم -10% على باقة Premium Plus (الإصدار 3.2).", { rtl: true, style: ar }),
          para(D, "المنتج: WA-2026 / الكود: SMS-500 / المرجع: [REF-77]", { rtl: true, style: { font: "Simplified Arabic", size: 13 } }),
          para(D, "The Client’s representative, Mr. خالد العتيبي (Khalid Al-Otaibi), will sign on behalf of مؤسسة النور التجارية.", { style: en }),
          para(D, "Meeting location: Riyadh office, برج المملكة, Floor 12.", { style: en }),
          ...heading("Signatures", "التوقيعات"),
          new Table({
            width: { size: CONTENT_WIDTH, type: WidthType.DXA },
            columnWidths: [half, half],
            rows: [
              new TableRow({
                children: [
                  cell([para(D, [["For the Provider / عن مقدم الخدمة", { bold: true }]], { style: en }), para(D, "Name / الاسم: Saad Al-Harbi", { style: en }), para(D, "Date / التاريخ: 21/10/2026", { style: en })], half),
                  cell([para(D, [["For the Client / عن العميل", { bold: true }]], { style: en }), para(D, "Name / الاسم: Khalid Al-Otaibi", { style: en }), para(D, "Date / التاريخ: 21/10/2026", { style: en })], half),
                ],
              }),
            ],
          }),
        ],
      },
    ],
  });
}

// 4. English styles --------------------------------------------------------------------

function englishStyles() {
  const D = "english-styles";
  const body = { font: "Calibri", size: 11 };
  const w = [2626, 2133, 2133, 2134];
  return new Document({
    numbering,
    sections: [
      {
        headers: { default: pageHeader(D, "Northwind Logistics - Confidential", false, { font: "Arial", size: 8, color: "7F7F7F" }) },
        footers: { default: pageFooter(["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES], false, { font: "Arial", size: 8 }) },
        children: [
          para(D, [["Quarterly Service Report", { font: "Georgia", size: 28, bold: true, color: "1F3864" }]], { spacingAfter: 60 }),
          para(D, [["Prepared for Northwind Logistics, Q3 2026", { font: "Segoe UI", size: 14, color: "7F7F7F" }]], { spacingAfter: 240 }),
          para(D, [["Executive summary", { font: "Calibri", size: 13, bold: true, smallCaps: true }]]),
          para(
            D,
            ["This report summarises the performance of the messaging platform between July and September 2026. Overall delivery rates ", ["improved", { bold: true }], ", and response times fell for the ", ["third", { color: "C00000" }], " quarter in a row."],
            { style: body },
          ),
          para(
            D,
            "Customers sent more messages than in any previous quarter, with the largest growth in order updates and delivery notifications. The support team handled the extra volume without adding staff, mainly because the new routing rules sent simple questions to automated replies and passed complex cases straight to the right agent.",
            { style: { font: "Times New Roman", size: 12 }, align: AlignmentType.BOTH },
          ),
          para(D, "Delivery rate: 98.7% | Average response: 42 seconds", { style: { font: "Verdana", size: 10 }, align: AlignmentType.CENTER }),
          para(D, "Report ID: NW-Q3-2026-118", { style: { font: "Arial", size: 9 }, align: AlignmentType.RIGHT }),
          para(
            D,
            ["Full statistics are available at ", { hyperlink: "https://dashboard.example.com/q3", text: "https://dashboard.example.com/q3" }, " or by email to ", { hyperlink: "mailto:reports@example.com", text: "reports@example.com" }, "."],
            { style: body },
          ),
          para(D, ["Carbon savings this quarter: 1.2 t CO", ["2", { sub: true }], ", ranking us 3", ["rd", { sup: true }], " in the regional green index."], { style: body }),
          para(D, [["important notice", { allCaps: true, bold: true }], ": the September figures are provisional and may change after the final audit."], { style: body, shading: "FFF2CC" }),
          para(D, [["C o n f i d e n t i a l", { spacing: 0 }], " / ", ["Confidential", { spacing: 60 }]], { style: { font: "Segoe UI", size: 10, color: "7F7F7F" } }),
          table(
            D,
            [
              ["Metric", "July", "August", "September"],
              ["Messages sent", "120,400", "131,900", "140,250"],
              ["Delivery rate", "98.1%", "98.5%", "98.7%"],
              ["Average response", "55 s", "47 s", "42 s"],
            ],
            w,
            { header: true, headerFill: "D9E2F3", style: { font: "Calibri", size: 10 } },
          ),
          para(D, [["Figures are rounded to one decimal place.", { size: 8, italic: true, color: "595959" }]], { style: body }),
          para(D, [["Highlight: response times improved by 24% this quarter.", { bold: true, color: "1F3864" }]], {
            style: { font: "Segoe UI", size: 10 },
            frame: {
              type: "alignment",
              width: 3600,
              height: 1000,
              alignment: { x: HorizontalPositionAlign.RIGHT, y: VerticalPositionAlign.BOTTOM },
              anchor: { horizontal: FrameAnchorType.MARGIN, vertical: FrameAnchorType.MARGIN },
            },
            border: Object.fromEntries(["top", "bottom", "left", "right"].map((s) => [s, { style: BorderStyle.SINGLE, size: 8, color: "1F3864", space: 4 }])),
            shading: "DEEAF6",
          }),
        ],
      },
      {
        properties: { type: SectionType.NEXT_PAGE, column: { count: 2, space: 708 } },
        children: [
          para(D, [["Next steps", { font: "Georgia", size: 16, bold: true, color: "1F3864" }]]),
          para(D, "Extend automated replies to returns and refunds.", { style: body, list: "numbers", spacingAfter: 60 }),
          para(D, "Add Arabic and French message templates.", { style: body, list: "numbers", spacingAfter: 60 }),
          para(D, "Review agent schedules before the holiday peak.", { style: body, list: "numbers" }),
          para(
            D,
            "The holiday season usually doubles the number of delivery questions. To prepare, the team will publish clearer tracking pages, send proactive delay notices, and keep two extra agents on call during the busiest weeks.",
            { style: { font: "Segoe UI", size: 10 }, align: AlignmentType.BOTH },
          ),
          para(D, "Risks", { style: { font: "Calibri", size: 12, bold: true } }),
          para(D, "Carrier delays outside our control.", { style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "Higher message costs from January.", { style: body, list: "bullets", spacingAfter: 60 }),
          para(D, "Staff holidays overlapping with peak demand.", { style: body, list: "bullets" }),
          para(
            D,
            "Each risk has an owner and a monthly review. Where a risk becomes likely, the owner prepares a short plan with options, costs and a recommended choice, so decisions can be made quickly.",
            { style: { font: "Segoe UI", size: 10 }, align: AlignmentType.BOTH },
          ),
          para(D, [["Prepared by the Customer Experience team", { italic: true, color: "595959" }]], { style: { font: "Georgia", size: 10 } }),
        ],
      },
    ],
  });
}

// 5. Turkish quotation -------------------------------------------------------------------
// Letters Word's simple fonts (Windows-1252) have (ç ö ü) and letters they haven't
// (ğ ı İ ş), often in one word: Müşteri, Görüşme, İşçilik, Çağdaş. Turkish amounts
// (1.250,00 ₺, %20) in a ruled table.

function turkishQuote() {
  const D = "turkish-quote";
  const body = { font: "Calibri", size: 11 };
  const heading = { font: "Cambria", size: 14, bold: true, color: "1F4E79" };
  const bullet = { style: body, list: "bullets", spacingAfter: 60 };
  return new Document({
    numbering,
    sections: [
      {
        headers: { default: pageHeader(D, "Yıldız Işık Mühendislik Ltd. Şti. - Atatürk Cad. No: 45, Şişli / İstanbul", false, { font: "Calibri", size: 9, color: "595959" }) },
        footers: { default: pageFooter(["Sayfa ", PageNumber.CURRENT, " / ", PageNumber.TOTAL_PAGES], false, { font: "Calibri", size: 9 }) },
        children: [
          para(D, "İstanbul, 21 Ekim 2026", { style: body, align: AlignmentType.RIGHT }),
          para(D, [["FİYAT TEKLİFİ No: 2026-118", { font: "Cambria", size: 20, bold: true, color: "1F4E79" }]], { align: AlignmentType.CENTER, spacingAfter: 240 }),
          para(D, "Sayın Şükrü Öztürk", { style: body, spacingAfter: 0 }),
          para(D, "Çağdaş Gıda Sanayi ve Ticaret A.Ş.", { style: body, spacingAfter: 0 }),
          para(D, "Bağdat Caddesi No: 212, Kadıköy / İstanbul", { style: body, spacingAfter: 240 }),
          para(D, [["Konu:", { bold: true }], " ofis aydınlatma sisteminin yenilenmesi"], { style: body }),
          para(
            D,
            "Sayın Öztürk, 14 Ekim tarihli görüşmemize istinaden, ofisinizin aydınlatma sisteminin yenilenmesi için hazırladığımız teklifimizi bilginize sunarız. Ekibimiz, işin tamamını belirlenen takvime uygun olarak, yüksek kalite ve düşük enerji tüketimi ilkeleriyle gerçekleştirmeyi taahhüt eder.",
            { style: body, align: AlignmentType.BOTH },
          ),
          para(
            D,
            "“Doğru ışık, bir mekânın havasını değiştirir” diyor kurucumuz. Bu yüzden her projeye ihtiyaçlarınızın ayrıntılı bir incelemesiyle başlıyoruz!",
            { style: { font: "Times New Roman", size: 12, italic: true } },
          ),
          para(
            D,
            "Müşteri memnuniyeti, iş güvenliği ve çevreye saygı çalışmalarımızın temelidir. Görüşme sırasında belirttiğiniz ölçüler ışığında toplantı salonu, açık ofis ve yönetim katı için ayrı çözümler öneriyoruz.",
            { style: { font: "Arial", size: 11 }, align: AlignmentType.BOTH },
          ),
          para(D, [["IŞIKLANDIRMA VE GÜVENLİK ÇALIŞMALARI", heading]]),
          para(
            D,
            ["Dış cephe, düşük tüketimli projektörlerle aydınlatılacaktır; bakım maliyeti oldukça düşüktür. ", ["ÇOK ÖNEMLİ", { bold: true, underline: true }], ": zemin kat ve 1. kattaki çıkışlar, toplam 450 m² alanda çalışmalar boyunca açık kalacaktır."],
            { style: body, align: AlignmentType.BOTH },
          ),
          para(D, [["Hizmet Ayrıntıları", heading]]),
          table(
            D,
            [
              ["Ürün / Hizmet", "Miktar", "Birim Fiyat", "Tutar"],
              ["LED panel armatür (60×60)", "24", "1.250,00 ₺", "30.000,00 ₺"],
              ["Kablolama ve bağlantı işleri", "1", "8.500,00 ₺", "8.500,00 ₺"],
              ["İşçilik (götürü)", "3 gün", "4.200,00 ₺", "12.600,00 ₺"],
              ["Toplam", "", "", "51.100,00 ₺"],
            ],
            [3826, 1400, 1900, 1900],
            { header: true, headerFill: "DEEAF6", boldLast: true, style: { font: "Calibri", size: 10.5 } },
          ),
          para(D, "KDV (%20): 10.220,00 TL", { style: body, align: AlignmentType.RIGHT, spacingAfter: 0 }),
          para(D, [["Genel Toplam: 61.320,00 TL", { bold: true, color: "C00000" }]], { style: body, align: AlignmentType.RIGHT }),
          pageBreak(),
          para(D, [["Koşullar", heading]]),
          para(D, "Teklif geçerlilik süresi: 30 gün", bullet),
          para(D, "Ödeme: %50 peşin, %50 iş bitiminde", bullet),
          para(D, "Armatürlerde 2 yıl garanti", bullet),
          para(D, "İş süresi: 3 hafta (resmî tatiller hariç)", { style: body, list: "bullets" }),
          para(
            D,
            ["Not: fiyatlara KDV dahil değildir. Sorularınız için bize 0212 555 01 23 numaralı telefondan veya ", { hyperlink: "mailto:bilgi@example.com", text: "bilgi@example.com" }, " adresinden ulaşabilirsiniz."],
            { style: body, align: AlignmentType.BOTH },
          ),
          para(D, "Şirketimiz 2009 yılından beri kamu ve özel sektör projelerinde çalışmaktadır.", { style: { font: "Segoe UI", size: 10, color: "404040" } }),
          para(D, "Saygılarımızla,", { style: body, spacingAfter: 480 }),
          para(D, [["Mehmet Çelik", { bold: true }]], { style: body, align: AlignmentType.RIGHT, spacingAfter: 0 }),
          para(D, [["Genel Müdür", { italic: true }]], { style: body, align: AlignmentType.RIGHT }),
        ],
      },
    ],
  });
}

const DOCUMENTS = [
  ["arabic-quotation", arabicQuotation],
  ["french-quote", frenchQuote],
  ["bilingual-contract", bilingualContract],
  ["english-styles", englishStyles],
  ["turkish-quote", turkishQuote],
];

// node make.js                 every document
// node make.js turkish-quote   just that one: the others' files and answers stay as they are
async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const only = process.argv.slice(2);
  const answers = path.join(OUT, "expected.json");
  const all = only.length ? JSON.parse(fs.readFileSync(answers, "utf8")).filter((p) => !only.includes(p.doc)) : [];
  for (const [name, build] of DOCUMENTS) {
    if (only.length && !only.includes(name)) continue;
    expected = [];
    const doc = build();
    fs.writeFileSync(path.join(OUT, `${name}.docx`), await Packer.toBuffer(doc));
    all.push(...expected);
    console.log(`${name}.docx: ${expected.length} paragraphs`);
  }
  fs.writeFileSync(answers, JSON.stringify(all, null, 1) + "\n", "utf8");
}


main();
