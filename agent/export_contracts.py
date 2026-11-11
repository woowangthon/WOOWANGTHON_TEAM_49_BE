"""Export public JSON schemas without sample data or model calls."""

import argparse
from pathlib import Path

from agent.rules.support_manifest import support_manifest
from agent.rules.wages import default_rule_registry
from agent.schemas.public.ports import CheckpointEnvelope, ProgressEvent
from agent.schemas.public.results import ArtifactRef
from agent.schemas.public.tasks import AgentOutcome, AgentTask
from agent.services.local_json import write_json


def export_contracts(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for model in (AgentTask, AgentOutcome, CheckpointEnvelope, ProgressEvent, ArtifactRef):
        write_json(
            directory / f"{model.__name__}.schema.json",
            model.model_json_schema(mode="serialization" if model is AgentOutcome else "validation"),
        )
    write_json(directory / "support_manifest.json", support_manifest(default_rule_registry()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("agent/contracts"))
    export_contracts(parser.parse_args().output)


if __name__ == "__main__":
    main()
