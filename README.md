# PDF Editor

A free web tool to edit the text in a PDF: change, add or remove words while keeping the original font, size and color. Works on phones and laptops. Supports English, French and Arabic.

Licensed under the GNU AGPL v3 (see `LICENSE`). Anyone running this as a public website must offer its source code to users.

## Status

| Step | What | State |
|---|---|---|
| 1 | Upload, view pages, download | Done |
| 2 | Detect every text line with its font, size, color and position | Done |
| 3 | Edit text in place, matching the original style | Done |
| 4 | Delete lines, with their underlines and links | Done |
| 5 | Add new text, in the style of text on the page | Done |
| 6 | Changes list, undo and redo, before/after view, look-alike fonts | Done |
| 7 | Public launch: limits, password PDFs, deploy | Next |

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

Turning a page (/Rotate), with or without a crop box, doesn't change what is read.

`backend/tests/test_textlayout.py` checks against an answer key, the made-up documents saved as PDF by Word and printed to PDF by Chrome. Every line must be typed text, and every paragraph must be found again in order. A consistency check also runs on any PDF. It confirms the display text lays out like the page, but it can't catch a wrong direction. So for private samples, a report (`samples/detection-report.txt`) lists every line whose direction was a guess.

## Editing

`backend/app/editing.py` changes one line at a time:

1. **Replay.** The page's drawing instructions are replayed (`pdfcontent.py`), and every glyph must land where MuPDF puts it. If not, the page uses something not modelled yet and the edit is refused.
2. **Keep unchanged words.** Words that didn't change keep their original glyphs. Changed words are shaped with the installed font (`fonts.py`, HarfBuzz), with the line's own letter spacing. A word that replaces one old word keeps its letters' looks, so superscripts ("3rd" to "21st"), subscripts ("CO2" to "CO3") and small capitals stay as they were. A spelled-out "third" isn't half raised. The font must match by its exact name: a missing bold or italic is never swapped for the regular weight.
3. **Choose the font.** The PDF's embedded copy of the font is reused only when it has every glyph needed, drawn identically, with correct copy/search labels. Otherwise the installed font's glyphs are embedded.
4. **Lay out.** The line keeps its alignment: left, right, centered, or justified (detected from the paragraph's other lines). Outside tables, the page's side margins are taken to mirror each other, so a short line on a sparse page can still grow. Text that would run into other text or a picture on the same row is refused. Word documents follow Word's ordering of mixed Arabic and English. Other PDFs follow the Unicode bidi algorithm (`bidi.py`).
5. **Decorations and links follow.** Underlines, strikethroughs and highlights (`decorations.py`) are redrawn under the words' new place. A link's clickable area moves with its words. A link whose target is its own text ("mailto:" plus the address shown) gets the new text as its target; any other target stays, and the user is told.
6. **Check before accepting.** The edit is refused, and nothing changes, unless:
   - the line reads back as typed;
   - the new text is visible (it leaves ink, in both engines);
   - a second reader (PDFium) sees the new letters;
   - nothing outside the line changes by a single pixel, in MuPDF and PDFium;
   - MuPDF sees the same shapes and links as before, except those that moved with their words;
   - no other page changes.

Each edit is a new version, so Undo goes back.

Known limits:
- one line at a time, with no reflow into the next line;
- text too long for its line is refused;
- kashida-justified lines come back justified with spaces, with a warning;
- an Arabic word that mixes sizes or colors inside it can't be changed;
- a word that is only partly underlined, highlighted or linked can't be changed (the rest of its line can);
- a link that covers several lines doesn't move with the text, with a warning;
- fonts whose licence forbids embedding can't get new letters;
- lines marked uncertain need their unreadable letters retyped;
- rotated text and text inside embedded objects can't be edited yet.

**Fonts on the server matter.** Word strips the Arabic shaping rules from the fonts it embeds, so new Arabic text needs the real font installed (`PDFEDIT_FONT_DIRS`). If the installed font isn't the exact version in the PDF, the edit goes ahead with a warning. On Windows, Office's cloud fonts (Aptos, the default in recent Word) are found in Office's font cache once Word has downloaded them. Without the font, the new letters are drawn in this order of preference:
1. the PDF's own copy of the font, for Latin letters it already contains (the exact glyphs);
2. a **look-alike font** that ships with the code (see below), and the user is asked to check the result;
3. otherwise the edit is refused.

### Look-alike fonts

`backend/lookalikes/` holds free fonts (SIL Open Font License) that stand in for common fonts that can't be shipped. `MANIFEST.json` lists each file with its source, version, checksum and licence file.

| Font in the PDF | Look-alike | Letter widths |
|---|---|---|
| Calibri | Carlito | nearly the same |
| Cambria | Caladea | nearly the same |
| Arial, Helvetica | Liberation Sans | nearly the same |
| Times New Roman, Times | Liberation Serif | nearly the same |
| Courier New, Courier | Liberation Mono | nearly the same |
| Traditional Arabic, Sakkal Majalla, Arabic Typesetting | Amiri | different |
| Simplified Arabic; the Arabic letters of Arial and Times New Roman | Markazi Text | different |
| Tahoma, Segoe UI (Arabic letters) | IBM Plex Sans Arabic | different |

The Arabic look-alikes were chosen by eye, side by side with the real fonts. They must draw each letter as one glyph: fonts that split a letter into a base and separate dots (Noto Naskh Arabic, for one) can't be given correct copy and search labels. Each is drawn at the size that matches the original: the page's own Arabic words in that font, against the same words set in the look-alike (for a line stretched with kashida, the height of the tall letters in the PDF's own copy of the font). Vowelled text drawn in a look-alike can be refused, when a mark would read back on the wrong letter. A bold or italic original gets the look-alike's bold or italic; where the look-alike has none, or for fonts with no free look-alike (Aptos, Georgia, Verdana, the Latin letters of Tahoma), the edit is refused.

After a change that used a look-alike, the website shows a close before/after of the spot and asks whether it looks right: keep it, or undo it.

### Underlines, strikethroughs and highlights

Word and Chrome draw each one as a filled rectangle next to the text, not as part of the font. `backend/app/decorations.py` tells them apart from shapes that only look similar and belong to the page: a paragraph's border, a table border right under a cell's text, a cell's shading, the footnote separator. A decoration starts and ends exactly at letter edges of one line, at a set height from its baseline. A list bullet drawn as a small filled circle just before a line (as Chrome draws them) goes with its line too. Shading and borders reach past the text on at least one side. The page's shapes are replayed too, and must match MuPDF's own list of them.

So far this is tested on PDFs from Word and Chrome only. In case another program draws decorations differently, a delete that leaves a shape right under the deleted text, touching no other text, comes with a warning to check the page.

## Changes, undo and redo, before and after

Every change is stored as a new version of the file (`backend/app/storage.py`), with what it was and where:
- the area its own check proved that nothing outside of changed;
- a tight outline of the old and new lines, for display.

Undo, redo and "go back to the original" move between versions; an undone change stays listed, and can be redone, until the next change. On the website:
- the Changes list shows every change and opens a close before/after of any of them (Check), or jumps to it on the page (Show);
- "Compare with the original" flips the pages between the original and the current version, with every change outlined. Editing is off while comparing.

A test makes changes, undoes and redoes one, then checks that every pixel differing from the original lies inside the recorded areas.

## Deleting

`backend/app/deleting.py` deletes whole lines, on one page or several, as one change: one Undo brings them all back. A line's glyphs are taken out, each replaced by an equal spacing number so nothing else moves. Its underlines, strikethroughs and highlights go with it, and so do links that only cover deleted text.

Then the text below moves up into the space, as a second, separately checked step (see [Moving text](#moving-text)). The next line takes the deleted line's place. After a paragraph's last line, the next paragraph moves up by one line instead, so the gap between paragraphs stays. Where moving can't be done exactly (deleted lines that aren't next to each other, a box around the text), the space stays and the website says why; the delete itself is never refused for that. Deleting whole numbered list items counts the items after them down.

Deleting needs no fonts and no layout, so lines that can't be edited (rotated text, unreadable letters, fonts not on the server) can still be deleted. Before a delete is accepted:
- every other glyph on the page is drawn exactly as before;
- nothing outside the deleted lines changes by a single pixel (MuPDF and PDFium);
- PDFium reads no letters left where the lines were;
- MuPDF sees the same shapes and links, minus those removed.

These checks are tested on turned pages (/Rotate), cropped pages, and pages whose boxes don't start at 0,0.

On the website: click a line and choose Delete line. To delete several, Shift-click or Ctrl-click them, or tap "Select more lines" on a phone. The Delete key works too. Emptying a line's text in the edit box also deletes it.

Deleting every line of every test document and private sample, one at a time, succeeds for all 635, with no warnings.

## Adding text

`backend/app/adding.py` adds one line of new text, in the style of a line already on the page: its font, size, color and text state. Its letters are shaped as for an edit, so the same font rules apply. There are two ways to place it.

**Below or above a line** (the line panel's "Add line below" and "Add line above"):
- **Making room:** first the text below (adding above: the line itself and the text below it) moves down one line spacing, as in a word processor (see [Moving text](#moving-text)); then the new line goes into the room made. Where the text can't move, the line still goes in if there's room for it at full line spacing; squeezed in, it's refused with the reason.
- **Spacing:** one line spacing away. It's measured in the line's paragraph; failing that, it's the page's most common spacing for that font; failing that, the font's own line height, which Word uses for single spacing.
- **Alignment:** as its paragraph is: start-aligned, centered, or flush right. Arabic under an English line starts at the column's right edge.
- **Lists** (`backend/app/lists.py`): below a list item's last line, or above its first, the new line becomes the list's next item ("Make it item 4.", ticked unless you untick it). Its number goes where the numbers are, in the list's own number font, with its text at the item's tab; a bullet is copied as drawn. The items after it are renumbered in the same change, each an ordinary checked edit of its number; if one can't be made exactly, nothing changes. Numbers, letters, Roman numerals and Arabic-Indic digits are counted on. Unticked, text under an item lines up with the item's text.

**At a tapped spot** (the "Add text anywhere" button, then tap the page):
- **Position:** the text starts at the mark. English runs right from it, Arabic runs left.
- **Snapping:** it lines up with text already on that row (an empty table cell next to a filled one), or with the edge of text just above or below, if it's close.
- **Style:** you choose from the styles on that page; the nearest is chosen first.

New text is drawn after the page's own drawing, with the page's state restored. Where nothing had to move, the page's own instructions are kept byte for byte. At a tapped spot nothing moves to make room, and in either case the new line is refused, with the reason, where it would:
- touch other text, a picture, a form field or a line;
- cross a border out of its table cell;
- sit half a row off nearby text;
- go past the page or its margins.

Before it is accepted:
- every glyph already on the page is drawn exactly as before;
- the new line reads back as typed (our reader), and the other lines read as before;
- PDFium sees its letters;
- nothing outside it changes by a single pixel (MuPDF and PDFium);
- MuPDF sees the same shapes and links, and no other page changes.

Adding a line under every line of every test document and private sample (656 lines): 161 are added. The rest are refused before anything is written, almost all for lack of room in the middle of paragraphs. None fails a check.

Known limits:
- one line at a time;
- the style comes from a line on the same page;
- rotated text can't be a style;
- Chrome draws list bullets as shapes, and those aren't copied for a new item (deleting an item does take its bullet);
- in other programs (copying the page's text, search, screen readers), added text comes at the end of the page's text, and outside a tagged PDF's structure. Other PDF editors work the same way.

### Edit accuracy

`backend/tests/test_edit_fidelity.py` measures edits independently of the editor, with MuPDF's own text extraction and pixels in MuPDF and PDFium. `backend/tests/test_editing.py` runs 20 realistic edits on the Word and Chrome test documents, and pins the ones that must be refused. It also runs many edits in a row on the same file, and edits words with superscripts, subscripts and small capitals.

Redrawing every word of every line with its own text, as a stress test, gives a median ink agreement of 0.93 with the original (1 px slack). Arabic lines score lowest. Their letters come out slightly differently spaced than Word's, though the text stays correct; short lines also score low from sub-pixel shifts alone. Normal edits redraw only the changed words.

## Moving text

`backend/app/moving.py` moves the text below a spot down (to make room for a new line) or up (into a deleted line's space), as a word processor would.

What moves is everything drawn in the spot's column below it: text, underlines and highlights, shapes, pictures and links. The column is the page's width, or a table cell between its side borders. It goes down to the first thing that has to stay:
- a footer or page number: text repeated at the same place on the pages around (digits aside), or inside the bottom margin;
- a table border between rows;
- anything only partly in the column.

Nothing goes onto another page or into the bottom margin. The margin is taken from the evidence, the lowest body text on the pages around; failing that, it's as wide as the narrower side margin. A picture or color behind the whole page stays behind it, and Chrome's page-sized clips that cut the gaps in an underline move with it.

Each moved text object, shape and picture keeps its own instructions: a translation is put just before it and undone just after it. Drawing order, clipping, and later edits of the moved lines are as before.

It's refused, with the reason, where it can't be done exactly: a box or line drawn across the spot, text drawn together with the text above it, a column beside other text, a color gradient, a form field in the way, or no room left before the bottom of the page or the cell (table cells don't grow yet).

Before it is accepted:
- every glyph, shape, picture and link is where it was, or exactly as far lower (or higher);
- nothing outside the block's old and new places changes by a single pixel (MuPDF and PDFium);
- drawn on its own, the block looks the same at its new place, pixel for pixel. It's rendered at a zoom that moves it by a whole number of pixels, so every glyph lands on the same fraction of a pixel. PDFium, which rounds glyph positions to whole pixels, gets one pixel of slack;
- every line on the page reads the same, the moved ones at their new place;
- no other page changes.

## The website

- A list of small page pictures sits beside the pages; on phones, the page number button opens it.
- A selected line shows in the panel magnified from the page itself, with its font, size and color.
- The magnifier (its button, or the M key) shows the print up close under the mouse.
- Ctrl+Z undoes and Ctrl+Shift+Z or Ctrl+Y redoes (Cmd on a Mac). The Delete key deletes the selected lines.
- A change lights up on the page once its new image is showing.
- Closing a file whose changes weren't downloaded asks first, and the browser warns before the tab closes.
- Light and dark looks. The ? button gives a short tour.

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

`backend/tests/fixtures/additions/` has lists (numbered and bulleted, English and Arabic), a centered title, a justified paragraph, a right-aligned line and a table with an empty cell, with free space after each, for adding text. It is saved by Word and printed by Chrome.

`backend/tests/fixtures/decorations/` has one document of underlines (single, double, colored, part of a word, under a link), strikethroughs and highlights, next to look-alikes that belong to the page (a paragraph border, a table border right under cell text, the footnote separator). It is saved by Word and printed by Chrome.

`expected.json` lists every paragraph's true text, font, size and color. `edits.json` lists realistic edits to try. `backend/tests/fixtures/chrome/` has the same paragraphs printed to PDF by Chrome, plus fully vowelled Arabic. To rebuild them all (Windows with Microsoft Word and Chrome; close Word first):

```powershell
cd tools\test-documents
npm install
node make.js
node decorations.js
node additions.js
powershell -ExecutionPolicy Bypass -File export.ps1
python print_chrome.py
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PDFEDIT_DATA_DIR` | `backend/data` | Where uploads are stored |
| `PDFEDIT_HIDE_FONTS` | (none) | Fonts to treat as not installed, comma-separated (`calibri*,traditionalarabic`; `*` matches any ending): to see what a server without them would do |
| `PDFEDIT_MAX_UPLOAD_MB` | `20` | Largest file accepted |
| `PDFEDIT_MAX_PAGES` | `50` | Most pages accepted |
| `PDFEDIT_DOC_TTL_MINUTES` | `60` | Delete files this long after last use |
| `VITE_APP_NAME` | `PDF Editor` | Name shown on the website. Set it in `frontend/.env.local` for a branded deployment |
| `VITE_SOURCE_URL` | this repository | Link to the source code shown on the website |
