"""The bucketing guard has a test, because it now gates every build.

``scripts/check-no-local-bucketing.mjs`` sits outside ``[tool.coverage.run] source`` and outside
``[tool.mutmut] source_paths``, so nothing in this repo re-proves it after an edit. Its correctness
rested on a manual run recorded in a commit message, and a guard whose only evidence is prose is
the class of gate that quietly stops gating.

Both directions are asserted. A guard that only ever passes is indistinguishable from one that
cannot fail, and the allowlist half is the part that rots: an entry matching nothing must be an
error, or the allowlist becomes a place to park anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SCRIPT_NAME = "check-no-local-bucketing.mjs"


def _find_script() -> Path | None:
    """Walk up for the real script rather than assuming the tree we are run from.

    mutmut copies the suite into ``mutants/`` and runs it from there, and ``mutants/`` has no
    ``scripts/`` — so ``parents[1]`` resolves to a path that does not exist and the whole mutation
    gate fails on collection rather than on a mutant. Walking up finds the checkout in both
    layouts.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / SCRIPT_NAME
        if candidate.is_file():
            return candidate
    return None


SCRIPT = _find_script()

pytestmark = pytest.mark.skipif(
    NODE is None or SCRIPT is None,
    reason="the guard is a node script run from a checkout; CI installs node for it",
)


def _run(tmp_path: Path, source: str | None, allow: dict | None = None):
    """Drive the real script over a scratch tree via its own GUARD_ROOT/GUARD_SRC knobs."""
    root = tmp_path / "tree"
    scripts = root / "scripts"
    src = root / "src" / "pkg"
    src.mkdir(parents=True)
    scripts.mkdir(parents=True)
    assert SCRIPT is not None
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    (scripts / "no-local-bucketing-allow.json").write_text(json.dumps(allow if allow else {}))
    if source is not None:
        (src / "flags.py").write_text(source)
    return subprocess.run(
        [NODE, str(scripts / SCRIPT_NAME)],
        capture_output=True,
        text=True,
        env={"GUARD_ROOT": str(root), "GUARD_SRC": "src", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


CLEAN = "def variation(key, ctx, default):\n    return default\n"
BUCKETING = (
    "import hashlib\n\n"
    "def assign(experience_id, identifier):\n"
    '    digest = hashlib.sha256(f"{experience_id}:{identifier}".encode()).hexdigest()\n'
    "    return int(digest, 16) % 10000\n"
)


def test_a_clean_tree_passes(tmp_path):
    result = _run(tmp_path, CLEAN)

    assert result.returncode == 0, result.stderr
    assert "no-local-bucketing OK" in result.stdout


def test_local_bucket_derivation_fails_the_build(tmp_path):
    result = _run(tmp_path, BUCKETING)

    assert result.returncode == 1
    assert "must be server-only" in result.stderr
    assert "src/pkg/flags.py" in result.stderr


def test_an_allowlist_entry_suppresses_only_what_it_names(tmp_path):
    result = _run(tmp_path, BUCKETING, allow={"src/pkg/flags.py": "cache key, not a bucket"})

    assert result.returncode == 0, result.stderr


def test_an_allowlist_entry_matching_nothing_is_itself_an_error(tmp_path):
    # The reverse check. Without it a justification that no longer applies sits there forever and
    # the allowlist stops meaning anything.
    result = _run(tmp_path, CLEAN, allow={"src/pkg/gone.py": "removed last year"})

    assert result.returncode == 1
    assert "no longer match anything" in result.stderr


def test_an_allowance_with_no_reason_is_an_error(tmp_path):
    result = _run(tmp_path, BUCKETING, allow={"src/pkg/flags.py": "   "})

    assert result.returncode == 1
    assert "no reason" in result.stderr
