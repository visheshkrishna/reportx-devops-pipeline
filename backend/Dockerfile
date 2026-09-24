FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tesseract-ocr \
  && rm -rf /var/lib/apt/lists/*

# Install runtime deps from pyproject for consistency/caching
COPY pyproject.toml ./
RUN python - <<'PY'
import tomllib
from pathlib import Path
data = tomllib.loads(Path('pyproject.toml').read_text())
deps = data.get('project', {}).get('dependencies', [])
Path('requirements.txt').write_text('\n'.join(deps) + '\n')
print('Generated requirements.txt with', len(deps), 'dependencies')
PY
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Add non-root user
RUN useradd -ms /bin/bash appuser

# Copy app + migration assets
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Test stage: same runtime, plus dev deps and tests. Not shipped. ---
FROM base AS test
USER root
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
ENV PYTHONPATH=/app
CMD ["pytest", "-q"]
