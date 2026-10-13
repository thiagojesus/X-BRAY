from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class PayloadValidationError(ValueError):
    pass


def resolve_json_path(payload: JsonValue, path: tuple[str, ...]) -> JsonValue:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise PayloadValidationError(f"missing JSON path: {'.'.join(path)}")
        current = current[key]
    return current


def _has_content(value: JsonValue) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        children = [child for child in value.values() if child is not None]
        return bool(children) and all(_has_content(child) for child in children)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def validate_payload(payload: JsonValue, required_paths: tuple[tuple[str, ...], ...]) -> None:
    for path in required_paths:
        if not _has_content(resolve_json_path(payload, path)):
            raise PayloadValidationError(f"empty JSON path: {'.'.join(path)}")
