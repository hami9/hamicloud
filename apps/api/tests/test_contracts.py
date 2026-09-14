import ast
import glob
import json
import os
import pytest
import yaml
from jsonschema.validators import validator_for
from openapi_spec_validator import validate as validate_openapi

from app.core.events import OutboxTopic

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_ROOT, "contracts")
OPENAPI_SPEC = os.path.join(CONTRACTS_DIR, "openapi", "v1.yaml")
EVENTS_DIR = os.path.join(CONTRACTS_DIR, "events")


def extract_producer_emitted_topics() -> set[str]:
    """Derive all outbox topics emitted by producers by statically inspecting OutboxEvent instantiations."""
    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
    topics: set[str] = set()

    for root, _, files in os.walk(app_dir):
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                with open(filepath, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=filepath)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        func_name = None
                        if isinstance(node.func, ast.Name):
                            func_name = node.func.id
                        elif isinstance(node.func, ast.Attribute):
                            func_name = node.func.attr
                        if func_name == "OutboxEvent":
                            for kw in node.keywords:
                                if kw.arg == "topic":
                                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                        topics.add(kw.value.value)
                                    elif isinstance(kw.value, ast.Attribute):
                                        if (
                                            isinstance(kw.value.value, ast.Attribute)
                                            and isinstance(kw.value.value.value, ast.Name)
                                            and kw.value.value.value.id == "OutboxTopic"
                                        ):
                                            enum_name = kw.value.value.attr
                                            topics.add(getattr(OutboxTopic, enum_name).value)
                                        elif (
                                            isinstance(kw.value.value, ast.Name)
                                            and kw.value.value.id == "OutboxTopic"
                                        ):
                                            enum_name = kw.value.attr
                                            topics.add(getattr(OutboxTopic, enum_name).value)
    return topics


def test_openapi_specification_validity():
    """Verify that contracts/openapi/v1.yaml is a syntactically and semantically valid OpenAPI 3.1 spec."""
    assert os.path.exists(OPENAPI_SPEC), f"OpenAPI contract missing at {OPENAPI_SPEC}"
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec_dict = yaml.safe_load(f)
    validate_openapi(spec_dict)


def test_event_schemas_validity():
    """Verify that every file in contracts/events/*.json is a valid JSON Schema."""
    schema_files = glob.glob(os.path.join(EVENTS_DIR, "*.json"))
    assert len(schema_files) > 0, "No event schemas found in contracts/events/"

    for schema_file in schema_files:
        with open(schema_file, "r", encoding="utf-8") as f:
            schema_data = json.load(f)
        validator_cls = validator_for(schema_data)
        validator_cls.check_schema(schema_data)


def test_all_emitted_topics_have_event_contracts():
    """Verify that every topic emitted by producers has a corresponding contract schema file in contracts/events/."""
    emitted_topics = extract_producer_emitted_topics()
    assert len(emitted_topics) > 0, "No emitted outbox topics found in producers"
    for topic in emitted_topics:
        expected_file = os.path.join(EVENTS_DIR, f"{topic}.json")
        assert os.path.isfile(expected_file), (
            f"Missing JSON schema for emitted outbox topic: '{topic}' at path {expected_file}"
        )
