"""Loads and validates pipeline.json against pipeline.schema.json."""
import json
import re
from pathlib import Path

import jsonschema

from . import ROOT

PIPELINE_PATH = ROOT / "pipeline.json"
SCHEMA_PATH = ROOT / "pipeline.schema.json"
UNITS = {"m": 60, "h": 3600, "d": 86400}


class PipelineError(ValueError):
    pass


def validate(data: dict) -> dict:
    schema = json.loads(SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise PipelineError(f"pipeline.json invalid at {where}: {e.message}") from None
    # Checks JSON Schema can't express.
    ids = [n["id"] for n in data["nodes"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise PipelineError(f"duplicate node ids: {sorted(dupes)}")
    for a, b in data.get("edges", []):
        for end in (a, b):
            if end not in ids:
                raise PipelineError(f"edge [{a}, {b}] points at unknown node {end!r}")
    return data


def load(path: str | Path = PIPELINE_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise PipelineError(f"{path} is not valid JSON: {e}") from None
    return validate(data)


def every_seconds(every: str) -> int:
    m = re.fullmatch(r"(\d+)([mhd])", every)
    if not m:
        raise PipelineError(f"bad schedule {every!r}")
    return int(m[1]) * UNITS[m[2]]


def upstream(data: dict, node_id: str) -> list[str]:
    return [a for a, b in data.get("edges", []) if b == node_id]
