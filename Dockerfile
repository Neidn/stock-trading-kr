# ── Stage 1: compile Python deps (TA-Lib 0.6.x bundles its own C source) ─────
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim

COPY --from=builder /usr/local/lib/python3.13/site-packages \
                    /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home trader
USER trader
WORKDIR /app
COPY src/ src/
COPY scripts/ scripts/

# Default: trading engine (override via command: in k8s manifests)
CMD ["python", "-m", "src.engine.main"]
