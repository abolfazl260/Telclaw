"""Provider JSON generation constraints, separate from factual validation."""
from ai.category_schemas import CATEGORY_FIELDS

STRICT_GROQ_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}

JSON_GENERATION_RULES = (
    "Write non-ASCII text (including Persian) directly as Unicode characters, "
    "not backslash-u escapes. Escape quotes, backslashes and line breaks correctly "
    "inside JSON strings. Return a complete JSON object, never an ellipsis or "
    "partial string. Unknown facts must be null; do not invent values."
)


def extraction_response_format(model, category):
    if model not in STRICT_GROQ_MODELS:
        return {"type": "json_object"}

    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    # Match the existing permissive internal field validator. This constrains
    # transport syntax/field names, not occupational or Advertio eligibility.
    value = {"anyOf": [
        {"type": "string"}, {"type": "number"}, {"type": "boolean"},
        {"type": "null"}, {"type": "array", "items": {"type": "string"}},
    ]}
    fields = {field: value for field in CATEGORY_FIELDS[category]}
    schema = obj({
        "category": {"type": "string", "enum": [category]},
        "data": obj({category: obj(fields)}),
    })
    return {"type": "json_schema", "json_schema": {
        "name": f"telclaw_{category}", "strict": True, "schema": schema,
    }}
