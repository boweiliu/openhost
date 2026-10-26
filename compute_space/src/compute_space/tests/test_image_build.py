"""Exercise the OVA capacity command from build.sh without booting a build VM."""

import json
import subprocess
from pathlib import Path

import pytest

BUILD_SCRIPT = Path(__file__).resolve().parents[4] / "image" / "build.sh"


def run_capacity_command(info: str) -> subprocess.CompletedProcess[str]:
    # Run the production shell statement, rather than a copy of its JSON parser.
    statement = next(
        line.strip() for line in BUILD_SCRIPT.read_text().splitlines() if line.strip().startswith("CAPACITY_BYTES=")
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail; DISK=disk.qcow2; "
            'qemu-img() { [[ "$*" == "info --output=json disk.qcow2" ]] || return 1; cat; }; '
            + statement
            + '; printf "%s\\n" "$CAPACITY_BYTES"',
        ],
        input=info,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("capacity", [21474836480, 42949672960])
@pytest.mark.parametrize("root_first", [False, True])
@pytest.mark.parametrize("indent", [None, 4])
def test_ova_capacity_uses_root_virtual_size(capacity: int, root_first: bool, indent: int | None) -> None:
    # QEMU can list children before the root: their virtual-size is the compressed
    # physical size, not the logical disk capacity declared by the OVF.
    children = [{"info": {"virtual-size": 3883925504, "children": [{"info": {"virtual-size": 1024}}]}}]
    pairs = [("virtual-size", capacity), ("children", children)]
    if not root_first:
        pairs.reverse()
    result = run_capacity_command(json.dumps(dict(pairs), indent=indent))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{capacity}\n"


@pytest.mark.parametrize("info", ['{"children": [{"info": {"virtual-size": 3883925504}}]}', "not JSON"])
def test_ova_capacity_rejects_missing_root_or_invalid_json(info: str) -> None:
    result = run_capacity_command(info)
    assert result.returncode != 0
    assert result.stdout == ""
