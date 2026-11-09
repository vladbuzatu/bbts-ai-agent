# Production image for the Django backend. Built by GitHub Actions (pipeline.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt gunicorn

COPY backend/ .
# Settings must allow collectstatic with only a dummy secret key (see docs/DEPLOYMENT.md).
RUN DJANGO_SECRET_KEY=build-only-not-secret python manage.py collectstatic --noinput

RUN useradd -m app
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--access-logfile", "-"]
