# PDF Editor

A free web tool to edit the text in a PDF: change, add or remove words while keeping the original font, size and color. Works on phones and laptops. Supports English, French and Arabic.

Licensed under the GNU AGPL v3 (see `LICENSE`). Anyone running this as a public website must offer its source code to users.

## Status

| Step | What | State |
|---|---|---|
| 1 | Upload, view pages, download | Done |
| 2 | Detect every text line with its font, size, color and position | Done |
| 3 | Edit text in place, matching the original style | Next |
| 4 | Delete text | |
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

`backend/tests/test_textlayout.py` checks this two ways:
- against the answer key of the made-up Word documents
- for any PDF, including private samples: laid out with the standard bidi rules a browser uses, the extracted text must come out in the same order as on the page

## Edit accuracy

`backend/tests/test_edit_fidelity.py` checks that an edit is invisible. Every other line must keep its text. The edited line must keep its font, size, color and baseline. Not one pixel may change outside the edited line, checked in MuPDF and in PDFium (the engine inside Chrome). Links must survive.

The editing approach already passes these checks on a real Word PDF, for justified text and for text on a background image. Known gaps for step 3, each tracked by a test that will start failing once it's fixed:

- Links on the edited line are removed.
- With some Word fonts, spaces in the new text come back as non-breaking spaces when the text is copied.
- A re-justified line can end about 0.5pt short of the original right edge.
- The removal step rewrites the whole page's text with tiny rounding changes (about 0.00003pt). That is invisible, but editing only the target line's instructions would leave the rest byte-for-byte identical. Doing so may also fix the first issue and keep the text order.
- Fonts trimmed to the letters a document uses (subsets) can't draw new letters. A font library and a clear warning are needed when the exact font isn't available.

## How it works

- `backend/`: Python (FastAPI + PyMuPDF). Stores uploads, renders page images, and will do all PDF editing. Page images are rendered by the same engine that edits the file, so the preview matches the result.
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

`expected.json` lists every paragraph's true text, font, size and color. `edits.json` lists realistic edits to try. To rebuild them (Windows with Microsoft Word, Word closed):

```powershell
cd tools	est-documents
npm install
node make.js
powershell -ExecutionPolicy Bypass -File export.ps1
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
