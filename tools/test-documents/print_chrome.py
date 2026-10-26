"""Make the same test documents as PDFs from a second program: Chrome's print to PDF.

Chrome (Skia) writes PDFs very differently from Word: its own font subsets, shaping
and glyph numbering. The paragraphs come from the Word answer key
(backend/tests/fixtures/word/expected.json), so the same checks apply.

    python print_chrome.py                  (needs Chrome or Edge installed)
    python print_chrome.py turkish-quote    just that one; the others stay as they are

Output: backend/tests/fixtures/chrome/<doc>-chrome.pdf and expected.json.
"""

import html
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORD = ROOT / "backend" / "tests" / "fixtures" / "word"
OUT = ROOT / "backend" / "tests" / "fixtures" / "chrome"

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome",
    "chromium",
]

# Extra paragraphs Word's documents don't have: fully vowelled Arabic, with shadda
# combined with other marks, and tanween.
EXTRA = {
    "arabic-quotation": [
        {
            "text": "إِنَّ العِلْمَ نُورٌ يُضِيءُ دُرُوبَ الحَيَاةِ، وَالمَعْرِفَةُ مِفْتَاحُ التَّقَدُّمِ وَالنَّجَاحِ، فَمَنْ جَدَّ وَجَدَ وَمَنْ زَرَعَ حَصَدَ.",
            "font": "Traditional Arabic",
            "size": 18,
        },
        {
            "text": "يُقَدِّمُ فَرِيقُنَا الدَّعْمَ الفَنِّيَّ عَلَى مَدَارِ السَّاعَةِ لِجَمِيعِ العُمَلَاءِ.",
            "font": "Arial",
            "size": 14,
        },
    ]
}

# The page's language, as a web page in it would say: Chrome then draws its letters the
# language's way (Turkish: no "fi" joined into one letter, since i keeps its dot).
LANG = {"turkish-quote": "tr"}
DOCS = ["arabic-quotation", "french-quote", "bilingual-contract", "english-styles", "turkish-quote"]

ALIGN = {"start": "start", "left": "left", "right": "right", "center": "center", "both": "justify", "mediumKashida": "justify"}


def run_html(run: dict) -> str:
    style = (
        f"font-family:'{run['font']}';font-size:{run['size']}pt;color:#{run['color']};"
        f"font-weight:{'bold' if run['bold'] else 'normal'};font-style:{'italic' if run['italic'] else 'normal'}"
    )
    text = html.escape(run["text"])
    if run.get("link"):
        return f'<a href="{html.escape(run["link"])}" style="{style}">{text}</a>'
    return f'<span style="{style}">{text}</span>'


def page_html(paragraphs: list[dict], lang: str | None = None) -> str:
    body = "\n".join(
        f'<p dir="{"rtl" if p["rtl"] else "ltr"}" style="text-align:{ALIGN.get(p["align"], "start")}">'
        + "".join(run_html(r) for r in p["runs"])
        + "</p>"
        for p in paragraphs
    )
    return (
        f"<!doctype html><html{f' lang={lang!r}' if lang else ''}><head><meta charset='utf-8'><style>"
        "@page{size:A4;margin:2.5cm} p{margin:0 0 8pt;line-height:1.35}"
        f"</style></head><body>{body}</body></html>"
    )


def browser() -> str:
    for b in BROWSERS:
        if Path(b).exists() or shutil.which(b):
            return b
    sys.exit("Chrome or Edge not found")


def main(only: list[str]):
    OUT.mkdir(parents=True, exist_ok=True)
    word = json.loads((WORD / "expected.json").read_text(encoding="utf-8"))
    answers = OUT / "expected.json"
    expected = [p for p in json.loads(answers.read_text(encoding="utf-8")) if p["doc"] not in only] if only else []
    exe = browser()
    with tempfile.TemporaryDirectory() as tmp:
        for name in only or DOCS:
            paras = [p for p in word if p["doc"] == name and p["text"].strip()]
            for extra in EXTRA.get(name, []):
                paras.append({
                    "doc": name, "text": extra["text"], "rtl": True, "align": "both",
                    "runs": [{"text": extra["text"], "font": extra["font"], "size": extra["size"],
                              "color": "000000", "bold": False, "italic": False}],
                })
            expected.extend(paras)
            page = Path(tmp) / f"{name}.html"
            page.write_text(page_html(paras, LANG.get(name)), encoding="utf-8")
            pdf = OUT / f"{name}-chrome.pdf"
            subprocess.run(
                [exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}\\profile",
                 f"--print-to-pdf={pdf}", page.as_uri()],
                check=True, capture_output=True, timeout=120,
            )
            print(f"saved {pdf.name}")
    answers.write_text(json.dumps(expected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def print_page(name: str):
    """<name>.html, written by hand, -> fixtures/<name>/<name>-chrome.pdf. For the
    decorations document (underlines, strikethroughs, highlights) and the additions
    document (lists and free space for adding text)."""
    out = ROOT / "backend" / "tests" / "fixtures" / name / f"{name}-chrome.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    page = Path(__file__).resolve().parent / f"{name}.html"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}\\profile",
             f"--print-to-pdf={out}", page.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
    print(f"saved {out.name}")


HAND_WRITTEN = ["decorations", "additions", "images", "tables"]

if __name__ == "__main__":
    # python print_chrome.py decorations   -> only that document
    if sys.argv[1:] and all(a in HAND_WRITTEN for a in sys.argv[1:]):
        for name in sys.argv[1:]:
            print_page(name)
    elif sys.argv[1:] and all(a in DOCS for a in sys.argv[1:]):
        main(sys.argv[1:])
    else:
        main([])
        for name in HAND_WRITTEN:
            print_page(name)
