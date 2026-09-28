FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY rootsignal ./rootsignal
RUN pip install --no-cache-dir .

EXPOSE 8000
# Alembic migrations run automatically on startup (AUTO_MIGRATE=true).
CMD ["uvicorn", "rootsignal.main:app", "--host", "0.0.0.0", "--port", "8000"]
