FROM debian:trixie-slim AS base

# Upgrade at build time so a rebuild picks up Debian security fixes without
# waiting for a new base image tag.
RUN apt-get update && \
  apt-get upgrade -y && \
  apt-get install -y --no-install-recommends ca-certificates && \
  rm -rf /var/lib/apt/lists/*


FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
  UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  UV_NO_CACHE=1

WORKDIR /build
COPY src/bot.py src/bot.py.lock ./

# --locked fails the build if bot.py.lock drifts from the inline script metadata.
RUN uv export --locked --script bot.py --no-header > requirements.txt && \
  uv venv --python 3.12 /opt/venv && \
  VIRTUAL_ENV=/opt/venv uv pip install --require-hashes -r requirements.txt

# The bot never calls pip, and pip's vendored copies of urllib3, msgpack and
# friends would otherwise show up in every image scan.
RUN rm -rf /opt/python/*/bin/pip* \
  /opt/python/*/lib/python3.*/site-packages/pip \
  /opt/python/*/lib/python3.*/site-packages/pip-*.dist-info \
  /opt/python/*/lib/python3.*/ensurepip


FROM base

WORKDIR /app

RUN useradd -r -m -s /usr/sbin/nologin bot

COPY --from=builder /opt/python /opt/python
COPY --from=builder /opt/venv /opt/venv
COPY --chown=bot:bot src/ ./src/

USER bot

ENV PATH=/opt/venv/bin:$PATH

CMD ["python", "src/bot.py"]
