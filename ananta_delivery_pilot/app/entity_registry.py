from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from .schemas import BankAccount, Entity
from .utils import read_json


def _normalized(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


@dataclass(frozen=True)
class EntityRegistry:
    entities: Dict[str, Entity]
    by_normalized_name: Dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "EntityRegistry":
        return cls.load_many([path])

    @classmethod
    def load_many(cls, paths: Iterable[Path]) -> "EntityRegistry":
        entities: Dict[str, Entity] = {}

        for path in paths:
            if not path.exists():
                continue
            payload = read_json(path)
            entities_payload = payload.get("entities", {}) if isinstance(payload, dict) else {}
            for entity_id, raw in entities_payload.items():
                if not isinstance(raw, dict):
                    continue
                bank_payload = raw.get("bank")
                bank = BankAccount.from_dict(bank_payload) if isinstance(bank_payload, dict) else None
                entity = Entity.from_dict(
                    {
                        **raw,
                        "entity_id": entity_id,
                        "bank": {
                            "bank_name": bank.bank_name,
                            "account_name": bank.account_name,
                            "account_number": bank.account_number,
                            "currency": bank.currency,
                            "sort_code": bank.sort_code,
                        }
                        if bank
                        else None,
                    }
                )
                entities[entity_id] = entity

        by_norm: Dict[str, str] = {}
        for entity_id, entity in entities.items():
            for name in _names_for_index(entity):
                key = _normalized(name)
                if key:
                    by_norm[key] = entity_id

        return cls(entities=entities, by_normalized_name=by_norm)

    def get(self, entity_id: str) -> Entity:
        entity = self.entities.get(entity_id)
        if not entity:
            raise KeyError(f"Unknown entity_id: {entity_id}")
        return entity

    def resolve_id(self, name: str) -> Optional[str]:
        key = _normalized(name)
        if not key:
            return None
        if key in self.by_normalized_name:
            return self.by_normalized_name[key]
        for candidate_key, entity_id in self.by_normalized_name.items():
            if candidate_key and candidate_key in key:
                return entity_id
        return None


def _names_for_index(entity: Entity) -> Iterable[str]:
    names = [entity.name]
    names.extend(entity.aliases or [])
    return [name for name in names if name]
