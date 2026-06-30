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

    # Native iOS push (APNs) is delivered via the self-hosted push-relay: one
    # shared signing key + central metrics for all our apps. Silent no-op until
    # the relay settings are configured. The relay does the Apple signing, so the
    # .p8 key no longer lives here.
    push_relay_url: str = ""       # e.g. http://192.168.1.42:8088 or https://push.<domain>
    push_relay_api_key: str = ""   # the relay's API_KEY_COLLOQUI — secret, env only
    apns_topic: str = "com.jworthington.colloqui"  # bundle id the relay routes on

    # Legacy direct-to-Apple APNs settings — unused now that push goes through the
    # relay. Kept so existing env files don't error; safe to remove later.
    apns_key: str = ""
    apns_key_path: str = ""
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_sandbox: bool = False

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
