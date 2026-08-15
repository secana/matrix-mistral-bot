FROM debian:bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
  apt-get install -y --no-install-recommends ca-certificates && \
  rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd -r -m -s /usr/sbin/nologin bot

COPY --chown=bot:bot src/ ./src/

USER bot

# Install the script's dependencies into the image. Without this, `uv run`
# resolved and downloaded them on every container start.
ENV UV_CACHE_DIR=/home/bot/.cache/uv
RUN uv sync --locked --script src/bot.py

# --locked makes startup fail loudly if bot.py.lock ever drifts from the inline
# script metadata, instead of quietly resolving something new.
CMD ["uv", "run", "--locked", "--script", "src/bot.py"]
