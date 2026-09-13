# Build context is the repo root so the schema file can be shared with postgres.
FROM python:3.12-slim

# pygrib needs the ECMWF eccodes library. The manylinux wheel usually bundles
# it, but installing the system package keeps the image working regardless.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libeccodes0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt

COPY db/init/01_schema.sql db/init/01_schema.sql
COPY pipeline/ pipeline/

ENV PYTHONUNBUFFERED=1 BRONZE_DIR=/data/bronze
VOLUME /data/bronze

ENTRYPOINT ["python", "-m", "pipeline"]
