"""API configuration — settings sourced from environment variables."""
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings:
    db_path: str = os.environ.get(
        "ANANTA_DB_PATH", str(REPO_ROOT / ".state" / "drep.sqlite")
    )
    api_key: str = os.environ.get("ANANTA_API_KEY", "dev-key-change-me")
    evidence_dir: str = os.environ.get(
        "ANANTA_EVIDENCE_DIR", str(REPO_ROOT / ".state" / "evidence")
    )
    config_dir: str = os.environ.get("ANANTA_CONFIG_DIR", str(REPO_ROOT / "config"))
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()


def load_catalog_meta(config_dir=None) -> dict:
    """Load version/ref/hash from core_event_requirements.json."""
    path = Path(config_dir or settings.config_dir) / "core_event_requirements.json"
    with path.open() as fh:
        data = json.load(fh)
    return {
        "schema_version": data.get("version", "0.1.0"),
        "core_requirements_ref": data.get("core_requirements_ref", ""),
        "core_event_requirements_hash": data.get("core_event_requirements_hash", ""),
        "pilot_contract_ref": "contracts/pilot_event_contract.md#v0.1",
    }
