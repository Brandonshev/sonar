"""Loads and validates pipeline.json against pipeline.schema.json."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    for n in data["nodes"]:
        if "every" in n and "at" in n:
            raise PipelineError(f"node {n['id']!r} has both 'every' and 'at'; pick one")
        if "tz" in n:
            try:
                ZoneInfo(n["tz"])
            except (ZoneInfoNotFoundError, ValueError):
                raise PipelineError(f"node {n['id']!r} has unknown timezone {n['tz']!r}") from None
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


def at_due(node: dict, last_run: int | None, now: int) -> bool:
    """For a daily "at": "HH:MM" node: due if it hasn't run since the most recent HH:MM in its tz."""
    tz = ZoneInfo(node.get("tz", "UTC"))
    local = datetime.fromtimestamp(now, tz)
    hh, mm = map(int, node["at"].split(":"))
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target > local:
        target -= timedelta(days=1)
    return last_run is None or last_run < target.timestamp()


def upstream(data: dict, node_id: str) -> list[str]:
    return [a for a, b in data.get("edges", []) if b == node_id]
