import re
import zipfile
from xml.etree import ElementTree

DOC_EXT = (".docx", ".md", ".markdown", ".txt")
DOC_CHAR_CAP = 30_000
DOC_ERRORS = (ValueError, OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t"))
        style = p.find(f"{_W}pPr/{_W}pStyle")
        level = re.match(r"Heading(\d)", style.get(f"{_W}val", "")) if style is not None else None
        lines.append(f"{'#' * int(level.group(1))} {text}" if level and text else text)
    return "\n".join(lines).strip()


def read_document(path: str) -> str:
    if path.lower().endswith(".docx"):
        return _docx_text(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def document_line(label: str, text: str) -> str:
    cut = len(text) > DOC_CHAR_CAP
    body = text[:DOC_CHAR_CAP] + ("\n[… text cut off here]" if cut else "")
    return f'[Attached document {label}. Its text:]\n{body.strip() or "(no text found)"}\n[End of {label}]'
