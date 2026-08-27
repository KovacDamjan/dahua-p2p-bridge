FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --system --uid 10001 bridge && mkdir -p /app/data && chown -R bridge:bridge /app
USER bridge

EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]

