FROM python:3.11-slim AS base

# CACHEBUST forces Docker to re-fetch the git source on every build.
# Pass with: docker compose build --build-arg CACHEBUST=$(date +%s)
ARG CACHEBUST=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY data/ data/

RUN pip install --no-cache-dir ".[ai]"

ENTRYPOINT ["switching"]
CMD ["list-detectors"]
