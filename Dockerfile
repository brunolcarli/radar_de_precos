FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PROGRESS_BAR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app app

COPY requirements-api.txt ./

RUN pip install \
    --no-cache-dir \
    --progress-bar off \
    --disable-pip-version-check \
    -r requirements-api.txt

COPY --chown=app:app api_olx_carros.py dashboard_olx.html ./

USER app

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "2", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "api_olx_carros:app"]