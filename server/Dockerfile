# Base image pinned by manifest-list digest (covers amd64 + arm64) so every
# build starts from the identical OS layer. To take base-image security
# updates, bump the digest deliberately:
#   docker buildx imagetools inspect python:3.13-slim | grep Digest
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

WORKDIR /srv

# Dependencies come from the hash-pinned lock, never from the loose ranges in
# requirements.txt: every wheel must match a recorded SHA-256 or the build
# fails, and a rebuild months later installs exactly the same set.
# Regenerate after editing requirements.txt (run from server/):
#   docker run --rm -v "$PWD:/w" -w /w python:3.13-slim sh -c \
#     'pip install -q pip-tools && pip-compile --generate-hashes --strip-extras \
#      --allow-unsafe -o requirements.lock requirements.txt'
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# Release version (the git tag), passed by CI and surfaced in the UI footer.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

RUN useradd --no-create-home svc
USER svc

# Apply DB migrations, then serve. `alembic upgrade head` is idempotent — it
# stamps the existing production DB on first run and applies new revisions after.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
