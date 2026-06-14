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


settings = Settings()
