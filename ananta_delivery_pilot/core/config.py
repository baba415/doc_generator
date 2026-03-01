from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.entity_registry import EntityRegistry
from app.schemas import Entity, SystemProfile
from app.utils import read_json


def _norm(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def infer_lane(vendor_of_record_id: str) -> str:
    if vendor_of_record_id == "guildgate":
        return "A"
    if vendor_of_record_id == "ananta_flows":
        return "B"
    return "C"


def infer_buyer_group(entity_id: str, entity_name: str = "") -> str:
    key = _norm(entity_id or entity_name)
    if "sumal" in key or "yale" in key:
        return "SUMAL"
    if "nycil" in key:
        return "NYCIL"
    if "bosh" in key:
        return "BOSH"
    return (entity_id or entity_name or "UNKNOWN").upper()


@dataclass(frozen=True)
class RuntimeConfig:
    root_dir: Path
    config_dir: Path
    state_dir: Path
    output_v2_dir: Path
    registry: EntityRegistry
    system_profile: SystemProfile
    coa_profiles: dict[str, Any]
    automation_thresholds: dict[str, Any]
    delivery_policies: dict[str, Any]
    drift_thresholds: dict[str, Any]

    @classmethod
    def load(cls, root_dir: Path) -> "RuntimeConfig":
        config_dir = root_dir / "config"
        registry = EntityRegistry.load_many(
            [
                config_dir / "entities.json",
                config_dir / "entities.local.json",
            ]
        )
        profile = SystemProfile.from_dict(read_json(config_dir / "system_profile.json"))
        coa_profiles = read_json(config_dir / "coa_profiles.json")
        thresholds_path = config_dir / "automation_thresholds.json"
        automation_thresholds = read_json(thresholds_path) if thresholds_path.exists() else {}
        delivery_policies_path = config_dir / "delivery_policies.json"
        delivery_policies = read_json(delivery_policies_path) if delivery_policies_path.exists() else {}
        drift_thresholds_path = config_dir / "drift_thresholds.json"
        drift_thresholds = read_json(drift_thresholds_path) if drift_thresholds_path.exists() else {}
        state_dir = root_dir / ".state"
        output_v2_dir = root_dir / "output_v2"
        state_dir.mkdir(parents=True, exist_ok=True)
        output_v2_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            root_dir=root_dir,
            config_dir=config_dir,
            state_dir=state_dir,
            output_v2_dir=output_v2_dir,
            registry=registry,
            system_profile=profile,
            coa_profiles=coa_profiles,
            automation_thresholds=automation_thresholds,
            delivery_policies=delivery_policies,
            drift_thresholds=drift_thresholds,
        )

    def resolve_entity(self, entity_id: str | None = None, entity_name: str | None = None) -> tuple[str, Entity]:
        if entity_id:
            return entity_id, self.registry.get(entity_id)
        if entity_name:
            resolved = self.registry.resolve_id(entity_name)
            if resolved:
                return resolved, self.registry.get(resolved)
        raise KeyError(f"Unable to resolve entity: entity_id={entity_id!r} entity_name={entity_name!r}")
