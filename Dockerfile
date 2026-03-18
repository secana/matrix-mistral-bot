FROM debian:bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends libolm-dev gcc python3-dev cmake ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ENV CMAKE_POLICY_VERSION_MINIMUM=3.5

WORKDIR /app

COPY src/ ./src/

RUN useradd -r -m -s /usr/sbin/nologin bot
USER bot

CMD ["uv", "run", "src/bot.py"]
