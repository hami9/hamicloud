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


def extract_topics_from_ast_tree(tree: ast.AST, filepath: str = "<unknown>") -> set[str]:
    """Extract topic strings from an AST tree, failing if any topic is dynamic, unrecognized, or missing."""
    topics: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            is_outbox_call = func_name == "OutboxEvent" or (
                func_name is not None and "outbox" in func_name.lower()
            )
            topic_kw = next((kw for kw in node.keywords if kw.arg == "topic"), None)

            if is_outbox_call and topic_kw is None:
                raise ValueError(
                    f"Outbox call '{func_name}' at {filepath}:{node.lineno} is missing required 'topic=' keyword argument."
                )

            if topic_kw is not None:
                val = topic_kw.value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    topics.add(val.value)
                elif isinstance(val, ast.Attribute):
                    if (
                        isinstance(val.value, ast.Attribute)
                        and isinstance(val.value.value, ast.Name)
                        and val.value.value.id == "OutboxTopic"
                    ):
                        topics.add(getattr(OutboxTopic, val.value.attr).value)
                    elif isinstance(val.value, ast.Name) and val.value.id == "OutboxTopic":
                        topics.add(getattr(OutboxTopic, val.attr).value)
                    else:
                        raise ValueError(
                            f"Unrecognized topic attribute expression at {filepath}:{node.lineno}: "
                            f"{ast.dump(val)}. Topics must be statically referenced from OutboxTopic."
                        )
                else:
                    raise ValueError(
                        f"Unrecognized or dynamic topic expression at {filepath}:{node.lineno}: "
                        f"{ast.dump(val)}. Dynamic topics (f-strings, variables, calls) are strictly forbidden; "
                        f"topics must be statically declared to guarantee contract coverage."
                    )
    return topics


def extract_producer_emitted_topics() -> set[str]:
    """Derive all outbox topics emitted by producers by statically inspecting Python AST."""
    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
    topics: set[str] = set()

    for root, _, files in os.walk(app_dir):
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                with open(filepath, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=filepath)
                topics.update(extract_topics_from_ast_tree(tree, filepath))
    return topics


def test_openapi_specification_validity():
    """Verify that contracts/openapi/v1.yaml is a syntactically and semantically valid OpenAPI 3.1 spec."""
    assert os.path.exists(OPENAPI_SPEC), f"OpenAPI contract missing at {OPENAPI_SPEC}"
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        spec_dict = yaml.safe_load(f)
    validate_openapi(spec_dict)


def test_no_nullable_keyword_in_openapi_spec():
    """Verify that OpenAPI 3.1 contract uses type: [<type>, 'null'] unions and never obsolete 'nullable:'."""
    with open(OPENAPI_SPEC, "r", encoding="utf-8") as f:
        content = f.read()
    assert "nullable:" not in content, (
        "Found obsolete 'nullable:' keyword in contracts/openapi/v1.yaml! "
        "OpenAPI 3.1 uses JSON Schema type unions such as type: [string, 'null']."
    )


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


def test_unrecognized_topic_expression_fails():
    """Verify that extract_producer_emitted_topics rejects dynamic topic expressions like f-strings or variables."""
    fstring_code = "OutboxEvent(topic=f'job.{action}.requested.v1')"
    tree = ast.parse(fstring_code)
    with pytest.raises(ValueError, match="Unrecognized or dynamic topic expression"):
        extract_topics_from_ast_tree(tree, "fake_producer.py")

    var_code = "topic_name = 'job.submitted.v1'; OutboxEvent(topic=topic_name)"
    tree_var = ast.parse(var_code)
    with pytest.raises(ValueError, match="Unrecognized or dynamic topic expression"):
        extract_topics_from_ast_tree(tree_var, "fake_producer.py")

    missing_kw_code = "OutboxEvent(payload_json={})"
    tree_missing = ast.parse(missing_kw_code)
    with pytest.raises(ValueError, match="missing required 'topic=' keyword argument"):
        extract_topics_from_ast_tree(tree_missing, "fake_producer.py")

