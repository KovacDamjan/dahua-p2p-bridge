FROM rust:1.85-bookworm AS rust-builder

WORKDIR /build
COPY rust-engine ./
RUN cargo build --release

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=rust-builder /build/target/release/dh-p2p /usr/local/bin/dh-p2p-engine

RUN useradd --system --uid 10001 bridge \
    && mkdir -p /app/data \
    && chown -R bridge:bridge /app \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/api/health')"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
