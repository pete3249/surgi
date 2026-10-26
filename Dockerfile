# Build:  docker build -t ready-for-surgery .
# Run:    docker run --rm -p 8000:8000 --env-file .env ready-for-surgery
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Install dependencies first: Docker caches this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the app needs. Never copy .env into the image (the .dockerignore also blocks it).
COPY app ./app
COPY index.html core-items.json procedures.json ./
# The approved plans (made with: python -m app.make_plans). Drafts are not copied.
COPY plans ./plans

RUN useradd --create-home appuser
USER appuser

# Hosts like Render tell us which port to use through $PORT. Shell form so ${PORT} is expanded.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
