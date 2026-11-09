FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY aegis ./aegis
RUN pip install .
COPY alembic.ini ./
COPY alembic ./alembic
RUN useradd --system --uid 10001 --no-create-home aegis
USER aegis
EXPOSE 8000
# each compose service overrides the command; SIGTERM triggers a graceful shutdown in every entry point
STOPSIGNAL SIGTERM
CMD ["python", "-m", "aegis.services.api"]
