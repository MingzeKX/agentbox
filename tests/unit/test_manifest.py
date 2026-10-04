"""Tool manifest validation and the JSON-Schema subset."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.models.tool import (
    Expect,
    ToolManifest,
    ToolRecord,
    summarize_schema,
    validate_params_schema,
)

BASE = {
    "name": "word_count",
    "description": "Count words in a text file inside the workspace.",
    "when_to_use": "You need a quick word frequency table for a file you wrote.",
    "tags": ["text", "analysis"],
    "params_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "top": {"type": "integer", "minimum": 1, "default": 10}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "permissions": ["fs.read"],
    "source": "def run(args):\n    return {'words': 0}\n",
    "tests": [
        {"name": "ok", "args": {"path": "/workspace/a.txt"}, "expect": {"contains": {"words": 0}}},
        {"name": "edge", "args": {"path": "/workspace/empty.txt"}, "expect": {"is_true": "result"}},
        {"name": "error", "args": {"path": "/workspace/missing"}, "expect": {"raises": "FileNotFoundError"}},
    ],
}


def manifest(**overrides) -> ToolManifest:
    payload = {**BASE, **overrides}
    return ToolManifest.model_validate(payload)


def test_accepts_a_valid_manifest():
    m = manifest()
    assert m.name == "word_count"
    assert m.permissions == ["fs.read"]
    assert len(m.source_sha256) == 64


@pytest.mark.parametrize("name", ["Word_Count", "ab", "1word", "word-count", "word count", "x" * 50, "search_tools",
                                  "a" * 32 + "." + "b" * 10, "a..b", "a.", ".a"])
def test_rejects_bad_names(name: str):
    with pytest.raises(ValidationError):
        manifest(name=name)


@pytest.mark.parametrize("name", ["fs.read", "exec.run", "toolsmith.check", "sandbox.info", "my_tool_2"])
def test_accepts_namespaced_names(name: str):
    assert manifest(name=name).name == name


def test_name_segment_length_is_bounded():
    with pytest.raises(ValidationError):
        manifest(name="a" * 32 + ".b")


def test_rejects_unknown_permission():
    with pytest.raises(ValidationError):
        manifest(permissions=["fs.read", "network"])


def test_exec_shell_requires_exec():
    with pytest.raises(ValidationError):
        manifest(permissions=["exec.shell"])


def test_normalises_permission_order_and_deduplicates():
    assert manifest(permissions=["fs.write", "fs.read", "fs.read"]).permissions == ["fs.read", "fs.write"]


def test_rejects_bad_tag():
    with pytest.raises(ValidationError):
        manifest(tags=["UPPER CASE"])


def test_rejects_short_description():
    with pytest.raises(ValidationError):
        manifest(description="short")


def test_rejects_unsupported_schema_keywords():
    with pytest.raises(ValidationError):
        manifest(params_schema={"type": "object", "properties": {"a": {"$ref": "#/x"}}})


def test_rejects_non_object_root_schema():
    with pytest.raises(ValidationError):
        manifest(params_schema={"type": "array", "items": {"type": "string"}})


def test_rejects_union_types_and_unknown_required():
    with pytest.raises(ValidationError):
        manifest(params_schema={"type": "object", "properties": {"a": {"type": ["string", "null"]}}})
    with pytest.raises(ValidationError):
        manifest(
            params_schema={
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["b"],
            }
        )


def test_schema_subset_accepts_nested_objects_and_arrays():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
                    "required": ["name"],
                },
            },
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "additionalProperties": False,
    }
    assert validate_params_schema(schema) == []


def test_summarize_schema_marks_optional_arguments():
    summary = summarize_schema(BASE["params_schema"])
    assert "path:string" in summary
    assert "top:integer?" in summary


def test_expect_requires_exactly_one_assertion():
    assert Expect(equals=1).equals == 1
    assert Expect(raises="ValueError").raises == "ValueError"
    with pytest.raises(ValidationError):
        Expect()
    with pytest.raises(ValidationError):
        Expect(equals=1, raises="ValueError")


def test_tool_record_payload_is_self_describing():
    m = manifest()
    record = ToolRecord(
        id="00000000-0000-0000-0000-000000000000",
        name=m.name,
        version=3,
        status="active",
        tier="generated",
        executor="sandbox_python",
        description=m.description,
        when_to_use=m.when_to_use,
        tags=list(m.tags),
        params_schema=m.params_schema,
        permissions=list(m.permissions),
        timeout_s=m.timeout_s,
        entrypoint=m.entrypoint,
        examples=[],
        source=m.source,
        source_sha256=m.source_sha256,
        embedding_model="fake",
        created_at="2026-01-01T00:00:00",
    )
    payload = record.payload()
    assert payload.version == 3
    assert payload.source == m.source
    assert payload.sha256 == m.source_sha256
    view = record.schema_view(include_source=True)
    assert view.source == m.source
    assert record.schema_view().source is None


def test_exec_run_params_normalise_argv_and_env():
    from agent.models.tool import ExecRunParams

    params = ExecRunParams(argv="ls -la", env={"FOO": "bar"})
    assert params.argv == ["ls -la"]
    with pytest.raises(ValidationError):
        ExecRunParams(argv=[])
    with pytest.raises(ValidationError):
        ExecRunParams(argv=["ls"], env={"1BAD": "x"})
