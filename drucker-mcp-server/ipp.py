"""Minimal IPP/2.0 client — just the operations the printer MCP needs.

Implements the binary encoding from RFC 8010 directly so the container needs
neither CUPS nor a printer driver: the Dell C2665dnf accepts application/pdf
natively over IPP. Supported operations: Print-Job, Get-Printer-Attributes,
Get-Jobs, Get-Job-Attributes, Cancel-Job.
"""

import os
import struct
from itertools import count
from typing import Any, Dict, List, Optional, Tuple

import httpx

PRINTER_URI = os.getenv("PRINTER_URI", "ipp://10.0.1.15:631/ipp/print")
REQUESTING_USER = os.getenv("PRINTER_USER", "claude-mcp")
TIMEOUT = float(os.getenv("PRINTER_TIMEOUT", "60"))

# Operation ids
OP_PRINT_JOB = 0x0002
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# Delimiter tags
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED = 0x05

# Value tags
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_OCTET = 0x30
TAG_DATETIME = 0x31
TAG_RESOLUTION = 0x32
TAG_RANGE = 0x33
TAG_BEG_COLLECTION = 0x34
TAG_END_COLLECTION = 0x37
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIME = 0x49
TAG_MEMBER_NAME = 0x4A

JOB_STATES = {
    3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped",
    7: "canceled", 8: "aborted", 9: "completed",
}
PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}
OPERATION_NAMES = {
    0x0002: "Print-Job", 0x0004: "Validate-Job", 0x0008: "Cancel-Job",
    0x0009: "Get-Job-Attributes", 0x000A: "Get-Jobs", 0x000B: "Get-Printer-Attributes",
}

_request_ids = count(1)


class IPPError(Exception):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(f"IPP status 0x{status:04x}{': ' + message if message else ''}")


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------
def _encode_value(tag: int, value: Any) -> bytes:
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.pack(">i", value)
    if tag == TAG_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == TAG_RANGE:
        return struct.pack(">ii", *value)
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _encode_attr(tag: int, name: str, value: Any) -> bytes:
    values = value if isinstance(value, list) else [value]
    out = b""
    for i, v in enumerate(values):
        n = name.encode("utf-8") if i == 0 else b""
        data = _encode_value(tag, v)
        out += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(data)) + data
    return out


def encode_request(
    operation: int,
    op_attrs: List[Tuple[int, str, Any]],
    job_attrs: Optional[List[Tuple[int, str, Any]]] = None,
    request_id: Optional[int] = None,
) -> bytes:
    rid = request_id if request_id is not None else next(_request_ids)
    out = struct.pack(">BBHI", 2, 0, operation, rid)
    out += bytes([TAG_OPERATION])
    base = [
        (TAG_CHARSET, "attributes-charset", "utf-8"),
        (TAG_LANGUAGE, "attributes-natural-language", "en"),
    ]
    for tag, name, value in base + op_attrs:
        out += _encode_attr(tag, name, value)
    if job_attrs:
        out += bytes([TAG_JOB])
        for tag, name, value in job_attrs:
            out += _encode_attr(tag, name, value)
    out += bytes([TAG_END])
    return out


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------
def _decode_value(tag: int, data: bytes) -> Any:
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.unpack(">i", data)[0]
    if tag == TAG_BOOLEAN:
        return data != b"\x00"
    if tag == TAG_RANGE:
        return struct.unpack(">ii", data)
    if tag == TAG_RESOLUTION:
        x, y, unit = struct.unpack(">iib", data)
        return (x, y, "dpi" if unit == 3 else "dpcm")
    if tag == TAG_DATETIME:
        y, mo, d, h, mi, s = struct.unpack(">HBBBBB", data[:7])
        return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"
    if tag == TAG_OCTET:
        return data.hex()
    if 0x10 <= tag <= 0x1F:  # out-of-band (unknown, no-value, …)
        return None
    return data.decode("utf-8", errors="replace")


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise IPPError(0xFFFF, "truncated IPP response")
        self.pos += n
        return chunk

    def peek(self) -> int:
        return self.data[self.pos]

    def read_attr(self) -> Tuple[int, str, bytes]:
        tag = self.read(1)[0]
        name_len = struct.unpack(">H", self.read(2))[0]
        name = self.read(name_len).decode("utf-8", errors="replace")
        val_len = struct.unpack(">H", self.read(2))[0]
        return tag, name, self.read(val_len)


def _read_collection(r: _Reader) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    member: Optional[str] = None
    while True:
        tag, _, data = r.read_attr()
        if tag == TAG_END_COLLECTION:
            return result
        if tag == TAG_MEMBER_NAME:
            member = data.decode("utf-8", errors="replace")
            continue
        value = _read_collection(r) if tag == TAG_BEG_COLLECTION else _decode_value(tag, data)
        if member is None:
            continue
        if member in result:
            prev = result[member]
            result[member] = (prev if isinstance(prev, list) else [prev]) + [value]
        else:
            result[member] = value


def decode_response(data: bytes) -> Tuple[int, List[Tuple[int, Dict[str, Any]]]]:
    """Return (status_code, [(group_tag, {name: value-or-list}), ...])."""
    r = _Reader(data)
    _major, _minor, status, _rid = struct.unpack(">BBHI", r.read(8))
    groups: List[Tuple[int, Dict[str, Any]]] = []
    current: Optional[Dict[str, Any]] = None
    last_name: Optional[str] = None
    while r.pos < len(data):
        tag = r.peek()
        if tag == TAG_END:
            break
        if tag < 0x10:  # delimiter → new group
            r.read(1)
            current = {}
            groups.append((tag, current))
            last_name = None
            continue
        tag, name, raw = r.read_attr()
        value = _read_collection(r) if tag == TAG_BEG_COLLECTION else _decode_value(tag, raw)
        if current is None:
            continue
        if name:
            current[name] = value
            last_name = name
        elif last_name is not None:  # additional value of a 1setOf
            prev = current[last_name]
            current[last_name] = (prev if isinstance(prev, list) else [prev]) + [value]
    return status, groups


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
def _http_uri(printer_uri: str) -> str:
    if printer_uri.startswith("ipps://"):
        return "https://" + printer_uri[len("ipps://"):]
    if printer_uri.startswith("ipp://"):
        rest = printer_uri[len("ipp://"):]
        host, _, path = rest.partition("/")
        if ":" not in host:
            host += ":631"
        return f"http://{host}/{path}"
    return printer_uri


def _call(operation: int, op_attrs, job_attrs=None, document: bytes = b"") -> List[Tuple[int, Dict[str, Any]]]:
    body = encode_request(operation, op_attrs, job_attrs) + document
    resp = httpx.post(
        _http_uri(PRINTER_URI),
        content=body,
        # The Dell's embedded server answers 406 to httpx's default
        # "Accept-Encoding: gzip, deflate" — it only speaks identity.
        headers={"Content-Type": "application/ipp", "Accept-Encoding": "identity"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    status, groups = decode_response(resp.content)
    if status >= 0x0100:
        msg = ""
        for tag, attrs in groups:
            if tag == TAG_OPERATION and attrs.get("status-message"):
                msg = attrs["status-message"]
        raise IPPError(status, msg)
    return groups


def _group(groups, tag: int) -> List[Dict[str, Any]]:
    return [attrs for t, attrs in groups if t == tag]


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def get_printer_attributes(requested: Optional[List[str]] = None) -> Dict[str, Any]:
    op = [(TAG_URI, "printer-uri", PRINTER_URI)]
    if requested:
        op.append((TAG_KEYWORD, "requested-attributes", requested))
    groups = _call(OP_GET_PRINTER_ATTRIBUTES, op)
    printers = _group(groups, TAG_PRINTER)
    return printers[0] if printers else {}


def print_job(
    pdf: bytes,
    job_name: str,
    copies: int = 1,
    sides: str = "one-sided",
    color_mode: str = "auto",
    media: str = "iso_a4_210x297mm",
) -> Dict[str, Any]:
    op = [
        (TAG_URI, "printer-uri", PRINTER_URI),
        (TAG_NAME, "requesting-user-name", REQUESTING_USER),
        (TAG_NAME, "job-name", job_name[:255]),
        (TAG_MIME, "document-format", "application/pdf"),
    ]
    job = [
        (TAG_INTEGER, "copies", copies),
        (TAG_KEYWORD, "sides", sides),
        (TAG_KEYWORD, "media", media),
        # The Dell firmware advertises both; set both so either path honours it.
        (TAG_KEYWORD, "print-color-mode", color_mode),
        (TAG_KEYWORD, "output-mode", color_mode),
    ]
    groups = _call(OP_PRINT_JOB, op, job, document=pdf)
    jobs = _group(groups, TAG_JOB)
    attrs = jobs[0] if jobs else {}
    return {
        "job_id": attrs.get("job-id"),
        "job_state": JOB_STATES.get(attrs.get("job-state"), attrs.get("job-state")),
        "job_state_reasons": _as_list(attrs.get("job-state-reasons")),
    }


_JOB_FIELDS = [
    "job-id", "job-name", "job-state", "job-state-reasons", "job-originating-user-name",
    "time-at-creation", "time-at-completed", "job-media-sheets-completed",
    "job-impressions-completed",
]


def _job_summary(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": attrs.get("job-id"),
        "name": attrs.get("job-name"),
        "state": JOB_STATES.get(attrs.get("job-state"), attrs.get("job-state")),
        "state_reasons": _as_list(attrs.get("job-state-reasons")),
        "user": attrs.get("job-originating-user-name"),
        "impressions_completed": attrs.get("job-impressions-completed"),
        "sheets_completed": attrs.get("job-media-sheets-completed"),
    }


def get_jobs(which: str = "not-completed", limit: int = 20) -> List[Dict[str, Any]]:
    op = [
        (TAG_URI, "printer-uri", PRINTER_URI),
        (TAG_NAME, "requesting-user-name", REQUESTING_USER),
        (TAG_INTEGER, "limit", limit),
        (TAG_KEYWORD, "which-jobs", which),
        (TAG_KEYWORD, "requested-attributes", _JOB_FIELDS),
    ]
    groups = _call(OP_GET_JOBS, op)
    return [_job_summary(a) for a in _group(groups, TAG_JOB)]


def get_job(job_id: int) -> Dict[str, Any]:
    op = [
        (TAG_URI, "printer-uri", PRINTER_URI),
        (TAG_INTEGER, "job-id", job_id),
        (TAG_NAME, "requesting-user-name", REQUESTING_USER),
        (TAG_KEYWORD, "requested-attributes", _JOB_FIELDS),
    ]
    groups = _call(OP_GET_JOB_ATTRIBUTES, op)
    jobs = _group(groups, TAG_JOB)
    return _job_summary(jobs[0]) if jobs else {"job_id": job_id, "state": "unknown"}


def cancel_job(job_id: int) -> None:
    op = [
        (TAG_URI, "printer-uri", PRINTER_URI),
        (TAG_INTEGER, "job-id", job_id),
        (TAG_NAME, "requesting-user-name", REQUESTING_USER),
    ]
    _call(OP_CANCEL_JOB, op)


def printer_status() -> Dict[str, Any]:
    a = get_printer_attributes([
        "printer-make-and-model", "printer-state", "printer-state-reasons",
        "printer-state-message", "printer-is-accepting-jobs", "marker-names",
        "marker-levels", "media-ready", "queued-job-count", "printer-info",
        "printer-location",
    ])
    names = _as_list(a.get("marker-names"))
    levels = _as_list(a.get("marker-levels"))
    return {
        "printer_uri": PRINTER_URI,
        "model": a.get("printer-make-and-model"),
        "state": PRINTER_STATES.get(a.get("printer-state"), a.get("printer-state")),
        "state_reasons": _as_list(a.get("printer-state-reasons")),
        "state_message": a.get("printer-state-message"),
        "accepting_jobs": a.get("printer-is-accepting-jobs"),
        "queued_jobs": a.get("queued-job-count"),
        "toner_percent": {n: lv for n, lv in zip(names, levels)},
        "media_ready": _as_list(a.get("media-ready")),
    }
