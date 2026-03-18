# matrix-mistral-bot

A Matrix bot that uses Mistral AI with web search to answer questions. Mention the bot in any room it has joined to get a response with up-to-date information.

## Features

- Responds to @mentions in Matrix rooms
- Web search via DuckDuckGo
- Thread-aware: replies in threads when mentioned in a thread
- Conversation context from the current thread or last 20 room messages
- End-to-end encryption with automatic cross-signing bootstrap
- SAS verification support (both to-device and in-room for Element Desktop)
- No public-facing web interface — communicates only with the Matrix homeserver internally

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MATRIX_HOMESERVER` | yes | | Matrix homeserver URL (e.g. `https://matrix.example.com`) |
| `MATRIX_USER_ID` | yes | | Bot's Matrix user ID (e.g. `@bot:example.com`) |
| `MATRIX_PASSWORD` | yes | | Bot's Matrix password (used for login) |
| `MISTRAL_API_KEY` | yes | | Mistral API key |
| `MISTRAL_MODEL` | no | `mistral-large-latest` | Mistral model to use |
| `MAX_CONTEXT_MESSAGES` | no | `20` | Max messages to include as context |
| `SYSTEM_PROMPT` | no | *(built-in)* | Custom system prompt |
| `LOG_LEVEL` | no | `INFO` | Logging level |
| `MAX_TOOL_ROUNDS` | no | `3` | Max web search round-trips per query |
| `STORE_PATH` | no | `./crypto_store` | Path for E2E encryption keys and cross-signing seeds |

## Development

```bash
nix develop
uv run src/bot.py  # dependencies are resolved automatically from inline metadata
```

Lint, format, and build commands are available as Nix apps:

```bash
nix run .#lint          # run ruff linter
nix run .#format        # auto-format with ruff
nix run .#format-check  # check formatting without modifying files
nix run .#build         # build the Docker image
```

## Container image

Built automatically via GitHub Actions on git tags. Push a semver tag to trigger a release:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Image is pushed to `ghcr.io/secana/matrix-mistral-bot:<version>` (e.g. `0.1.0`, `0.1`).

```bash
docker build -t matrix-mistral-bot .
docker run --env-file .env matrix-mistral-bot
```
