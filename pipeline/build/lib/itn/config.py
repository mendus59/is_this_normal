"""Settings read from the environment. Missing values fail only when used."""

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "schema" / "migrations"

# Earliest effective date for scored artifacts.
SCOPE_START = "2000-01-01"
# A law is contentious if any recorded final-passage vote fell below this yes share.
CONTENTIOUS_THRESHOLD = 2 / 3


class MissingSetting(RuntimeError):
    pass


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingSetting(f"{name} is not set. See SETUP.md for where it comes from.")
    return value


@dataclass(frozen=True)
class R2Settings:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls) -> "R2Settings":
        names = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise MissingSetting(
                f"Not set: {', '.join(missing)}. These must be repository secrets "
                "(not variables or environment secrets). See SETUP.md."
            )
        return cls(*(os.environ[n] for n in names))


def database_url() -> str:
    return require("DATABASE_URL")
