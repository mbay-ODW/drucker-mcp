import base64
import io
import os
import sys

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drucker-mcp-server"))

import convert  # noqa: E402
import ipp  # noqa: E402
import main  # noqa: E402
import sources  # noqa: E402


def _pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _png(size=(400, 200), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (255, 0, 0, 128) if mode == "RGBA" else (255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


# --- convert ---------------------------------------------------------------
def test_detect_kinds():
    assert convert.detect_kind(_pdf(1)) == "pdf"
    assert convert.detect_kind(_png()) == "image"
    assert convert.detect_kind("Grüße\nZeile 2".encode()) == "text"
    with pytest.raises(convert.ConversionError):
        convert.detect_kind(b"PK\x03\x04\x00\x00binary", "brief.docx")


def test_page_ranges():
    assert convert.parse_page_ranges("1-3,5", 10) == [0, 1, 2, 4]
    assert convert.parse_page_ranges("8-", 10) == [7, 8, 9]
    assert convert.parse_page_ranges("-2", 10) == [0, 1]
    for bad in ("0", "5-3", "11", "x"):
        with pytest.raises(convert.ConversionError):
            convert.parse_page_ranges(bad, 10)


def test_pdf_passthrough_and_select():
    pdf, pages, kind = convert.to_printable_pdf(_pdf(12), "a.pdf")
    assert (pages, kind) == (12, "pdf")
    pdf, pages, _ = convert.to_printable_pdf(_pdf(12), "a.pdf", page_ranges="2-4")
    assert pages == 3 and len(PdfReader(io.BytesIO(pdf)).pages) == 3


def test_image_with_alpha_to_a4_landscape():
    pdf, pages, kind = convert.to_printable_pdf(_png((800, 400)), "x.png")
    assert (pages, kind) == (1, "image")
    box = PdfReader(io.BytesIO(pdf)).pages[0].mediabox
    # wide image → auto-oriented A4 landscape
    assert round(float(box.width)) == 842 and round(float(box.height)) == 595


def test_text_paginates():
    text = "\n".join(f"Zeile {i} äöü ß €" for i in range(200)).encode()
    pdf, pages, kind = convert.to_printable_pdf(text, "notiz.txt")
    assert kind == "text" and pages >= 3


# --- ipp encoding ----------------------------------------------------------
def test_ipp_roundtrip_multivalue():
    req = ipp.encode_request(
        ipp.OP_GET_PRINTER_ATTRIBUTES,
        [(ipp.TAG_KEYWORD, "requested-attributes", ["a", "b"])],
        request_id=7,
    )
    # Reinterpret the request as a response: same layout, "status" = op id.
    status, groups = ipp.decode_response(req)
    assert status == ipp.OP_GET_PRINTER_ATTRIBUTES
    assert groups[0][1]["requested-attributes"] == ["a", "b"]
    assert groups[0][1]["attributes-charset"] == "utf-8"


def test_http_uri():
    assert ipp._http_uri("ipp://10.0.1.15/ipp/print") == "http://10.0.1.15:631/ipp/print"
    assert ipp._http_uri("ipp://h:8631/x") == "http://h:8631/x"


# --- sources ---------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://example.com/a.pdf",
    "https://127.0.0.1/a.pdf",
    "https://10.0.1.15/a.pdf",
    "https://localhost/a.pdf",
])
def test_url_blocks_insecure_and_internal(url):
    with pytest.raises(sources.SourceError):
        sources.from_url(url)


def test_base64_data_url():
    raw = b"%PDF-1.4 test"
    data, name, _ = sources.from_base64("data:application/pdf;base64," + base64.b64encode(raw).decode(), "x.pdf")
    assert data == raw and name == "x.pdf"


def test_instances_from_env(monkeypatch):
    monkeypatch.setenv("PAPERLESS_PRIVAT_URL", "http://p:8000/")
    monkeypatch.setenv("PAPERLESS_PRIVAT_TOKEN", "t")
    monkeypatch.setenv("PAPERLESS_GEB_URL", "http://g:8000")  # no token → ignored
    inst = sources.paperless_instances()
    assert list(inst) == ["privat"] and inst["privat"].url == "http://p:8000"
    with pytest.raises(sources.SourceError):
        sources._pick(inst, "geb", "Paperless")


# --- main: limits + validation (printer mocked) -----------------------------
@pytest.fixture
def fake_printer(monkeypatch):
    calls = []

    def fake_print_job(pdf, name, **kw):
        calls.append((name, kw, len(PdfReader(io.BytesIO(pdf)).pages)))
        return {"job_id": 999, "job_state": "pending", "job_state_reasons": []}

    monkeypatch.setattr(ipp, "print_job", fake_print_job)
    return calls


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_print_defaults_a4(fake_printer):
    r = main.print_file(_b64(_pdf(2)), "a.pdf")
    assert r["success"] and r["job_id"] == 999 and r["pages"] == 2
    name, kw, pages = fake_printer[0]
    assert kw["media"] == "iso_a4_210x297mm" and kw["sides"] == "one-sided" and pages == 2


def test_page_limit(fake_printer):
    r = main.print_file(_b64(_pdf(30)), "a.pdf", copies=2)
    assert not r["success"] and "60" in r["error"] and not fake_printer
    r = main.print_file(_b64(_pdf(30)), "a.pdf", copies=2, allow_large_job=True)
    assert r["success"]
    r = main.print_file(_b64(_pdf(30)), "a.pdf", copies=2, page_ranges="1-10")
    assert r["success"] and r["pages"] == 10


def test_option_validation(fake_printer):
    assert not main.print_file(_b64(_pdf(1)), "a.pdf", duplex="both")["success"]
    assert not main.print_file(_b64(_pdf(1)), "a.pdf", paper="A3")["success"]
    assert not main.print_file(_b64(_pdf(1)), "a.pdf", copies=0)["success"]
    assert not fake_printer
