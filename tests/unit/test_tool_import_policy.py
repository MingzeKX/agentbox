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

#: one curated root per family the operator complains about most (HTTP, XML/HTML, YAML,
#: Excel, imaging, numerics) so a regression in the profile is caught for each
CURATED_ROOTS = (
    ("requests", "import requests"),
    ("urllib3", "import urllib3"),
    ("certifi", "import certifi"),
    ("charset_normalizer", "import charset_normalizer"),
    ("idna", "import idna"),
    ("bs4", "from bs4 import BeautifulSoup"),
    ("soupsieve", "import soupsieve"),
    ("lxml", "from lxml import etree"),
    ("yaml", "import yaml"),
    ("dateutil", "from dateutil import parser"),
    ("pytz", "import pytz"),
    ("openpyxl", "import openpyxl"),
    ("PIL", "from PIL import Image"),
    ("numpy", "import numpy"),
    ("pandas", "import pandas"),
)

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


def third_party_tool(statement: str) -> str:
    """A minimal, otherwise valid tool whose only interesting line is ``statement``."""
    return f"{statement}\n\n\ndef run(args):\n    return {{'ok': True}}\n"


def import_findings(report: checker.Report) -> list[checker.Finding]:
    """Only the import verdicts; a real tool's version probing is a separate rule.

    ``requests.__version__`` trips ``dunder_attribute`` (unchanged policy), which would
    otherwise mask the import verdict this module is about.
    """
    return [finding for finding in report.findings if finding.rule.startswith("import_")]


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


# ------------------------------------------------- third-party imports (live complaint)
# `import requests` is the exact shape a model writes and the checker rejected: the package
# is baked into the sandbox image (deploy/sandbox/build-sandbox-image.sh), so the "extended"
# profile -- and only that one -- has to admit it.  strict stays stdlib-only on purpose.


def test_requests_is_rejected_under_strict_and_accepted_under_extended():
    source = third_party_tool("import requests")
    strict = checker.check_source(source, profile="strict")
    assert strict.ok is False
    assert [finding.rule for finding in import_findings(strict)] == ["import_forbidden"]

    extended = checker.check_source(source, profile="extended")
    assert extended.ok is True, [finding.message for finding in import_findings(extended)]
    assert extended.stats["imports"] == ["requests"]


def test_extended_lists_every_curated_root_including_third_party():
    for module in ("requests", "bs4", "lxml", "yaml", "openpyxl", "PIL", "numpy", "pandas"):
        assert module in checker.EXTENDED_IMPORTS, module
    assert checker.THIRD_PARTY_IMPORTS <= checker.EXTENDED_IMPORTS


@pytest.mark.parametrize("module,statement", CURATED_ROOTS)
def test_every_curated_root_needs_extended(module, statement):  # noqa: ARG001 - module is the id
    source = third_party_tool(statement)
    assert checker.check_source(source, profile="strict").ok is False, "strict must stay stdlib-only"
    report = checker.check_source(source, profile="extended")
    assert report.ok is True, [finding.message for finding in import_findings(report)]


def test_a_third_party_module_is_still_rejected_under_strict_even_with_allow_open():
    """`allow_open` is about open(), not about imports: it must not widen the allow-list."""
    source = "import requests\n\n\ndef run(args):\n    with open(args['path']) as handle:\n        return {'ok': True, 'text': handle.read()}\n"
    report = checker.check_source(source, permissions=["fs.read"], profile="strict", allow_open=True)
    assert report.ok is False
    assert [finding.rule for finding in import_findings(report)] == ["import_forbidden"]


def test_unrestricted_admits_any_third_party_root():
    """The escape hatch: a package installed per session (/pip install) needs no allow-listing."""
    source = third_party_tool("import pyftpdlib")
    strict = checker.check_source(source, profile="strict")
    assert strict.ok is False
    assert [finding.rule for finding in import_findings(strict)] == ["import_forbidden"]

    report = checker.check_source(source, profile="unrestricted")
    assert report.ok is True, [finding.message for finding in import_findings(report)]
    assert report.stats["imports"] == ["pyftpdlib"]


def test_a_rejection_also_names_the_unrestricted_escape_hatch():
    report = checker.check_source(third_party_tool("import pyftpdlib"), profile="extended")
    message = " ".join(finding.message for finding in report.findings)
    assert "/config tool_extra_modules pyftpdlib" in message
    assert "tool_import_profile extended" in message
    assert "unrestricted" in message, "the ad-hoc (/pip install) path must be discoverable"


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
