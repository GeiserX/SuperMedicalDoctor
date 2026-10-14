FROM python:3.12.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY supermedicaldoctor ./supermedicaldoctor
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH SMD_DATA=/data HOME=/tmp
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/smd.alive') >= 180)"
CMD ["smd", "run"]
