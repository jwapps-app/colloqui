# Colloqui

A fully self-hosted, sovereign team/family chat server. Your own Slack, with no
third-party services, no phone-home, and no vendor lock-in. FastAPI +
PostgreSQL + a no-framework web client, shipped as a single Docker image.

Spaces → channels (public/private) → messages, plus DMs and group DMs, threads,
reactions, @mentions, pinned messages, shared task checklists, reminders with an
iCal feed, file uploads with inline preview, full-text search, per-channel
notification levels, incoming webhooks, and link previews. Sign in with a
**passkey** (WebAuthn) or a classic **username + password**, your choice.

The client is an installable **PWA**: add it to your home screen on iOS or
desktop for an app-like window, offline message viewing, and push notifications
(see Web Push below). There is no separate native app to install or maintain.

Everything runs on hardware you control. The only outbound network call the
server ever makes is fetching a pasted URL's title for a link preview (and that
is SSRF-hardened, refusing private/loopback/cloud-metadata addresses).

## Quick start (self-host)

You need Docker with Compose. Then:

```sh
git clone <this-repo> colloqui && cd colloqui
cp .env.example .env
# edit .env, at minimum set POSTGRES_PASSWORD, COLLOQUI_IMAGE, RP_ID, ORIGIN
docker compose -f docker-compose.prod.yml up -d
```

Open `http://<host>:3300`. The **first account you register becomes the admin**.

> The database schema is created/upgraded automatically on startup (Alembic),
> so there's no manual migration step.

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

### Web Push (PWA notifications)

Notifications for the installed web app (Add to Home Screen), including on iOS
16.4+ and desktop Chrome/Firefox. **This works out of the box, with no
configuration.** On first boot the server generates a VAPID keypair and persists
it on the data volume (`uploads/vapid.json`), and `VAPID_SUBJECT` defaults to
your `ORIGIN`. We sign with our own keys and POST straight to the browser's push
service, with no third-party gateway. (The delivery hop itself necessarily routes
through the browser vendor's push service: Apple for iOS/Safari, Google for
Chrome, Mozilla for Firefox. There is no self-hosted web-push transport.)

To **manage your own keys** instead (e.g. to share one pair across instances),
set these env vars, which override the auto-generated pair:

| Variable             | What it is                                                          |
|----------------------|--------------------------------------------------------------------|
| `VAPID_PUBLIC_KEY`   | base64url public key (the app server key sent to browsers)          |
| `VAPID_PRIVATE_KEY`  | base64url private key. Never commit it.                             |
| `VAPID_SUBJECT`      | A contact URL, e.g. `mailto:you@example.com` (defaults to `ORIGIN`) |

Generate a pair with:

```bash
python -c "
import base64
from py_vapid import Vapid01
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
v = Vapid01(); v.generate_keys()
priv = base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32,'big')).rstrip(b'=').decode()
pub = base64.urlsafe_b64encode(v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).rstrip(b'=').decode()
print('VAPID_PRIVATE_KEY=' + priv); print('VAPID_PUBLIC_KEY=' + pub)"
```

The PWA subscribes via `POST /api/v1/push/subscribe` after the user grants
notification permission; dead subscriptions (404/410) are pruned automatically.
Notifications fire through a single `notify_user()` seam, and per-channel mute is
respected before a push is ever sent.

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
moving hosts, for example from a laptop to a NAS, carries everything over,
**including passkeys**, as long as the new host serves the **same domain**
(`RP_ID`/`ORIGIN` unchanged).

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
`POSTGRES_PASSWORD` may differ between hosts (the dump doesn't carry it) as long
as the new host's `db` and `api` agree.

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
