"""Turn incoming files into a single PDF the printer accepts natively.

PDF passes through (optionally cut down to a page range); images are placed on
an A4 page (auto-rotated to landscape when that fits better); plain text is
typeset in a monospace font. Everything else is rejected with a clear error.
"""

import io
import os
import re
from typing import List, Optional, Tuple

import img2pdf
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - optional
    pass

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yml", ".yaml", ".ini", ".conf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}

MONO_FONT_PATH = os.getenv("MONO_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


class ConversionError(ValueError):
    pass


def detect_kind(data: bytes, filename: str = "", content_type: str = "") -> str:
    """Return 'pdf', 'image' or 'text' (or raise ConversionError)."""
    head = data[:16]
    ext = os.path.splitext(filename.lower())[1]
    ct = (content_type or "").split(";")[0].strip().lower()
    if head.startswith(b"%PDF") or (ct == "application/pdf" and b"%PDF" in data[:1024]):
        return "pdf"
    if (
        head.startswith(b"\xff\xd8\xff")                   # JPEG
        or head.startswith(b"\x89PNG")                     # PNG
        or head[:6] in (b"GIF87a", b"GIF89a")              # GIF
        or head.startswith(b"BM")                          # BMP
        or head[:4] in (b"II*\x00", b"MM\x00*")            # TIFF
        or (head[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WebP
        or data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif")  # HEIC
    ):
        return "image"
    if ext in IMAGE_EXTENSIONS or ct.startswith("image/"):
        return "image"
    if ext in TEXT_EXTENSIONS or ct.startswith("text/") or ct in ("application/json", "application/xml"):
        return "text"
    if _looks_like_text(data):
        return "text"
    raise ConversionError(
        f"Nicht unterstütztes Dateiformat ({filename or ct or 'unbekannt'}). "
        "Druckbar sind PDF, Bilder (JPG/PNG/GIF/TIFF/WebP/HEIC) und reiner Text."
    )


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# --------------------------------------------------------------------------
# Converters
# --------------------------------------------------------------------------
A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
BORDER = (img2pdf.mm_to_pt(8), img2pdf.mm_to_pt(8))


def image_to_pdf(data: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(data))
        frames = []
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            frame = ImageOps.exif_transpose(img.copy())
            if frame.mode not in ("RGB", "L"):
                rgba = frame.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                frame = bg
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            frames.append(buf.getvalue())
            if img.format == "GIF":  # animated GIF → first frame only
                break
    except Exception as e:
        raise ConversionError(f"Bild konnte nicht gelesen werden: {e}") from e
    layout = img2pdf.get_layout_fun(pagesize=A4, border=BORDER, fit=img2pdf.FitMode.into, auto_orient=True)
    return img2pdf.convert(frames, layout_fun=layout)


def text_to_pdf(data: bytes, title: str = "") -> bytes:
    from reportlab.lib.pagesizes import A4 as RL_A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\t", "    ")
    font, size, leading = "Courier", 9.5, 12.5
    if os.path.exists(MONO_FONT_PATH):
        pdfmetrics.registerFont(TTFont("Mono", MONO_FONT_PATH))
        font = "Mono"

    width, height = RL_A4
    margin = 18 * mm
    usable = width - 2 * margin
    char_w = pdfmetrics.stringWidth("M", font, size)
    max_chars = max(20, int(usable // char_w))

    lines: List[str] = []
    for raw in text.split("\n"):
        while len(raw) > max_chars:
            lines.append(raw[:max_chars])
            raw = raw[max_chars:]
        lines.append(raw)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=RL_A4)
    if title:
        c.setTitle(title)
    per_page = int((height - 2 * margin) // leading)
    for start in range(0, max(len(lines), 1), per_page):
        c.setFont(font, size)
        y = height - margin
        for line in lines[start:start + per_page]:
            c.drawString(margin, y, line)
            y -= leading
        c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------
# PDF helpers
# --------------------------------------------------------------------------
def parse_page_ranges(spec: str, total: int) -> List[int]:
    """'1-3,5,8-' → zero-based page indexes, validated against total."""
    pages: List[int] = []
    for part in re.split(r"\s*,\s*", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ConversionError(f"Ungültiger Seitenbereich: '{part}' (Beispiel: '1-3,5')")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else total
        if a < 1 or b < a or a > total:
            raise ConversionError(f"Seitenbereich '{part}' passt nicht zu {total} Seiten.")
        pages.extend(range(a - 1, min(b, total)))
    if not pages:
        raise ConversionError("Seitenbereich ist leer.")
    return pages


def select_pages(pdf: bytes, page_ranges: Optional[str]) -> Tuple[bytes, int]:
    """Return (pdf, page_count) — cut down to page_ranges if given."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            reader.decrypt("")
        total = len(reader.pages)
    except Exception as e:
        raise ConversionError(f"PDF konnte nicht gelesen werden: {e}") from e
    if not page_ranges:
        return pdf, total
    indexes = parse_page_ranges(page_ranges, total)
    writer = PdfWriter()
    for i in indexes:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), len(indexes)


def to_printable_pdf(
    data: bytes, filename: str = "", content_type: str = "", page_ranges: Optional[str] = None
) -> Tuple[bytes, int, str]:
    """Return (pdf_bytes, page_count, detected_kind)."""
    if not data:
        raise ConversionError("Datei ist leer.")
    kind = detect_kind(data, filename, content_type)
    if kind == "image":
        pdf = image_to_pdf(data)
    elif kind == "text":
        pdf = text_to_pdf(data, title=filename)
    else:
        pdf = data
    pdf, pages = select_pages(pdf, page_ranges)
    return pdf, pages, kind
