# local-agent: rules for coding agents

## Python conventions

- Prefer Pydantic models over plain `dict` for structured data wherever possible: API request and
  response bodies, data crossing a service boundary (HTTP JSON, CDP/JS results, tool inputs and
  outputs), config, and anything passed between functions with a known shape. Parse raw data into
  a model at the boundary (`Model.model_validate(...)`) and pass the model on, instead of reading
  `data.get("key")` deep inside the code. A `dict` is fine only for truly free-form data (e.g. a
  JSON Schema passed through as-is, or an arbitrary tool `input` before it is validated).
