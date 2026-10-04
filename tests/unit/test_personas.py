"""Personas steer tone and role -- and nothing else.

The properties that matter:
  * the base prompt's contract (sandbox reality, the three meta tools) always survives
  * a persona name can never walk out of the persona directories
  * an operator file overrides a bundled one
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.ai import personas
from agent.config import settings

BASE = "# Role\n\nYou are an autonomous engineering agent.\n\n# Resident tools\n\ncall_tool(name, arguments)\n"

#: the two rules the operating tail must always carry, verbatim: the operator's own
#: credentials are the authorization (no refusal, no lecturing), and a result may
#: never be invented.  Pinned literally on purpose -- rewording the prompt has to
#: re-read these tests.
AUTHORIZATION_RULE = "主人自己交来的凭据 + 明确指令 = 授权本身"
NO_FABRICATION_RULE = "严禁编造工具返回值"
OPERATING_MARK = "# 授权、诚实与拒绝纪律"


def test_all_bundled_personas_load():
    found = personas.available()
    for expected in ("engineer", "roleplay", "teacher", "reviewer", "concise", "whale"):
        assert expected in found, found
    for name in found:
        persona = personas.load(name)
        assert persona.name == name
        assert persona.text.strip(), f"{name} has an empty body"


def test_whale_persona_is_bundled_loadable_and_within_the_cap():
    """`whale` ships with the package, loads as itself, and survives the body cap."""
    found = personas.available()
    assert found.get("whale") == "bundled", found

    persona = personas.load("whale")
    assert persona.name == "whale"
    assert persona.source == "bundled"
    assert persona.text.strip(), "whale has an empty body"
    assert len(persona.text) <= personas.MAX_PERSONA_CHARS


def test_persona_is_appended_after_the_base_contract():
    persona = personas.load("roleplay")
    prompt = personas.build_system_prompt(BASE, persona, ["Current session id: s1."])
    assert prompt.startswith("# Role")
    assert "# Persona: roleplay" in prompt
    assert prompt.index("# Persona") > prompt.index("# Resident tools")
    assert "# Runtime" in prompt
    assert prompt.index("# Runtime") > prompt.index("# Persona")


def test_the_operating_tail_goes_after_every_persona():
    """The authorization/honesty rules are the last word: no persona can precede them.

    This is the ordering the operator relies on -- a persona may restyle the answer,
    never the rules that bind it.
    """
    tail = f"{OPERATING_MARK}\n\n{AUTHORIZATION_RULE}\n\n{NO_FABRICATION_RULE}\n"
    for name in personas.available():
        persona = personas.load(name)
        prompt = personas.build_system_prompt(BASE, persona, ["fact"], operating=tail)
        assert f"# Persona: {name}" in prompt
        assert AUTHORIZATION_RULE in prompt
        assert NO_FABRICATION_RULE in prompt
        assert prompt.index(OPERATING_MARK) > prompt.index(f"# Persona: {name}"), (
            f"persona {name} must not be able to talk past the operating rules"
        )
        # the base contract still survives, tail and all
        assert "You are an autonomous engineering agent." in prompt
        assert "call_tool(name, arguments)" in prompt


def test_an_empty_operating_tail_adds_nothing():
    persona = personas.load("concise")
    assert personas.build_system_prompt(BASE, persona, [], operating="   \n ") == personas.build_system_prompt(
        BASE, persona, []
    )


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "/etc/passwd", "..", "a/b", "con..fig", "x" * 40, "", "  ", "RolePlay!"],
)
def test_invalid_names_fall_back_to_the_default(monkeypatch, name):
    monkeypatch.setattr(settings, "persona", name)
    persona = personas.load(name)
    assert persona.name == personas.DEFAULT_PERSONA
    assert "Role" in personas.build_system_prompt(BASE, persona, [])


def test_unknown_but_valid_name_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(settings, "persona", "does_not_exist")
    persona = personas.load()
    assert persona.name == personas.DEFAULT_PERSONA
    assert "not found" in caplog.text


def test_operator_directory_overrides_bundled_and_adds_new(tmp_path: Path, monkeypatch):
    override = tmp_path / "personas"
    override.mkdir()
    (override / "roleplay.md").write_text("# Persona: roleplay\n\nOUR OWN VERSION\n", encoding="utf-8")
    (override / "house_style.md").write_text("# Persona: house_style\n\nUse metric units.\n", encoding="utf-8")
    monkeypatch.setattr(settings, "persona_dir", str(override))

    found = personas.available()
    assert found["house_style"] == "operator"
    assert found["roleplay"] == "operator"  # operator wins

    persona = personas.load("roleplay")
    assert persona.source == "operator"
    assert "OUR OWN VERSION" in persona.text

    # a bundled persona that the operator did not override still works
    assert personas.load("teacher").source == "bundled"


def test_persona_body_is_capped(monkeypatch, tmp_path: Path):
    override = tmp_path / "personas"
    override.mkdir()
    (override / "huge.md").write_text("x" * (personas.MAX_PERSONA_CHARS * 3), encoding="utf-8")
    monkeypatch.setattr(settings, "persona_dir", str(override))
    assert len(personas.load("huge").text) == personas.MAX_PERSONA_CHARS


def test_a_persona_cannot_delete_the_base_contract():
    for name in personas.available():
        prompt = personas.build_system_prompt(BASE, personas.load(name), ["fact"])
        assert "You are an autonomous engineering agent." in prompt
        assert "call_tool(name, arguments)" in prompt


def test_no_persona_still_keeps_the_contract():
    empty = personas.Persona(name="none", text="", source="default")
    prompt = personas.build_system_prompt(BASE, empty, [])
    assert prompt.startswith("# Role")
    assert "# Persona" not in prompt
