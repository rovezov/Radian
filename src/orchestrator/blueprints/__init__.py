from __future__ import annotations

import json
from pathlib import Path

from core.atomic_io import atomic_write_json
from src.orchestrator.blueprints.base import BlueprintTemplate, GenericBlueprint
from src.orchestrator.schemas import PlanStep

_BUILTIN_BLUEPRINTS: dict[str, BlueprintTemplate] = {}

_DATA_DIR = Path("data")
_CUSTOM_BLUEPRINTS_PATH = _DATA_DIR / "orchestrator_blueprints.json"


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_custom_blueprint_defs() -> list[dict]:
    try:
        if not _CUSTOM_BLUEPRINTS_PATH.exists():
            return []
        raw = json.loads(_CUSTOM_BLUEPRINTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [x for x in raw if isinstance(x, dict)]
    except Exception:
        return []


def _save_custom_blueprint_defs(defs: list[dict]) -> None:
    _ensure_data_dir()
    # Atomic write avoids partial/truncated JSON on process interruption.
    atomic_write_json(str(_CUSTOM_BLUEPRINTS_PATH), defs, indent=2)


def _build_generic_blueprint(raw: dict) -> GenericBlueprint | None:
    try:
        name = str(raw.get("name") or "").strip().lower()
        if not name:
            return None
        display_name = str(raw.get("display_name") or name).strip()
        description = str(raw.get("description") or "").strip()
        trigger_keywords = [str(k).strip().lower() for k in (raw.get("trigger_keywords") or []) if str(k).strip()]
        steps_raw = raw.get("steps") or []
        steps = [PlanStep.model_validate(s) for s in steps_raw if isinstance(s, dict)]
        if not steps:
            return None
        return GenericBlueprint(
            name=name,
            display_name=display_name,
            description=description,
            trigger_keywords=trigger_keywords,
            steps=steps,
        )
    except Exception:
        return None


def _custom_blueprints_map() -> dict[str, BlueprintTemplate]:
    out: dict[str, BlueprintTemplate] = {}
    for raw in _load_custom_blueprint_defs():
        bp = _build_generic_blueprint(raw)
        if bp is None:
            continue
        out[bp.name] = bp
    return out


def _merged_blueprints() -> dict[str, BlueprintTemplate]:
    return _custom_blueprints_map()


def is_builtin_blueprint(name: str) -> bool:
    return False


def list_blueprint_dicts() -> list[dict]:
    rows = []
    for name, bp in _merged_blueprints().items():
        row = bp.to_dict()
        row["source"] = "custom"
        rows.append(row)
    rows.sort(key=lambda r: r.get("name", ""))
    return rows


def upsert_custom_blueprint(raw: dict) -> dict:
    bp = _build_generic_blueprint(raw)
    if bp is None:
        raise ValueError("Invalid blueprint payload")

    defs = _load_custom_blueprint_defs()
    kept = [d for d in defs if str(d.get("name") or "").strip().lower() != bp.name]
    kept.append(bp.to_dict())
    _save_custom_blueprint_defs(kept)
    out = bp.to_dict()
    out["source"] = "custom"
    return out


def delete_custom_blueprint(name: str) -> bool:
    key = (name or "").strip().lower()
    if not key:
        return False
    defs = _load_custom_blueprint_defs()
    kept = [d for d in defs if str(d.get("name") or "").strip().lower() != key]
    if len(kept) == len(defs):
        return False
    _save_custom_blueprint_defs(kept)
    return True


def all_blueprints() -> list[BlueprintTemplate]:
    return list(_merged_blueprints().values())


def get_blueprint(name: str) -> BlueprintTemplate | None:
    return _merged_blueprints().get((name or "").strip().lower())
