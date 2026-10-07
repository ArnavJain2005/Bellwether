"""Write shared/schemas.json for the dashboard: `python -m shared.export_schema`."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from shared.schemas import EXPORTED_MODELS

SCHEMA_PATH = Path(__file__).with_name("schemas.json")


def build_schema() -> str:
    _, schema = models_json_schema(
        [(model, "serialization") for model in EXPORTED_MODELS],
        title="Bellwether shared schemas",
    )
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_PATH.write_text(build_schema(), encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
