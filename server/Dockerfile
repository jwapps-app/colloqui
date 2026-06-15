FROM python:3.13-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
