FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv==0.12.5
WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Optional, and private: without it the platform runs and a DB cutover stops with a clear reason.
ARG DB_OPERATOR_REF=""
RUN --mount=type=secret,id=github_token,required=false sh -ec '\
  if [ -n "$DB_OPERATOR_REF" ] && [ -f /run/secrets/github_token ]; then \
    apt-get update -qq && apt-get install -qq -y --no-install-recommends git; \
    export HOME=$(mktemp -d); \
    git config --global url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf https://github.com/; \
    uv pip install --python .venv/bin/python \
      "db-migration-adaptor @ git+https://github.com/SoftBank-2026-Hackathon-Foxtrot/db-migration-adaptor@$DB_OPERATOR_REF"; \
    rm -rf "$HOME"; \
  fi'

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY --from=build /srv/.venv ./.venv
COPY config ./config
USER app
ENV PATH=/srv/.venv/bin:$PATH
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD \
  python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["control-plane"]
