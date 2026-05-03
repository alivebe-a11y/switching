FROM python:3.11-slim AS base

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY data/ data/

RUN pip install --no-cache-dir ".[ai]"

ENTRYPOINT ["switching"]
CMD ["list-detectors"]
