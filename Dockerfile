FROM python:3.12-slim
# Docker-Secrets: NAME_FILE=/run/secrets/... wird beim Start zu NAME geladen
COPY --chmod=0755 env-secrets.sh /usr/local/bin/env-secrets
WORKDIR /app

# DejaVu Sans Mono for text → PDF (umlauts, €, box drawing).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY drucker-mcp-server/ .

RUN useradd --system --uid 10001 app
USER app

ENV MCP_TRANSPORT=sse
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/env-secrets"]
CMD ["python", "main.py"]
