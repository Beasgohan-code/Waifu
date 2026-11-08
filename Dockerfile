# The frontend is built separately and only its output enters the runtime image.
FROM node:22.23.4-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS frontend
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11.18-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39 AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUMMON_WEB_DIST=/app/web/dist \
    PATH=/app/.venv/bin:$PATH

COPY pyproject.toml uv.lock README.md ./
COPY summon/ ./summon/
# Bootstrap uv separately with distribution hashes; project dependencies and
# isolated build requirements are then verified against uv.lock/pyproject.toml.
RUN printf '%s\n' 'uv==0.13.7 \
    --hash=sha256:2c9bdc4a872b60a93c5f26c0791089ea11585a4f9d3b3c017d78eb8487505e25 \
    --hash=sha256:a90f5d95716ab83091f0f73a8e7ec64e557abf2a28537fcd719b2ac88e6f725a \
    --hash=sha256:08f16fd463926786b702b497473511deae4b3e17a2fa6d22180950d6059a120d' > /tmp/uv-requirements.txt \
    && /usr/local/bin/python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/uv-requirements.txt \
    && uv sync --locked --no-dev --no-editable --python /usr/local/bin/python --no-python-downloads \
    && uv pip check --python /app/.venv/bin/python \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /tmp/uv-requirements.txt /root/.cache

COPY --from=frontend /build/dist/ /app/web/dist/
RUN groupadd --system --gid 10001 summon \
    && useradd --system --uid 10001 --gid summon --home-dir /app --shell /usr/sbin/nologin summon
USER 10001:10001
EXPOSE 10000
# Render supplies PORT; other environments default to 8000. Run summon-bot as
# a separate, singleton Wzgram worker; its MongoDB lease coordinates restarts.
CMD ["summon-api"]
