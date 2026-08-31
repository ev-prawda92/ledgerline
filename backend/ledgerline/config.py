"""Runtime configuration.

Secrets are read from the environment, never committed.  OAuth tokens for
connected sources do NOT belong here or in a database column -- they go to a
KMS.  See docs/plan.md, Stage 1.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline"
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""
    # Reconciliation runs are replayable; bump when match rules change so a run
    # records which ruleset produced it.
    ruleset_version: str = "2026.08.31"


settings = Settings()
