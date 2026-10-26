# PDF Editor

A free web tool to edit the text in a PDF: change, add or remove words while keeping the original font, size and color. Works on phones and laptops. Supports English, Arabic, French and Turkish.

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
4. **Lay out.** The line keeps its alignment: left, right, centered, or justified (detected from the paragraph's other lines). A line set against the far margin (a date or a signature at the right of an English, French or Turkish letter) keeps that edge and grows the other way. Outside tables, the page's side margins are taken to mirror each other, so a short line on a sparse page can still grow. Text that would run into other text or a picture on the same row is refused, and so is text past the edge of a box the line is clipped to (a table cell, a text box): its end would be hidden. Word documents follow Word's ordering of mixed Arabic and English. Other PDFs follow the Unicode bidi algorithm (`bidi.py`).
5. **Decorations and links follow.** Underlines, strikethroughs and highlights (`decorations.py`) are redrawn under the words' new place. A link's clickable area moves with its words. A link whose target is its own text ("mailto:" plus the address shown) gets the new text as its target; any other target stays, and the user is told.
6. **Check before accepting.** The edit is refused, and nothing changes, unless:
   - the line reads back as typed;
   - the new text is visible, in both engines: each new word leaves ink where it's drawn, and the line as much as its letters would (a line made much shorter leaves less ink, and that's fine);
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

### When the font isn't on the server

No edit is refused for want of a font. New letters are drawn, in this order:

1. **In the installed font itself**, when the server has it.
2. **With the PDF's own letters.** A PDF carries a copy of every letter it uses, in its own font. New English, French and Turkish words take each letter the copy has from it, exactly as on the page, letter by letter. Word often puts a font in a PDF as two copies, the letters of its old Western code page (é, ç, ö) in one and the others (ş, ğ, ı, İ, ₺) in the other: a word takes its letters from either. A letter a copy lists with width 0 (a code Word didn't use) or can't read back as itself counts as missing there. New Arabic words are drawn from the letter shapes the document's text already shows in that font: which glyph draws each letter alone, at the start, in the middle and at the end of a word (Unicode's joining rules decide which shape a letter takes; lam-alef and other letters drawn as one glyph, like "ريال", are learned too). On this PC, with the real fonts to compare, every Arabic word drawn this way is drawn with exactly the glyphs the font itself would use (`Fonts._from_forms`). This is skipped for words with vowel marks (where a mark sits needs the font's own rules) and for fonts that draw pairs of letters as one glyph depending on their neighbours (Traditional Arabic's stacked pairs).
3. **With a stand-in**, for letters the PDF doesn't have: the font's look-alike (below), or else a free font of its kind (serif, sans-serif or fixed width, as the PDF describes the font; a Naskh or a plainer Arabic font), sized to match the line. Only these letters are named in the note, and the website shows a close before/after to check them: keep it, or undo it. Letters already checked on a line aren't asked about again.

Only a character no font here has at all (a Chinese character, say) is refused, by name.

`backend/lookalikes/` holds the stand-ins (free licences that allow shipping them). `MANIFEST.json` lists each file with its source, version, checksum and licence file.

| Font in the PDF | Look-alike | Letter widths |
|---|---|---|
| Calibri | Carlito | nearly the same |
| Cambria | Caladea | nearly the same |
| Segoe UI | Selawik (Microsoft's own free match) | the same |
| Tahoma | Wine Tahoma (Wine's, renamed so it's never taken for Tahoma) | the same (Latin) |
| Garamond | EB Garamond | different |
| Arial, Helvetica | Liberation Sans | nearly the same |
| Times New Roman, Times | Liberation Serif | nearly the same |
| Courier New, Courier | Liberation Mono | nearly the same |
| Traditional Arabic, Sakkal Majalla, Arabic Typesetting | Amiri | different |
| Simplified Arabic; the Arabic letters of Arial and Times New Roman | Markazi Text | different |
| The Arabic letters of Tahoma, Segoe UI and Calibri | IBM Plex Sans Arabic | different |
| Any other font | Liberation Serif, Sans or Mono; Amiri, IBM Plex Sans Arabic or Markazi Text | different |

The Arabic look-alikes were chosen by eye, side by side with the real fonts. They must draw each letter as one glyph: fonts that split a letter into a base and separate dots (Noto Naskh Arabic, for one) can't be given correct copy and search labels. A stand-in whose widths differ is drawn at the size that matches: the page's own words in that font, against the same words set in the stand-in (else the height of the tall letters in the PDF's own copy of the font). Vowelled text drawn in a stand-in can be refused, when a mark would read back on the wrong letter.

Fonts like Calibri, Segoe UI or Traditional Arabic can't be copied onto a server: their licences allow them only on the computer they came with.

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

Deleting every line of every test document and private sample (656 lines), one at a time, succeeds for all. The text below moves up into the space for 464 of them; for 163 nothing is below. For 29 the space stays, with the reason: mostly a box drawn around the text, or very tight table cells.

## Adding text

`backend/app/adding.py` adds one line of new text, in the style of a line already on the page: its font, size, color and text state. Its letters are shaped as for an edit, so the same font rules apply. There are two ways to place it.

**Below or above a line** (the line panel's "Add line below" and "Add line above"):
- **Making room:** first the text below (adding above: the line itself and the text below it) moves down one line spacing, as in a word processor (see [Moving text](#moving-text)); then the new line goes into the room made. Where the text can't move, the line still goes in if there's room for it at full line spacing; squeezed in, it's refused with the reason.
- **Spacing:** one line spacing away. It's measured in the line's paragraph; failing that, it's the page's most common spacing for that font; failing that, the font's own line height, which Word uses for single spacing.
- **Alignment:** as its paragraph is: start-aligned, centered, or flush right. Arabic under an English line starts at the column's right edge.
- **Lists** (`backend/app/lists.py`): below a list item's last line, or above its first, the new line becomes the list's next item ("Make it item 4.", ticked unless you untick it). Its number goes where the numbers are, in the list's own number font, with its text at the item's tab; a bullet is copied as drawn. The items after it are renumbered in the same change, each an ordinary checked edit of its number; if one can't be made exactly, nothing changes. Numbers, letters, Roman numerals and Arabic-Indic digits are counted on. Where English and Arabic items take turns (1., ١., 2., ٢.), each list counts on past the other language's lines. Unticked, text under an item lines up with the item's text.

**Where you click** (the "Add text" button, then click the page): `backend/app/spots.py` works out what adding there means, and the website says so, with a dotted outline on the page, before anything is written:
- **On a line, between two lines, or just under one:** a new line there, below or above that line, as above (the text below moves down; under a list item's last line, the list's next item).
- **Beside a line, on its row:** the text joins that line, before its first word or after its last; just after the end, it starts a space after it, as if one were typed.
- **In empty space** (an empty table cell, a margin, a blank area): the text starts at the mark. English runs right from it, Arabic runs left. It lines up with the edge of text just above or below, if it's close; you choose from the styles on that page, the nearest first.
- Text doesn't reach across a table's border: a click in an empty cell stays in that cell.
- When a new line can't be made there (a full table cell, a full page), the website offers to place the text exactly where you clicked instead.

New text is drawn after the page's own drawing, with the page's state restored. Where nothing had to move, the page's own instructions are kept byte for byte. Text placed in empty space or joining a line moves nothing, and in every case the new text is refused, with the reason, where it would:
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

Adding a line under every line of every test document and private sample (656 lines): 379 are added (161 before text could move), 313 of them by moving the text below. The rest are refused before anything is written: mostly a full page (93), text too long for one line (44), a full table cell (23), or lines and shapes in the way. None fails a check. On purpose, six lines that used to fit into the gap after them, on full pages of the Chrome test documents, are now refused: they only fitted squeezed in, closer than a line spacing.

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

## Pictures

`backend/app/images.py` lists a page's pictures and changes them. Click a picture on the page to:
- **Replace** it with a JPEG or PNG: **Fit** (all of the new picture, inside the old one's place), **Fill** (covering the place, its edges cut) or **Stretch** (the place's exact shape). A picture used on several pages (a logo in the header) can be replaced on all of them at once.
- **Move or resize** it: drag it, or drag a corner (its shape is kept). Its old place is marked until the move is saved.
- **Delete** it (or press Delete).

**Add picture** puts a new one on the page: choose it, then drag it into place; clicking another spot, on any page, puts it there.

A picture is drawn by one instruction: an image, an inline image, or a form of shapes without text (some programs keep a logo that way). Every change touches only that instruction, with a transformation put around it; a new picture is drawn after the page's own drawing. A picture behind most of the page can't be picked, so text over it still can.

New pictures are kept at print quality: at most 300 dots per inch at the size they're shown, so a phone photo used as a small logo doesn't make the file huge. JPEG photos keep their own compression (unless they have to be made smaller or turned upright); other pictures keep their see-through parts. Files over 15 MB or 40 megapixels are refused.

Before a change is accepted:
- every glyph and shape is where it was, and every other picture too;
- the changed picture is where it was put (replaced: within its old place);
- nothing outside the picture's old and new places changes by a single pixel (MuPDF and PDFium);
- no other page changes, except the pages a picture was replaced on.

## Table rows

Click a line in a table with borders: the panel adds a row below or above its row, or deletes the row (`backend/app/rows.py`).

- **A new row** looks like the row it's added next to, as Word's Insert Below and Above do. First everything below the spot, across the page, moves down one row's height (`moving.py`), the table's lower rows with it. Then the row's own borders and shading are drawn again in the room: its side borders, Word's little corner pieces under its top line, its shading however many strips it's drawn in, and the line along its bottom edge; not its top line (the row above's), nor the underlines and highlights of its text. A path that draws other rows too gives only its pieces in this row. Then each cell's text goes in, typed in a box per cell (in the table's direction: an Arabic table's first cell on the right), in the style of that cell's text (or the nearest cell with text in its column), against the same side of the cell with the same margin, or centered, as it is there.
- **Deleting a row** takes its text away (as deleting lines does: links and underlines with it), then its pictures, borders and shading but its top line, then everything below moves up.
- **Merged cells**, as in Word:
  - A cell merged over the rows on both sides of a new row gets a row taller instead of a new cell (no box for it in the form): its shading and clipping that run across the spot get taller, their top staying. A new row's cells are one under each cell of the row it's added beside, a merged one from a row above too.
  - Deleting a row a merged cell covers makes that cell a row shorter. Under its last row, the line along the row's bottom goes up with it (its bottom border). Refused if its text wouldn't fit any more, with the room above and below text that the table's other cells have.
  - A merged cell's text keeps its place in the cell: at its top it stays, in its middle it moves half a row, at its bottom a whole row. While the rows below move it's held still, then moved once, as a dragged line is, and checked the same way.
  - Beside a cell merged over several rows, a new row goes below its last row or above its first. Above its first, its top border is drawn again under the new row's cell. To delete a row, click a cell one row tall.
  - The new row copies the row's own borders and shading: a merged cell's shading drawn once for all its rows (as Chrome does) isn't copied into a new row beside it; drawn row by row (as Word does), it is.
- It's refused, with the reason, where it can't be done exactly: no room left at the bottom of the page (nothing goes onto the next page), a cell's text too long for its cell (cells don't wrap), a table without borders, lines drawn through all the rows, or a table's only row.
- Each step is checked as other changes are: what moves is exactly where it was, one row lower or higher; the copied borders and shading, drawn alone, look exactly like the row's own, pixel for pixel; nothing outside the row's place changes. The steps make one change: one Undo takes it all back.
- Totals aren't recalculated: a "Total" row stays as it was, and can be edited like any line.

On every bordered table of the test documents, adding a row below and above each row and deleting each row: 28 of 34, 27 of 34 and 32 of 34 are done; the others are refused, for a full page (6), a table's only row, and a line with an icon just above a table, which reads differently once the table moves.

## Table columns

Click a line in a table with borders and choose **Add column left**, **Add column right** or **Delete column** (`backend/app/columns.py`).

**Adding a column.** A box for each new cell takes its text (or leave them empty). The new column is as wide as the narrowest column, if there's room: the table grows where the page has room beside it (on its right, or its left for an Arabic table); otherwise each column gives up whole points of its empty space, its text keeping the side of the cell it keeps to (or its middle) and the table's own margin, its pictures still fitting. Every rectangle's ends move with the edge they're on, or with the nearer side of their cell (a cell's inner shading keeps its margin); a clip smaller than its cell goes with what it clips. The new column gets the look of the one it's added beside: its shading, bars and inner shading drawn again right after them, and that column's edge bars on the new edge. Its text goes in as a new line does, in the style of the cell beside it. Checked: every glyph where it was or moved along its row with its cell, every rectangle where it should be (moved, stretched or drawn again), nothing outside the table's old and new places changed, each cell's text drawn alone looking the same at its new place, every line reading the same, and the page reading as the table with its new column. Refused when there's no room (a table as wide as the page whose columns are full), as for deleting.

**Deleting a column.** Its column goes and the table gets narrower, as in Word.

- A table is drawn as rectangles: each cell's shading and clipping, thin bars on the column edges, bars between them along the rows, small squares where they meet. The column's text goes first, as deleting lines does (with its underlines and links), then its rectangles and pictures, and the bars of one of its edges: its right one, or its left one for the last column, so the table keeps its outer border. Everything right of it moves left by exactly the column's width: rectangles by having their four numbers rewritten, text and pictures whole, inside `q … Q`, as a dragged block does.
- Text is judged by its visible letters: Word puts the spaces of a row's empty cells in the same text object as the text before them.
- Checked: every glyph, shape and picture where it was or exactly one column's width further left; nothing outside the table changes; the table, drawn, is pixel for pixel what it was left of the column, and right of it one column's width further left (a letter or bar exactly between two pixels may match at twice the size; PDFium, which rounds each letter to a whole pixel, may differ by a few pixels there once MuPDF has matched exactly); every line reads the same; the page reads as the same table with one column fewer.
- Refused, with the reason: rows split into columns differently, a table's only column, something drawn across the table's edge.

**Merged cells** (`tests/fixtures/tables`: four made-up tables, as Word users make them):
- Deleting a column: a cell merged across it gets a column narrower and keeps its text, which moves with the side of the cell it keeps to (half as far when it's centered); it must still fit, or the delete is refused. The column's edge bars go only beside its own cells. To delete a column, click a cell one column wide: a cell merged across columns doesn't say which. When the column's cells were all that split two rows (a merged cell beside them on each side), those rows read as one after, as in Word.
- Adding a column: the new column gets a cell beside each cell of the column, as tall as it (beside a cell five rows tall, one cell five rows tall), with its look. A cell merged across the new column's place gets wider instead (no box for it in the form): its shading and bars across it stretch, its centered text stays in its middle. Beside a cell merged across columns, the new column goes beside its last column (right) or its first (left).
- Checked as other columns are; the text of merged cells that moves otherwise than its side of the table is compared on its own, line by line.
- A small square drawn inside a cell, not on a column edge (Word sometimes leaves one where a border was cut short), is treated like the cell's shading, not as a sign of rows split differently.

On the test documents, every column of every bordered table: unmerged tables as before (30 deleted; 55 of 60 added: 4 refused for a table as wide as the page with full columns, 1 where a line just above the table would read differently); the merged tables: every column deleted, and a column added left and right of every cell. In the samples: every operation on their tables with merged cells works (24 of 24); a table whose first column has no borders at all (it reads as part of the next one) works for 3 of 6, the others refused.

## Moving lines and tables

Pick a line and choose **Move line**, pick several lines (Shift-click) and choose **Move**, or pick a line in a table and choose **Move table**. A picture of just what moves (see-through around it) comes up over it: drag it, click a spot, or nudge it with the arrow keys (Shift: ten points), then **Put it here**. Its old place is left empty. It stays on its page (`backend/app/dragging.py`).

- What moves: the lines' text, each text object kept whole and moved by a translation (as `moving.py` moves text), with its underlines, strikethroughs, highlights and links; a table with borders moves with everything drawn inside them: text, borders, shading and pictures. A table without borders moves as its lines.
- Inside `q … Q`, not undone by a second translation: on a page drawn at 0.74999996875 points per unit (Chrome), undoing leaves a rounding error that shifts what's drawn after it by a hair. What the moved text object sets that lasts (its font, color, spacing) is set again after the `Q`.
- Clipping that applies only to what moves goes with it (Word clips a table cell's text to its cell); clipping that applies to other things too stays, and the move must stay inside it (Chrome shows a page's text only inside its margins).
- A move goes by half points, so that, at twice the size, it's a whole number of pixels both ways.

It's refused, with the reason, before anything is done:
- the new place isn't empty: other text, a picture, a line or a shape (a background under both the old and the new place is fine), a form field or comment;
- off the page, or outside the clipping that stays;
- a line the PDF stores as one with other lines (Word draws English and Arabic side by side as one): the website offers to move them together;
- something drawn across a table's edge, a shape drawn together with other shapes, a link that also covers other text, a turned page.

And after, by the same checks as other moves: every glyph, shape, picture and link is where it was or exactly as far away as the move; nothing outside the old and new places changes by a single pixel (MuPDF and PDFium); drawn on its own, what moved looks the same at its new place (a glyph exactly between two pixels can round either way: then it must match at twice the size); and every line reads the same. A line dropped on the same row as other text would be read as one line with it, and a line moved into a margin can change how a mixed English and Arabic line is read: both are refused, saying which.

On the test documents, each line dragged to an empty spot found by scanning (sideways, up or down, and diagonally, in turn) and each table in all three ways: 424 lines and 27 tables moved; 53 lines and 6 tables refused, nearly all for landing on another line's row; every one of 130 drops onto text refused. On the real samples: 129 lines moved, 4 refused (another line's row).

## Download as…

The Download button gives the edited PDF, or the same pages as another kind of file (`backend/app/exporting.py`, `GET /api/documents/{id}/export?format=…`):

| Choice | What you get |
|---|---|
| **Word** | The pages as a document that flows: paragraphs (wrapped lines joined), tables, pictures, header and footer. Text you can edit; lines may wrap a little differently. |
| **PowerPoint** | Every page a slide: a picture of the page without its text, and every line on top in its own text box, editable. |
| **PowerPoint, as pictures** | Every page a slide showing a picture of it: looks exactly the same, text can't be edited. |
| **Excel** | Every table a sheet; amounts become numbers. A PDF without tables gives its text, a paragraph a row. |
| **JPG / PNG pictures** | A picture of every page, 200 dpi (one file, or a zip). |

The text in Word, PowerPoint and Excel always comes from the editor's own reading of the page, never from another library's: that keeps Arabic and mixed Arabic and English right. (pdf2docx, tried first, got 114 of the 326 paragraphs of the test documents exactly right, and garbled Arabic: "ريال" became "لاير". The Word export gets 317; the rest are mostly letters the PDF itself stores wrongly.)

- **Paragraphs** (`flow.py`): pieces of one row are read in their direction; rows join when the first was wrapped (the next row's first word wouldn't have fitted at its end); a bigger gap, a new size or a list marker starts a new paragraph. Text in a box or a column wraps at that box's or column's edge, found from a drawn box, text beside it, or several lines ending at the same place.
- **Tables** (`tables.py`): columns and rows from the drawn borders (Word draws each cell's border as its own piece; pieces at nearly the same place are one), merged cells where there's no border between two cells, shading; or, without borders, text in columns over three rows or more. Arabic tables start on the right.
- **Word**: alignment (start, end, centered, justified, Arabic kashida), indents, the space before each paragraph and the line spacing as on the page; each piece in its font, size, color, bold, italic, raised or lowered, underlined, struck through, highlighted or linked. Arabic paragraphs and pieces are right-to-left, and Arabic letters get their font as Word's "complex script" font, as Word itself saves them. The rest is marked English, or French or Turkish when the document is (for Word's spelling check). Pictures go where they were: on a line of their own, inside a table cell, or within a line (an icon). Text and pictures repeated at the top or bottom of the pages go into the header and footer, with live page numbers. Each page starts a new page; a page of another size starts a new section.
- **PowerPoint**: the page's text is taken out of the picture underneath with the same removal deleting a line uses. Lines the PDF stores with wrong letters, turned text and invisible text stay in the picture. PowerPoint decides a piece's direction by its language, so Arabic pieces are marked Arabic (otherwise a colon between Arabic and English jumps to the end).
- **Excel**: amounts written the English, French or Turkish way (1,700.00 / 1 700,00 € / 1.250,00 ₺ / 15 % / %20) become numbers with a format that shows them as written; codes with leading zeros, dates and phone numbers stay text.
- Lines whose letters the PDF stores wrongly are highlighted in yellow in Word, and the website says how many.

Known limits: shapes drawn with lines (a logo made of shapes, a chart) don't come into Word; a page in two columns becomes one column, wrapped at the column's width; text boxes become indented paragraphs.

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
| `turkish-quote` | Turkish letters (ç ğ ı İ ö ş ü, often in one word: Müşteri, İşçilik), capitals with dotted and dotless I, Turkish amounts (1.250,00 ₺, %20) in a price table, five fonts |

`backend/tests/fixtures/additions/` has lists (numbered and bulleted, English and Arabic), a centered title, a justified paragraph, a right-aligned line and a table with an empty cell, with free space after each, for adding text. It is saved by Word and printed by Chrome.

`backend/tests/fixtures/decorations/` has one document of underlines (single, double, colored, part of a word, under a link), strikethroughs and highlights, next to look-alikes that belong to the page (a paragraph border, a table border right under cell text, the footnote separator). It is saved by Word and printed by Chrome.

`backend/tests/fixtures/images/` has a report with pictures: a logo (in Word's version, in the header of every page), a photo, an icon inside a line, a see-through signature and stamp in a table, Arabic text, and the same signature on page 2. Chrome's version also has a logo drawn as shapes. `make_images.py` draws the pictures; `images.js` (Word) and `images.html` (Chrome) build the document.

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

## Putting it online

`deploy/` puts the site on a small Linux server, such as Oracle Cloud's Always Free one (Ubuntu 24.04):
- **Once, on the server:** `sudo bash server-setup.sh`. It installs Python, the fonts that may be installed on Linux (Microsoft's core fonts, Carlito and Caladea), Caddy for https, the app's own user and folders, and opens ports 80 and 443.
- **Each time, from this PC (Git Bash):** `deploy/push.sh ubuntu@<server address> <domain>`. It builds the website, sends only committed files (never `samples/` or local data), installs the exact package versions the tests passed with (`backend/requirements.lock`), and restarts the app. Caddy gets the https certificate for the domain itself.

The app runs as a service (`deploy/pdf-editor.service`) that may write only `/var/lib/pdf-editor`. Its log: `journalctl -u pdf-editor`. Limits per visitor, per file and in total (below) keep one person, or a script, from wearing the server out. Fonts that can't be installed on Linux (Calibri, Windows' Arabic fonts) are replaced by look-alikes, with the "Does it look right?" check.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PDFEDIT_DATA_DIR` | `backend/data` | Where uploads are stored |
| `PDFEDIT_HIDE_FONTS` | (none) | Fonts to treat as not installed, comma-separated (`calibri*,traditionalarabic`; `*` matches any ending): to see what a server without them would do |
| `PDFEDIT_MAX_UPLOAD_MB` | `20` | Largest file accepted |
| `PDFEDIT_MAX_PAGES` | `50` | Most pages accepted |
| `PDFEDIT_DOC_TTL_MINUTES` | `60` | Delete files this long after last use |
| `PDFEDIT_RATE_UPLOADS_PER_HOUR` | `30` | Files one visitor may open per hour |
| `PDFEDIT_RATE_CHANGES_PER_HOUR` | `300` | Changes (and undo, redo) one visitor may make per hour |
| `PDFEDIT_RATE_READS_PER_MINUTE` | `900` | Page images and the like one visitor may load per minute |
| `PDFEDIT_RATE_EXPORTS_PER_HOUR` | `60` | Downloads as Word, PowerPoint, Excel or pictures one visitor may make per hour |
| `PDFEDIT_HEAVY_JOBS` | `2` | Changes and uploads worked on at once; more wait their turn |
| `PDFEDIT_MAX_VERSIONS` | `60` | Changes kept per file (each keeps a full copy); then: download, and open the download |
| `PDFEDIT_MAX_DOC_MB` | `300` | Space all of a file's versions may take |
| `PDFEDIT_MAX_TOTAL_MB` | `4000` | Space all stored files may take; new uploads wait until older files are deleted |
| `VITE_APP_NAME` | `PDF Editor` | Name shown on the website. Set it in `frontend/.env.local` for a branded deployment |
| `VITE_SOURCE_URL` | this repository | Link to the source code shown on the website |
