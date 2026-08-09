FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-impress poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 qbr

WORKDIR /app
COPY pyproject.toml README.md ./
COPY apps ./apps
COPY packages ./packages
COPY skills ./skills
# The runtime image includes the optional vector backend so switching from FTS
# to hybrid retrieval is a configuration change, not a different image build.
RUN pip install '.[vector]'

RUN mkdir -p /data/sqlite /data/objects \
    && chown -R qbr:qbr /data /app
USER qbr

EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
