# The hosted Teacher (Fly.io app "airistotle"; see fly.toml and README "Hosting").
#
# Only the app: Python 3.12, the locked dependencies (uv.lock) and the code. No Node.js and no
# Pyodide, so labs are not available in this image (the app says so where labs are used). No
# data and no secrets: .dockerignore keeps .env*, the database, the data folders and the tests
# out; the keys and TEACHER_PASSWORD come from the host's secrets at run time.
#
# The data lives in $TEACHER_HOME=/data (a volume). The server runs as the unprivileged user
# "teacher"; the entrypoint starts as root only to give /data to that user (a new volume is
# owned by root). `teacher serve` refuses to start on 0.0.0.0 without $TEACHER_PASSWORD.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# The dependencies first (a layer that only changes with uv.lock), then the project itself.
# The project is installed editable (uv's default): teacher.db.migrate finds migrations/ next
# to src/, i.e. /app/migrations.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
    && useradd --system --user-group --uid 10001 --home-dir /data --no-create-home teacher \
    && mkdir -p /data \
    && chown teacher:teacher /data
# Executable in the build context (git keeps the mode); no `COPY --chmod`, which needs BuildKit.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# FORWARDED_ALLOW_IPS: uvicorn trusts X-Forwarded-For/-Proto from any address, because the only
# way in is the host's proxy (Fly's), which terminates HTTPS. Without it, behind the proxy
# every request would look like plain http and the session cookie would not be marked Secure.
ENV PATH="/app/.venv/bin:$PATH" \
    TEACHER_HOME=/data \
    FORWARDED_ALLOW_IPS="*"

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["teacher", "serve", "--host", "0.0.0.0", "--port", "8080"]
