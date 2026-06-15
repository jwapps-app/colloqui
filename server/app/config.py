from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    # WebAuthn relying party: passkeys are cryptographically bound to rp_id,
    # and the browser origin must match origin exactly.
    rp_id: str = "localhost"
    rp_name: str = "Colloqui"
    origin: str = "http://localhost:3300"
    session_ttl_days: int = 30
    challenge_ttl_seconds: int = 300
    dev_mode: bool = False
    upload_dir: str = "/srv/uploads"
    max_file_size_mb: int = 25
    # Release version, stamped into the image at build time (the git tag, e.g.
    # "v1.0.12"). "dev" for local/source builds. Surfaced in the UI footer.
    app_version: str = "dev"

    # APNs (native iOS push). All optional — push is a silent no-op until these
    # are set, so the server runs fine without them. The server talks to Apple
    # directly with our own signing key (no third-party push gateway).
    apns_key: str = ""        # the .p8 private key contents (PEM), OR…
    apns_key_path: str = ""   # …a path to the .p8 file mounted into the container
    apns_key_id: str = ""     # the key's Key ID
    apns_team_id: str = ""    # Apple Developer Team ID
    apns_topic: str = ""      # the app's bundle id, e.g. co.jjrrr.colloqui
    apns_sandbox: bool = False  # true for dev/TestFlight builds (sandbox APNs)

    # Web Push (PWA notifications) via VAPID. All optional — web push is a silent
    # no-op until these are set. Keys are base64url (generate them with the
    # snippet in the README). We sign with our own keys and POST straight to the
    # browser's push service; the delivery hop necessarily touches Apple/Google/
    # Mozilla (there is no self-hosted web-push transport), but no third party of
    # ours is involved.
    vapid_public_key: str = ""    # base64url application server key, sent to clients
    vapid_private_key: str = ""   # base64url raw private key
    vapid_subject: str = ""       # contact URL, e.g. mailto:you@example.com


settings = Settings()
