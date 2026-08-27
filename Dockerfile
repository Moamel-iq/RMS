# A pinned runtime makes Render builds match the project's Python 3.14 policy.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# collectstatic imports production settings but does not contact the database.
# These values exist only for this build step; Render supplies real secrets at
# runtime through render.yaml and never receives a secret from this image.
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    DJANGO_SECRET_KEY=build-only-not-a-production-secret \
    DJANGO_ALLOWED_HOSTS=build.invalid \
    DB_NAME=build DB_USER=build DB_PASSWORD=build DB_HOST=build \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers ${WEB_CONCURRENCY:-2} --timeout 60 --access-logfile - --error-logfile -"]
