"""OpenAPI metadata for handlers that read their own bodies after authorization."""

from typing import Annotated

from pydantic import WithJsonSchema

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
UUID = {**STRING, "format": "uuid", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}

ResourceId = Annotated[str, WithJsonSchema(UUID)]  # describes canonical ids without changing owner-first parsing


def exact(**properties):
    schema = {"type": "object", "additionalProperties": False, "properties": properties}
    if properties:
        schema["required"] = list(properties)
    return schema


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def enum(*values):
    return {**STRING, "enum": list(values)}


def array(items, **constraints):
    return {"type": "array", "items": items, **constraints}


def text_field(high, low=1):
    return {**STRING, "minLength": low, "maxLength": high, "description": "Length in Unicode characters after trimming."}


def json_body(schema, *, required=True):
    return {"requestBody": {"required": required, "content": {"application/json": {"schema": schema}}}}


def query(name, schema, description, *, required=False):
    return {"name": name, "in": "query", "required": required, "schema": schema, "description": description}


def header(description, schema=STRING, *, required=False):
    return {"description": description, "schema": schema, "required": required}


RETRY_AFTER = {"Retry-After": header("Seconds to wait before retrying.", INTEGER)}
BEARER = {"WWW-Authenticate": header("Bearer authentication challenge.", enum("Bearer"), required=True)}
LOCATION = {"Location": header("Relative URL of the resource.", required=True)}
REPLAYED = {"Idempotent-Replayed": header("Present on a replay.", enum("true"))}


def validation_error(*application_variants):
    """UUID path validation can also produce FastAPI's standard detail array."""
    variants = [{"$ref": "#/components/schemas/ErrorBody"}, *application_variants,
                {"$ref": "#/components/schemas/HTTPValidationError"}]
    return {"description": "Invalid application input or malformed path parameter.",
            "content": {"application/json": {"schema": {"anyOf": variants}}}}
