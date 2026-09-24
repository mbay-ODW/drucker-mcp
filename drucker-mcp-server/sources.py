"""Fetch printable files from the supported sources.

- inline base64 content
- public HTTPS URLs (private/internal targets blocked against SSRF)
- Paperless-ngx instances   (PAPERLESS_<NAME>_URL / PAPERLESS_<NAME>_TOKEN)
- Nextcloud instances       (NEXTCLOUD_<NAME>_URL / _USER / _PASSWORD)

Every fetch returns (data, filename, content_type) and is capped at MAX_BYTES.
"""

import base64
import binascii
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import Dict, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

MAX_BYTES = int(os.getenv("MAX_FILE_MB", "50")) * 1024 * 1024
HTTP_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "60"))

Fetched = Tuple[bytes, str, str]


class SourceError(ValueError):
    pass


# --------------------------------------------------------------------------
# Instance configuration from env
# --------------------------------------------------------------------------
@dataclass
class PaperlessInstance:
    url: str
    token: str


@dataclass
class NextcloudInstance:
    url: str
    user: str
    password: str


def _instances(prefix: str, suffixes: Tuple[str, ...]) -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    pattern = re.compile(rf"^{prefix}_([A-Z0-9]+)_({'|'.join(suffixes)})$")
    for key, value in os.environ.items():
        m = pattern.match(key)
        if m and value:
            found.setdefault(m.group(1).lower(), {})[m.group(2).lower()] = value
    return {name: cfg for name, cfg in found.items() if all(s.lower() in cfg for s in suffixes)}


def paperless_instances() -> Dict[str, PaperlessInstance]:
    return {
        n: PaperlessInstance(c["url"].rstrip("/"), c["token"])
        for n, c in _instances("PAPERLESS", ("URL", "TOKEN")).items()
    }


def nextcloud_instances() -> Dict[str, NextcloudInstance]:
    return {
        n: NextcloudInstance(c["url"].rstrip("/"), c["user"], c["password"])
        for n, c in _instances("NEXTCLOUD", ("URL", "USER", "PASSWORD")).items()
    }


def _pick(instances: Dict, name: str, kind: str):
    if not instances:
        raise SourceError(f"Keine {kind}-Instanz konfiguriert.")
    if not name:
        if len(instances) == 1:
            return next(iter(instances.values()))
        raise SourceError(f"Bitte {kind}-Instanz angeben: {', '.join(sorted(instances))}")
    inst = instances.get(name.lower())
    if not inst:
        raise SourceError(f"Unbekannte {kind}-Instanz '{name}'. Verfügbar: {', '.join(sorted(instances))}")
    return inst


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _read_capped(resp: httpx.Response) -> bytes:
    length = resp.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BYTES:
        raise SourceError(f"Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB.")
    buf = bytearray()
    for chunk in resp.iter_bytes():
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise SourceError(f"Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB.")
    return bytes(buf)


def _filename_from(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return unquote(m.group(1)).strip()
    return fallback


def _content_type(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip()


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def from_base64(content_base64: str, filename: str) -> Fetched:
    raw = content_base64.strip()
    if raw.startswith("data:") and "," in raw:  # tolerate data: URLs
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as e:
        raise SourceError(f"Ungültiges base64: {e}") from e
    if len(data) > MAX_BYTES:
        raise SourceError(f"Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB.")
    return data, filename or "dokument", ""


def _assert_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SourceError(f"Host '{host}' nicht auflösbar: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise SourceError(
                f"URL zeigt auf eine interne Adresse ({ip}) — aus Sicherheitsgründen gesperrt. "
                "Interne Dateien bitte über Paperless oder Nextcloud drucken."
            )


def from_url(url: str) -> Fetched:
    current = url
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        for _ in range(6):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not parsed.hostname:
                raise SourceError("Nur https://-URLs werden unterstützt.")
            _assert_public_host(parsed.hostname)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    current = urljoin(current, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise SourceError(f"Download fehlgeschlagen: HTTP {resp.status_code}")
                data = _read_capped(resp)
                name = _filename_from(resp, os.path.basename(parsed.path) or parsed.hostname)
                return data, name, _content_type(resp)
    raise SourceError("Zu viele Weiterleitungen.")


def from_paperless(document_id: int, instance: str = "", original: bool = False) -> Fetched:
    inst: PaperlessInstance = _pick(paperless_instances(), instance, "Paperless")
    headers = {"Authorization": f"Token {inst.token}", "Accept": "application/json"}
    with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as client:
        meta = client.get(f"{inst.url}/api/documents/{document_id}/")
        if meta.status_code == 404:
            raise SourceError(f"Paperless-Dokument {document_id} nicht gefunden.")
        if meta.status_code != 200:
            raise SourceError(f"Paperless-API: HTTP {meta.status_code}")
        title = meta.json().get("title") or f"paperless-{document_id}"
        # Default download = archived (OCR'd) PDF when present, else the original.
        params = {"original": "true"} if original else None
        with client.stream("GET", f"{inst.url}/api/documents/{document_id}/download/", params=params) as resp:
            if resp.status_code != 200:
                raise SourceError(f"Paperless-Download: HTTP {resp.status_code}")
            data = _read_capped(resp)
            return data, _filename_from(resp, title), _content_type(resp)


def from_nextcloud(path: str, instance: str = "") -> Fetched:
    inst: NextcloudInstance = _pick(nextcloud_instances(), instance, "Nextcloud")
    clean = "/".join(p for p in path.strip().split("/") if p and p not in (".", ".."))
    if not clean:
        raise SourceError("Bitte einen Dateipfad angeben.")
    url = f"{inst.url}/remote.php/dav/files/{quote(inst.user)}/{quote(clean)}"
    with httpx.Client(timeout=HTTP_TIMEOUT, auth=(inst.user, inst.password)) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code == 404:
                raise SourceError(f"Nextcloud-Datei '{clean}' nicht gefunden.")
            if resp.status_code != 200:
                raise SourceError(f"Nextcloud: HTTP {resp.status_code}")
            data = _read_capped(resp)
            return data, os.path.basename(clean), _content_type(resp)
