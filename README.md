# Colloqui

A fully self-hosted, sovereign team/family chat server — your own Slack, with no
third-party services, no phone-home, and no vendor lock-in. FastAPI +
PostgreSQL + a no-framework web client, shipped as a single Docker image.

Spaces → channels (public/private) → messages, plus DMs and group DMs, threads,
reactions, @mentions, pinned messages, shared task checklists, reminders with an
iCal feed, file uploads with inline preview, full-text search, per-channel
notification levels, incoming webhooks, and link previews. Sign in with a
**passkey** (WebAuthn) or a classic **username + password** — your choice.

Everything runs on hardware you control. The only outbound network call the
server ever makes is fetching a pasted URL's title for a link preview (and that
is SSRF-hardened — it refuses private/loopback/cloud-metadata addresses).

## Quick start (self-host)

You need Docker with Compose. Then:

```sh
git clone <this-repo> colloqui && cd colloqui
cp .env.example .env
# edit .env — at minimum set POSTGRES_PASSWORD, COLLOQUI_IMAGE, RP_ID, ORIGIN
docker compose -f docker-compose.prod.yml up -d
```

Open `http://<host>:3300`. The **first account you register becomes the admin**.

> The database schema is created/upgraded automatically on startup (Alembic) —
> there's no manual migration step.

On a **Synology NAS**, put the project under `/volume1/docker/colloqui` and set
`DATA_DIR=/volume1/docker/colloqui/data` in `.env` so the database, uploads, and
backups all live there.

## Configuration (`.env`)

| Variable           | What it is                                                                 |
|--------------------|----------------------------------------------------------------------------|
| `COLLOQUI_IMAGE`   | Published image to run, e.g. `ghcr.io/youruser/colloqui:latest`            |
| `POSTGRES_PASSWORD`| Database password (`openssl rand -base64 24`)                              |
| `API_PORT`         | Host port for the web UI + API (default `3300`)                           |
| `DATA_DIR`         | Host path for database + uploads + backups (default `./data`). On Synology, e.g. `/volume1/docker/colloqui/data` |
| `RP_ID`            | Domain passkeys are bound to. Use `localhost` for local, your apex domain in production |
| `ORIGIN`           | Exact browser origin, e.g. `https://chat.example.com`                      |
| `DEV_MODE`         | `true` exposes `/docs` (Swagger). Keep `false` in production.             |

### About passkeys vs. passwords and addresses

Passkeys are a browser security feature bound to **one origin** and only work in
a **secure context** (HTTPS, or `localhost`). That means:

- Over your public HTTPS domain → **passkeys and passwords both work**.
- Over a bare LAN IP like `http://192.168.1.50:3300` → **password only** (the
  browser blocks passkeys, the PWA install, and clipboard copy on plain `http`).

So a local/LAN instance is perfectly usable with passwords; reserve passkeys for
the HTTPS domain. **`RP_ID` is the apex domain on purpose** (e.g. `example.com`,
not `chat.example.com`) so every passkey stays valid even if you rename the
subdomain later.

## Public access with a Cloudflare Tunnel

Expose the server without opening any inbound ports:

1. Install `cloudflared` on the host and create a tunnel for your domain.
2. Route the hostname to the local app: ingress `https://chat.example.com` →
   `http://localhost:3300`.
3. Set `RP_ID` to your apex domain and `ORIGIN` to the full HTTPS URL in `.env`,
   then `docker compose -f docker-compose.prod.yml up -d` to apply.

Because the tunnel terminates TLS at Cloudflare and the app trusts proxy headers
(`--proxy-headers`), the server sees the correct HTTPS origin.

## Updating

```sh
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Pin a specific release by setting `COLLOQUI_IMAGE=ghcr.io/youruser/colloqui:v1.2.0`.

## Backups

The `backup` container writes a daily snapshot to `./data/backups`: a compressed
Postgres dump (`db-*.dump`) and a tarball of uploaded files
(`uploads-*.tar.gz`), keeping the most recent 14 of each. Run one on demand:

```sh
docker compose -f docker-compose.prod.yml exec backup sh /backup.sh
```

### Restore

```sh
# database (custom-format dump)
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U app -d app --clean --if-exists < data/backups/db-YYYYMMDD-HHMMSS.dump
# uploaded files
tar xzf data/backups/uploads-YYYYMMDD-HHMMSS.tar.gz -C data/
```

## Moving an existing instance to a new host

Because all state lives in Postgres + the uploads directory (never in the image),
moving hosts — e.g. from a laptop to a NAS — carries everything over, **including
passkeys**, as long as the new host serves the **same domain** (`RP_ID`/`ORIGIN`
unchanged).

On the **old** host, take a fresh snapshot and copy it over:

```sh
docker compose exec backup sh /backup.sh          # or docker-compose.prod.yml
scp data/backups/db-*.dump  data/backups/uploads-*.tar.gz  newhost:~/colloqui/data/backups/
```

On the **new** host:

```sh
cp .env.example .env        # keep RP_ID / ORIGIN identical to the old host
docker compose -f docker-compose.prod.yml up -d db        # start only Postgres
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U app -d app --clean --if-exists < data/backups/db-<stamp>.dump
tar xzf data/backups/uploads-<stamp>.tar.gz -C data/
docker compose -f docker-compose.prod.yml up -d           # start everything
```

Then repoint your Cloudflare Tunnel from the old host to the new one. Since the
domain is unchanged, existing passkeys keep working with no re-enrollment.
`POSTGRES_PASSWORD` may differ between hosts — the dump doesn't carry it — as
long as the new host's `db` and `api` agree.

## Development

Run the stack from source (builds locally instead of pulling):

```sh
cp .env.example .env        # RP_ID=localhost  ORIGIN=http://localhost:3300
docker compose up -d --build
```

Run the test suite against a throwaway database:

```sh
cd server && pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+asyncpg://app:<pw>@localhost:5432/app python -m pytest -q
```

GitHub Actions runs the tests on every push and publishes the multi-arch image
to `ghcr.io` when a `v*` tag is pushed.

## Releasing

```sh
git tag v1.0.0
git push origin v1.0.0      # triggers the multi-arch build → ghcr.io/<owner>/<repo>
```

The first push makes the package private by default; flip it to public in the
repo's **Packages** settings if you want others to pull it.
