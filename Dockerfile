# Railpack builds this project happily, but it cannot install the Alpaca CLI,
# and the agent refuses to start without it -- correctly. The CLI is the
# reconciler: order lookups go through it so confirmation arrives over a
# different transport than the one that submitted, and a transport-specific
# failure cannot both lose an order and lose the evidence of it. The hackathon
# also requires that the MCP server or the CLI be used, and a deploy without
# either would make that claim untrue.
FROM python:3.12-slim

# Pinned. v0.0.14 is the release whose --legs behaviour we verified; the CLI is
# stamped "alpha preview, commands may change without notice between releases",
# so floating to latest would be trusting an unread changelog with the order
# path.
ARG ALPACA_CLI_VERSION=0.0.14

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL -o /tmp/alpaca.tar.gz \
      "https://github.com/alpacahq/cli/releases/download/v${ALPACA_CLI_VERSION}/cli_${ALPACA_CLI_VERSION}_linux_amd64.tar.gz" \
 && tar -xzf /tmp/alpaca.tar.gz -C /tmp alpaca \
 && install -m755 /tmp/alpaca /usr/local/bin/alpaca \
 && rm -rf /tmp/alpaca.tar.gz /tmp/alpaca /var/lib/apt/lists/* \
 && apt-get purge -y curl && apt-get autoremove -y \
 && alpaca version

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a source change does not reinstall the world.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN uv sync --locked --no-dev

# The journal lives on the mounted volume, not in the image.
ENV UNDERWRITER_JOURNAL=/data/underwriter.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080
# Invoke the installed entry point directly rather than through `uv run`,
# which re-syncs the environment on every start -- it was reinstalling ruff and
# mypy before serving a single request. Restarts are not rare here: managed
# host migrations are mandatory, so start-up cost is paid repeatedly and during
# market hours.
CMD ["/app/.venv/bin/underwriter-serve"]
