"""The openable window on the tool import allow-list.

Regression for the live complaint: a model-written `smtp_send_mail` tool was rejected for
importing smtplib/os/email and calling open(), so it could only build the message and had
to hand the send to exec.run.  The profiles make the width an operator decision, and a
permission-gated module now produces a "declare permission X" finding the model can act
on instead of a dead end.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.config import Settings
from agent.sandbox import checker

REPO = Path(__file__).resolve().parents[2]

SMTP_TOOL = '''import smtplib
from email.message import EmailMessage


def _check_addr(value):
    if "@" not in value:
        raise ValueError("not an email address")
    return value


def run(args):
    sender = _check_addr(args["from"])
    message = EmailMessage()
    message["From"] = sender
    return {"ok": True, "from": sender}
'''


def rules(report: checker.Report) -> set[str]:
    return {finding.rule for finding in report.findings}


def test_strict_still_refuses_smtplib():
    report = checker.check_source(SMTP_TOOL, permissions=["net"], profile="strict")
    assert report.ok is False
    assert "import_forbidden" in rules(report)


def test_extended_allows_it_when_the_permission_is_declared():
    report = checker.check_source(SMTP_TOOL, permissions=["net"], profile="extended")
    assert report.ok is True, [f.message for f in report.findings]


def test_extended_explains_a_missing_permission():
    """Without `net` the failure must say which permission to declare, not "unknown"."""
    report = checker.check_source(SMTP_TOOL, permissions=[], profile="extended")
    assert report.ok is False
    assert "permission_missing" in rules(report)
    assert any("could" in f.message or "needs the 'net' permission" in f.message for f in report.findings)


def test_unrestricted_allows_an_unlisted_module():
    source = "import ctypes\n\n\ndef run(args):\n    return {'ok': True, 'sizeof': ctypes.sizeof(ctypes.c_int)}\n"
    assert checker.check_source(source, profile="strict").ok is False
    assert checker.check_source(source, profile="unrestricted").ok is True


def test_unknown_profile_falls_back_to_strict():
    assert checker.allowed_modules("exTENDED") is not None  # case-insensitive match
    assert checker.allowed_modules("nonsense") == dict(checker.ALLOWED_IMPORTS)


def test_extra_modules_widen_one_module_at_a_time():
    # sqlite3 is in the extended profile but not in strict, so it isolates the extras path
    source = "import sqlite3\n\n\ndef run(args):\n    return {'ok': True}\n"
    assert checker.check_source(source, profile="strict").ok is False
    assert checker.check_source(source, profile="strict", extra_modules=["sqlite3"]).ok is True
    assert checker.check_source(source, profile="extended").ok is True


def test_open_needs_both_the_switch_and_a_filesystem_permission():
    source = "def run(args):\n    with open(args['path']) as handle:\n        return {'ok': True, 'text': handle.read()}\n"
    # switch off -> still forbidden
    assert checker.check_source(source, permissions=["fs.read"], profile="extended").ok is False
    # switch on but no fs permission -> still forbidden
    assert checker.check_source(source, permissions=["net"], profile="extended", allow_open=True).ok is False
    # switch on + fs.read -> allowed
    assert checker.check_source(source, permissions=["fs.read"], profile="extended", allow_open=True).ok is True


def test_gates_send_the_configured_policy():
    """The host must actually forward the profile, otherwise the knob does nothing."""
    source = (REPO / "src" / "agent" / "toolsmith" / "gates.py").read_text(encoding="utf-8")
    ast.parse(source)  # the file must stay parseable
    body = source[source.index("async def _static_check") : source.index("async def _run_tests")]
    assert "settings.tool_import_profile" in body
    assert "settings.tool_allow_open" in body
    assert "settings.tool_extra_modules" in body
    assert '"py.check"' in body


def test_guest_handler_forwards_the_policy():
    source = (REPO / "src" / "agent" / "sandbox" / "handlers.py").read_text(encoding="utf-8")
    block = source[source.index("def m_py_check") :][:900]
    for key in ("profile", "extra_modules", "allow_open"):
        assert f'params.get("{key}")' in block, key


def test_settings_defaults_stay_strict():
    settings = Settings()
    assert settings.tool_import_profile == "strict"
    assert settings.tool_allow_open is False
    assert settings.tool_extra_modules == ""


@pytest.mark.parametrize("profile", ["strict", "extended", "unrestricted"])
def test_every_profile_still_blocks_dynamic_evaluation(profile):
    """Widening imports must not re-open eval/exec/getattr-style escapes."""
    source = "def run(args):\n    return {'ok': True, 'x': eval('1+1')}\n"
    report = checker.check_source(source, profile=profile)
    assert report.ok is False
    assert "forbidden_name" in rules(report)


def test_a_rejection_tells_the_operator_exactly_what_to_run():
    """Usability: nobody should have to read the source to widen the allow-list."""
    report = checker.check_source(SMTP_TOOL, permissions=["net"], profile="strict")
    messages = " ".join(f.message for f in report.findings)
    assert "/config tool_extra_modules smtplib" in messages
    assert "tool_import_profile extended" in messages
