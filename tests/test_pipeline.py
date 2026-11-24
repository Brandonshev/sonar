import copy
import json

import pytest

from sonar import pipeline, run
from sonar.pipeline import PipelineError

GOOD = {
    "version": 1,
    "nodes": [
        {"id": "a", "type": "source.x", "enabled": True, "every": "1h"},
        {"id": "s", "type": "scorer.heat", "enabled": True},
        {"id": "off", "type": "output.ntfy", "enabled": False, "every": "1h"},
    ],
    "edges": [["a", "s"]],
}


def bad(mutate):
    data = copy.deepcopy(GOOD)
    mutate(data)
    return data


def test_repo_pipeline_is_valid():
    pipeline.load()


def test_good_passes():
    pipeline.validate(copy.deepcopy(GOOD))


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("edges"),
    lambda d: d["nodes"][0].update(type="collector.x"),
    lambda d: d["nodes"][0].update(every="3 hours"),
    lambda d: d["nodes"][0].update(every="0h"),
    lambda d: d["nodes"][0].pop("enabled"),
    lambda d: d["nodes"][0].update(surprise=1),
    lambda d: d["edges"].append(["a"]),
    lambda d: d["nodes"].append({"id": "a", "type": "source.y", "enabled": True}),
    lambda d: d["edges"].append(["a", "ghost"]),
])
def test_bad_rejected(mutate):
    with pytest.raises(PipelineError):
        pipeline.validate(bad(mutate))


def test_load_rejects_broken_json(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{not json")
    with pytest.raises(PipelineError):
        pipeline.load(f)


def test_run_refuses_invalid_file(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(bad(lambda d: d.pop("nodes"))))
    assert run.main(["--all-due", "--pipeline", str(f)]) == 2


def test_every_seconds():
    assert pipeline.every_seconds("30m") == 1800
    assert pipeline.every_seconds("3h") == 10800
    assert pipeline.every_seconds("1d") == 86400
