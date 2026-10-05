from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.allow_system
def test_runtime_and_web_test_client_import_without_deprecation_filters():
    # Fresh locked interpreter: no pytest warning filters or imported aliases.
    # Import-only, with no bytecode, client operations, network or state writes.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import tow.web; import starlette.testclient; print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "ok"
