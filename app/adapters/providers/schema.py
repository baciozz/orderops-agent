"""Strict local validator for the parser schema subset, without extra dependencies.

Only local JSON Pointer references are resolved. Unsupported assertions fail closed;
annotations are ignored. Errors never include the schema or untrusted payload.
"""

import math

from app.errors import SchemaInvalid

_ANNOTATIONS = {"title", "description", "default", "$defs", "$schema"}
_ASSERTIONS = {
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
}


def ensure_supported_schema(schema: dict[str, object]) -> None:
    """Refuse unsupported schemas before billing, including unused branches."""

    def check(node, depth=0):
        if depth > 64 or not isinstance(node, dict):
            raise ValueError
        if node.keys() - _ANNOTATIONS - _ASSERTIONS:
            raise ValueError
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise ValueError
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            check(target, depth + 1)
        for keyword in ("$defs", "properties"):
            if keyword in node:
                if not isinstance(node[keyword], dict):
                    raise ValueError
                for child in node[keyword].values():
                    check(child, depth + 1)
        if "anyOf" in node:
            if not isinstance(node["anyOf"], list):
                raise ValueError
            for child in node["anyOf"]:
                check(child, depth + 1)
        if "required" in node and not isinstance(node["required"], list):
            raise ValueError
        if "items" in node:
            check(node["items"], depth + 1)
        if isinstance(node.get("additionalProperties"), dict):
            check(node["additionalProperties"], depth + 1)

    try:
        check(schema)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise SchemaInvalid("响应 schema 含本地校验器不支持的关键字") from None


def validate_payload(payload: dict[str, object], schema: dict[str, object]) -> None:
    def equal(left, right):
        # JSON booleans are not numbers, despite Python's bool/int relationship.
        return left == right and (isinstance(left, bool) == isinstance(right, bool))

    def matches_type(value, kind):
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
            "boolean": type(value) is bool,
            "null": value is None,
        }.get(kind, False)

    def check(value, node, depth=0):
        if depth > 64 or not isinstance(node, dict):
            raise ValueError
        if node.keys() - _ANNOTATIONS - _ASSERTIONS:
            raise ValueError
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise ValueError
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            check(value, target, depth + 1)
        if "anyOf" in node:
            for choice in node["anyOf"]:
                try:
                    check(value, choice, depth + 1)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError
        if "type" in node:
            kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
            if not any(matches_type(value, kind) for kind in kinds):
                raise ValueError
        if "const" in node and not equal(value, node["const"]):
            raise ValueError
        if "enum" in node and not any(equal(value, candidate) for candidate in node["enum"]):
            raise ValueError
        if isinstance(value, dict):
            properties = node.get("properties", {})
            if any(name not in value for name in node.get("required", [])):
                raise ValueError
            for name, item in value.items():
                if name in properties:
                    check(item, properties[name], depth + 1)
                elif node.get("additionalProperties") is False:
                    raise ValueError
                elif isinstance(node.get("additionalProperties"), dict):
                    check(item, node["additionalProperties"], depth + 1)
        if isinstance(value, list) and "items" in node:
            for item in value:
                check(item, node["items"], depth + 1)

    try:
        check(payload, schema)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise SchemaInvalid("模型输出不合严格 schema") from None
