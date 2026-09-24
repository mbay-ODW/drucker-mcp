# drucker-mcp

MCP-Server zum Drucken auf dem Hausdrucker **Dell C2665dnf Color MFP**. Er arbeitet mit
jedem IPP-Everywhere-Drucker, der PDF annimmt.

Der Server enthält einen eigenen, minimalen IPP-Client (`ipp.py`). Du brauchst also weder
CUPS noch einen Treiber. Jede Eingabe wird in PDF umgewandelt und per IPP `Print-Job`
direkt an den Drucker geschickt.

## Tools

| Tool | Zweck |
|---|---|
| `print_file` | Datei direkt als base64 drucken |
| `print_url` | Datei von einer öffentlichen `https://`-URL drucken (interne Ziele gesperrt, SSRF-Schutz) |
| `print_paperless_document` | Paperless-Dokument per ID drucken, Instanzen `privat` / `geb` |
| `print_nextcloud_file` | Nextcloud-Datei per Pfad drucken, Instanzen `privat` / `geb` |
| `printer_status` | Zustand, Fehlerursachen, Toner, Warteschlange, konfigurierte Quellen |
| `list_print_jobs` / `get_print_job` / `cancel_print_job` | Warteschlange und Verlauf |

Druckoptionen: `copies`, `duplex` (`off`/`long-edge`/`short-edge`), `color`
(`auto`/`color`/`monochrome`), `paper` (Standard **A4**, obwohl der Drucker selbst auf Letter
eingestellt ist), `page_ranges`, `job_name`.

Formate: PDF, Bilder (JPG/PNG/GIF/TIFF/WebP/HEIC, werden auf A4 eingepasst und automatisch
gedreht) sowie reiner Text. Office-Dateien werden bewusst nicht unterstützt, denn dafür wäre
LibreOffice nötig und das Image würde rund 1 GB groß.

**Limit:** höchstens 50 Seiten × Kopien pro Auftrag (`MAX_PAGES_PER_JOB`). Für mehr muss
`allow_large_job=true` gesetzt werden, und das nur, wenn der Nutzer es ausdrücklich will.

## Konfiguration

| Env | Bedeutung |
|---|---|
| `PRINTER_URI` | Standard `ipp://10.0.1.15:631/ipp/print` |
| `PAPERLESS_<NAME>_URL` / `_TOKEN` | eine Paperless-Instanz je `<NAME>` |
| `NEXTCLOUD_<NAME>_URL` / `_USER` / `_PASSWORD` | eine Nextcloud-Instanz je `<NAME>` (App-Passwort) |
| `MCP_API_KEY`, `OIDC_*`, `OAUTH_ISSUER`, `MCP_SERVER_URL` | Auth wie bei signal-mcp |

Die Auth ist **fail-closed**: Ist weder `MCP_API_KEY` noch das OIDC-Tripel gesetzt, startet der
Server nicht. Die einzige Ausnahme ist `ALLOW_NO_AUTH=true`, gedacht nur für die lokale Entwicklung.

## Deployment (TrueNAS / Portainer)

1. Den Build-Kontext auf den Host kopieren und dort `sudo docker build -t drucker-mcp:latest .`
   ausführen. Das GHCR-Package ist privat, deshalb wird lokal gebaut.
2. Portainer Local Stack `drucker-mcp` aus `docker-compose.yml` anlegen. Stack-Env:
   `DOMAIN`, `MCP_API_KEY`, `OIDC_CLIENT_SECRET`, `PAPERLESS_{PRIVAT,GEB}_TOKEN`,
   `NEXTCLOUD_{PRIVAT,GEB}_{USER,PASSWORD}`.
3. `traefik/drucker-mcp.yml` nach `/mnt/apps/traefik/data/traefik2/rules/` legen. Traefik
   lädt die Datei selbst neu.
4. Den Block aus `authelia/drucker-mcp-client.yml` (mit bcrypt-Hash) in Authelias
   `configuration.yml` eintragen, dann `authelia validate-config` und `docker restart authelia`.
5. In claude.ai den Connector `https://drucker-mcp.<domain>/mcp` hinzufügen.

**Update:** neu bauen und anschließend den Stack in Portainer mit `pullImage=false` neu deployen.

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests
```
