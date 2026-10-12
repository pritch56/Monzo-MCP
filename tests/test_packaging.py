"""The documentation and packaging promises that are cheap to keep true automatically."""

import re
import tomllib
from pathlib import Path

from monzo_mcp import USER_AGENT, __version__

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "monzo_mcp"
TEXT_FILES = [
    *sorted(SRC.glob("*.py")),
    *sorted((ROOT / "tests").glob("*.py")),
    *(ROOT / name for name in ("README.md", ".env.example", "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md")),
    ROOT / "pyproject.toml",
]


def env_vars_read_by_the_code() -> set[str]:
    return {name for path in SRC.glob("*.py") for name in re.findall(r"\bMONZO_[A-Z0-9_]+\b", path.read_text("utf-8"))}


def test_every_environment_variable_is_documented():
    names = env_vars_read_by_the_code()
    assert {"MONZO_CLIENT_ID", "MONZO_REDIRECT_URI", "MONZO_MAX_RANGE_DAYS", "MONZO_MCP_ALLOW_NO_AUTH"} <= names
    readme = (ROOT / "README.md").read_text("utf-8")
    example = (ROOT / ".env.example").read_text("utf-8")
    table = {m for m in re.findall(r"^\| `(MONZO_[A-Z0-9_]+)` \|", readme, flags=re.M)}
    assert table == names, (names - table, table - names)
    in_example = set(re.findall(r"^#? ?(MONZO_[A-Z0-9_]+)=", example, flags=re.M))
    assert in_example == names, (names - in_example, in_example - names)
    assert "PORT" not in {
        n for n in re.findall(r"environ\.get\(\"([A-Z_]+)\"", "".join(p.read_text("utf-8") for p in SRC.glob("*.py")))
    }


def test_env_example_contains_no_values_for_secrets():
    for line in (ROOT / ".env.example").read_text("utf-8").splitlines():
        if re.match(r"#? ?MONZO_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN|ACCESS_TOKEN|MCP_AUTH_TOKEN)=", line):
            assert line.rstrip().endswith("="), line


def test_no_trace_of_any_private_setup():
    banned = [
        r"C:\\+Finance",
        r"Finance sheet",
        r"weekly (job|routine|run)",
        r"the routine",
        r"sibling server",
        r"Amex agent",
        r"on your behalf",
        r"while building this",
        r"--directory\W+mcp/",
        r"would churn",
        r"no credentials were available",
    ]
    this_file = Path(__file__).resolve()
    for path in TEXT_FILES:
        if path.resolve() == this_file:
            continue
        text = path.read_text("utf-8")
        for pattern in banned:
            assert not re.search(pattern, text, flags=re.I), (path.name, pattern)
    # "spreadsheet" survives only as the formula-injection warning, never as a description of a setup
    for path in TEXT_FILES:
        for line in path.read_text("utf-8").splitlines():
            if "spreadsheet" in line.lower() and path.resolve() != this_file:
                assert path.name in ("README.md", "SECURITY.md"), (path.name, line)


def test_readme_says_what_it_must():
    readme = (ROOT / "README.md").read_text("utf-8")
    flat = " ".join(readme.replace(">", " ").split())
    assert "unofficial" in flat.lower().split("##")[0]
    assert "Status: alpha" in flat and "Unverified assumptions" in flat
    assert '"The Monzo Developer API is not suitable for building public applications."' in flat
    assert '"You may only connect to your own account or those of a small set of users you explicitly allow."' in flat
    assert "not affiliated with, endorsed by or supported by Monzo Bank" in flat
    assert "belongs to an unrelated project" in flat  # the PyPI name
    for heading in (
        "## Unverified assumptions",
        "## Always re-fetch an overlapping window",
        "## Error codes",
        "## Configuration",
        "## Limitations",
        "## Security",
        "## Troubleshooting",
    ):
        assert heading in readme, heading
    codes = "not_connected token_expired consent_expired sca_not_approved rate_limited invalid_request"
    codes += " account_not_found provider_error config_error token_store_error internal_error"
    for code in codes.split():
        assert f"| `[{code}]` |" in readme, code
    assert "git clone" in readme and "uv sync" in readme and "```powershell" in readme and "```bash" in readme
    assert not re.search(r"\b\d+ (passing )?tests\b", readme)  # no hard-coded test counts
    # config examples use placeholders, never values
    assert '"${MONZO_CLIENT_SECRET}"' in readme


def test_every_error_code_the_code_can_raise_is_in_the_readme():
    from monzo_mcp import errors

    readme = (ROOT / "README.md").read_text("utf-8")
    kinds = {obj.kind for obj in vars(errors).values() if isinstance(obj, type) and issubclass(obj, errors.MonzoError)}
    assert "api_error" not in kinds and "history_window_exceeded" not in kinds and "monzo_error" not in kinds
    for kind in kinds:
        assert f"`[{kind}]`" in readme, kind


def test_pyproject_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    meta = project["project"]
    assert meta["name"] == "monzo-mcp"
    assert meta["description"].startswith("Unofficial ")
    assert meta["requires-python"] == ">=3.11" and meta["readme"] == "README.md"
    assert meta["license"] == "MIT" and meta["authors"] == [{"name": "pritch56"}]
    assert meta["keywords"] and meta["classifiers"]
    assert not any("License" in c for c in meta["classifiers"])
    assert "mcp>=2.3,<3" in meta["dependencies"]
    assert meta["scripts"] == {
        "monzo-mcp": "monzo_mcp.server:main",
        "monzo-mcp-authorize": "monzo_mcp.authorize:main",
    }
    assert project["build-system"]["build-backend"] == "hatchling.build"
    assert "ruff" in project["tool"] and "dev" in project["dependency-groups"]
    assert (ROOT / "LICENSE").exists() and not (ROOT / ".python-version").exists()
    assert USER_AGENT == f"monzo-mcp/{__version__}"

    # every third-party package the source imports is a declared runtime dependency
    declared = {re.split(r"[<>=!~ \[]", d, maxsplit=1)[0].lower() for d in meta["dependencies"]}
    stdlib_or_self = {"monzo_mcp", "__future__"}
    imported = set()
    for path in SRC.glob("*.py"):
        for match in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][\w]*)", path.read_text("utf-8"), flags=re.M):
            imported.add(match.group(1))
    import sys

    third_party = {m for m in imported if m not in sys.stdlib_module_names and m not in stdlib_or_self}
    assert third_party <= declared, third_party - declared
    assert "starlette" not in third_party  # only the tests use it, so it is a dev dependency
    assert "tzdata" in declared  # zoneinfo needs it on Windows


def test_gitignore_covers_token_files_and_local_state():
    lines = (ROOT / ".gitignore").read_text("utf-8").split()
    for pattern in (
        ".env",
        ".env.*",
        "!.env.example",
        "*.pem",
        "*.key",
        "tokens.json",
        "*.tokens.json",
        "*-tokens.json",  # e.g. monzo-tokens.json
        "*_tokens.json",
        ".monzo-mcp/",
        ".amex-mcp/",
        ".nationwide-mcp/",
        "statements/",
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        "*.egg-info/",
        "build/",
        "dist/",
        ".pytest_cache/",
        ".pytest_tmp/",
        ".ruff_cache/",
        ".mypy_cache/",
        "ruvector.db",
        "*.db",
        ".claude/",
        ".remember/",
    ):
        assert pattern in lines, pattern


def test_release_documents_exist():
    assert "private vulnerability reporting" in (ROOT / "SECURITY.md").read_text("utf-8")
    assert "0.1.0" in (ROOT / "CHANGELOG.md").read_text("utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text("utf-8")
    assert "ruff" in contributing and "pytest" in contributing and "real tokens" in contributing
    assert (SRC / "__main__.py").exists()
