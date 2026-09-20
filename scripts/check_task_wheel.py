"""Install a sonata-tasks wheel with dependencies and smoke-test its public API."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path

EXTRA_IMPORTS = {
    "azure": "sonata_tasks.vm.providers.azure",
    "multipass": "sonata_tasks.vm.providers.multipass",
    "prometheus": "sonata_tasks.prometheus",
    "proxmox": "sonata_tasks.vm.providers.proxmox",
    "shell": "sonata_tasks.shell",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument(
        "--with",
        dest="siblings",
        action="append",
        type=Path,
        default=[],
        metavar="WHEEL",
        help=(
            "another wheel built from this repository, installed alongside. The "
            "catalogue pins the engine exactly and the two are released together, "
            "so on a pull request the pinned engine is not on PyPI yet and pip "
            "cannot resolve it from an index."
        ),
    )
    parser.add_argument(
        "--extra", action="append", choices=tuple(EXTRA_IMPORTS), default=[]
    )
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")
    siblings = [path.resolve() for path in args.siblings]
    for sibling in siblings:
        if not sibling.is_file():
            parser.error(f"sibling wheel does not exist: {sibling}")
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode()
        if "<S>" in metadata or "..." in metadata:
            raise SystemExit("wheel metadata contains a placeholder revision")

    with tempfile.TemporaryDirectory(prefix="sonata-task-wheel-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin" / "python"
        requested = [str(wheel) + (f"[{','.join(args.extra)}]" if args.extra else "")]
        # One pip invocation for all of them: the exact pin then resolves from the
        # sibling wheel instead of being looked up on an index.
        requested.extend(str(sibling) for sibling in siblings)
        subprocess.run((str(python), "-m", "pip", "install", *requested), check=True)
        extra_modules = [EXTRA_IMPORTS[extra] for extra in args.extra]
        code = (
            "import importlib, sonata_tasks; "
            "from sonata_tasks import CommandOptions,CommandTask; "
            "from sonata_tasks.docker import DockerBuildTask; "
            "from sonata_tasks.helm import HelmInstallTask; "
            "assert sonata_tasks.__file__; "
            f"[importlib.import_module(name) for name in {extra_modules!r}]"
        )
        subprocess.run((str(python), "-c", code), cwd=directory, check=True)
        example = (
            Path(__file__).resolve().parents[1] / "examples" / "shared_tasks_client.py"
        )
        subprocess.run((str(python), str(example)), cwd=directory, check=True)


if __name__ == "__main__":
    main()
