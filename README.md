# PDF Editor

A free web tool to edit the text in a PDF: change, add or remove words while keeping the original font, size and color. Works on phones and laptops. Supports English, French and Arabic.

Licensed under the GNU AGPL v3 (see `LICENSE`). Anyone running this as a public website must offer its source code to users.

## Status

| Step | What | State |
|---|---|---|
| 1 | Upload, view pages, download | Done |
| 2 | Detect every text line with its font, size, color and position | Done |
| 3 | Edit text in place, matching the original style | Done |
| 4 | Delete text | Next (an empty edit already removes a line) |
| 5 | Add new text | |
| 6 | Undo, before/after view, several edits | |
| 7 | Public launch: limits, password PDFs, deploy | |

## Text detection

`backend/app/textlayout.py` rebuilds lines from the glyphs on the page. It doesn't use the PDF engine's own line grouping, which splits lines Word drew in pieces and scrambles Arabic. For every line it finds:
- the text in typing order, including where Arabic and English mix
- the direction
- the font, size, color and bold/italic, plus any style changes inside the line
- its box on the page

Word sometimes stores wrong characters in the PDF:
- **Kashida-justified Arabic:** stretch glyphs labelled "ز", spaces labelled "يم".
- **Some Arabic ligatures:** for example the Rial sign, labelled "ريا".

Each glyph is checked against the font's own character table and corrected where it can be. Lines that still can't be trusted are marked *uncertain* instead of shown as fact.

Files not made by Word are read the way the standard Unicode bidi rules lay text out (Chrome, for one, draws a phone number in an Arabic sentence with its groups reversed). For those, the reading chosen is the one that, laid out by those rules, matches the page exactly.

`backend/tests/test_textlayout.py` checks against an answer key, the made-up documents saved as PDF by Word and printed to PDF by Chrome. Every line must be typed text, and every paragraph must be found again in order. A consistency check also runs on any PDF. It confirms the display text lays out like the page, but it can't catch a wrong direction. So for private samples, a report (`samples/detection-report.txt`) lists every line whose direction was a guess.

## Editing

`backend/app/editing.py` changes one line at a time:

1. **Replay.** The page's drawing instructions are replayed (`pdfcontent.py`), and every glyph must land where MuPDF puts it. If not, the page uses something not modelled yet and the edit is refused.
2. **Keep unchanged words.** Words that didn't change keep their original glyphs. Changed words are shaped with the installed font (`fonts.py`, HarfBuzz).
3. **Choose the font.** The PDF's embedded copy of the font is reused only when it has every glyph needed, drawn identically, with correct copy/search labels. Otherwise the installed font's glyphs are embedded.
4. **Lay out.** The line keeps its alignment: left, right, centered, or justified (detected from the paragraph's other lines). Word documents follow Word's ordering of mixed Arabic and English. Other PDFs follow the Unicode bidi algorithm (`bidi.py`).
5. **Check before accepting.** The edit is refused, and nothing changes, unless:
   - the line reads back as typed;
   - a second reader (PDFium) sees the new letters;
   - nothing outside the line changes by a single pixel, in MuPDF and PDFium;
   - no other page changes.

Each edit is a new version, so Undo goes back.

Known limits:
- one line at a time, with no reflow into the next line;
- text too long for its line is refused;
- kashida-justified lines come back justified with spaces, with a warning;
- lines marked uncertain need their unreadable letters retyped;
- rotated text and text inside embedded objects can't be edited yet.

**Fonts on the server matter.** Word strips the Arabic shaping rules from the fonts it embeds, so new Arabic text needs the real font installed (`PDFEDIT_FONT_DIRS`). Without it, only letters already in the PDF can be reused.

### Edit accuracy

`backend/tests/test_edit_fidelity.py` measures edits independently of the editor, with MuPDF's own text extraction and pixels in MuPDF and PDFium. `backend/tests/test_editing.py` runs 20 realistic edits on the Word and Chrome test documents, and pins the ones that must be refused.

## How it works

- `backend/`: Python. FastAPI serves the API, and PyMuPDF renders pages and reads text. pikepdf, HarfBuzz and fontTools do the editing. Page images are rendered by the engine that reads the file, so the preview matches it.
- `frontend/`: React + Vite website. Shows the pages and the editing tools.

Uploaded files live in `backend/data/<random id>/` and are deleted an hour after they were last used.

## Run it locally (Windows PowerShell)

One-time setup:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
cd ..\frontend
npm install
```

Run (two terminals):

```powershell
# Terminal 1: the server
cd backend
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000

# Terminal 2: the website, with live reload
cd frontend
npm run dev
```

Then open http://localhost:5173. To try it on your phone, run `npm run dev -- --host` and open the "Network" address it prints, with the phone on the same Wi-Fi.

Production-style (one server): `npm run build` in `frontend`, then start the server only and open http://localhost:8000.

## Tests

```powershell
cd backend
.venv\Scripts\python -m pytest -q
```

Put real PDFs to test with in `samples/`. That folder is never committed.

### Test documents

`backend/tests/fixtures/word/` holds made-up business documents saved as PDF by Microsoft Word, the most common source of the PDFs people edit:

| Document | Tests |
|---|---|
| `arabic-quotation` | Arabic right-to-left text, Simplified/Traditional Arabic, Sakkal Majalla and Tahoma fonts, kashida justification, vowel marks, Arabic-Indic digits, an RTL table |
| `french-quote` | Every French accent, œ/æ, « guillemets », French non-breaking spaces, ligatures, superscript, a price table |
| `bilingual-contract` | English and Arabic side by side, Arabic sentences with English words, phone numbers and URLs, and the reverse |
| `english-styles` | Many fonts and sizes, style changes mid-sentence, links, shading, text box, small caps, two columns |

`expected.json` lists every paragraph's true text, font, size and color. `edits.json` lists realistic edits to try. `backend/tests/fixtures/chrome/` has the same paragraphs printed to PDF by Chrome, plus fully vowelled Arabic. To rebuild them (Windows with Microsoft Word and Chrome; close Word first):

```powershell
cd tools	est-documents
npm install
node make.js
powershell -ExecutionPolicy Bypass -File export.ps1
python print_chrome.py
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PDFEDIT_DATA_DIR` | `backend/data` | Where uploads are stored |
| `PDFEDIT_MAX_UPLOAD_MB` | `20` | Largest file accepted |
| `PDFEDIT_MAX_PAGES` | `50` | Most pages accepted |
| `PDFEDIT_DOC_TTL_MINUTES` | `60` | Delete files this long after last use |
| `VITE_APP_NAME` | `PDF Editor` | Name shown on the website. Set it in `frontend/.env.local` for a branded deployment |
| `VITE_SOURCE_URL` | this repository | Link to the source code shown on the website |
