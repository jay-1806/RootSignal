# One image for every demo component; docker-compose picks the command per service.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY demo ./demo
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "demo.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
