"""Build and probe one ordinary wheel using an explicitly provisioned wheelhouse."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.dont_write_bytecode = True

from check_distribution import check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("app", "schemas", "seeds", "order_ops_migrations")


def copy_inputs(source: Path, target: Path) -> None:
    """Copy package sources plus explicit MANIFEST include inputs, never build in source."""
    files = {"pyproject.toml", "MANIFEST.in"}
    for line in (source / "MANIFEST.in").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        words = line.split()
        if words[0] != "include" or any("*" in w or "?" in w for w in words[1:]):
            raise ValueError("Update copy_inputs for new MANIFEST directives")
        files.update(words[1:])
    for package in PACKAGES:
        for path in (source / package).rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlink packaging input: {path}")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                # Explicit packaging types: never copy secrets, databases or stale outputs.
                if path.name.startswith("."):
                    continue
                if path.suffix not in {".py", ".html", ".css", ".js", ".json", ".mako"}:
                    raise ValueError(f"Unsupported packaging input: {path}")
                files.add(path.relative_to(source).as_posix())
    for name in sorted(files):
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Unsafe packaging input: {name}")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)


def select_wheel(directory: Path) -> Path:
    wheels = list(directory.iterdir())
    if len(wheels) != 1 or not wheels[0].name.startswith("order_ops_superagent-"):
        raise ValueError("Expected exactly one newly built project wheel")
    if wheels[0].suffix != ".whl":
        raise ValueError("Expected a wheel")
    return wheels[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="New evidence directory outside source"
    )
    args = parser.parse_args()
    wheelhouse = args.wheelhouse.resolve()
    output = args.output.resolve()
    if not wheelhouse.is_dir() or not list(wheelhouse.glob("*.whl")):
        parser.error("Provision dependency wheels first; wheelhouse is empty or absent")
    if output.is_relative_to(ROOT) or output.exists():
        parser.error("Output must be a new directory outside the source tree")
    output.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON"))}
    env.update(
        PIP_NO_INDEX="1",
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONDONTWRITEBYTECODE="1",
        OOS_LIVE_ENABLED="false",
    )
    report = {"status": "failed", "stage": "setup"}
    try:
        with tempfile.TemporaryDirectory(prefix="order ops wheel % ") as temporary:
            work = Path(temporary)
            cwd = work / "unrelated cwd %"
            cwd.mkdir()

            def run(stage: str, command: list[str]) -> None:
                report["stage"] = stage
                with (output / f"{stage}.log").open("w") as log:
                    subprocess.run(
                        command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=True
                    )

            def python(name: str) -> str:
                location = work / name
                venv.EnvBuilder(with_pip=True).create(location)
                return str(location / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

            build = python("build")
            runtime = python("runtime")
            common = [
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--only-binary=:all:",
                "--no-cache-dir",
                "--no-compile",
            ]
            run(
                "build-tools",
                [
                    build,
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    *common,
                    "-r",
                    str(ROOT / "requirements-build.lock"),
                ],
            )
            source = work / "source"
            report["stage"] = "copy-inputs"
            copy_inputs(ROOT, source)
            artifacts = work / "artifacts"
            artifacts.mkdir()
            run(
                "build",
                [
                    build,
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--no-cache-dir",
                    "-w",
                    str(artifacts),
                    str(source),
                ],
            )
            report["stage"] = "inventory"
            wheel = select_wheel(artifacts)
            report["resources"] = check(wheel)
            report["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
            shutil.copyfile(wheel, output / wheel.name)
            run(
                "runtime-tools",
                [runtime, "-I", "-B", "-m", "pip", "install", *common, "pip==26.2.2"],
            )
            run(
                "runtime-dependencies",
                [
                    runtime,
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    *common,
                    "-r",
                    str(ROOT / "requirements.lock"),
                ],
            )
            run(
                "install",
                [runtime, "-I", "-B", "-m", "pip", "install", *common, "--no-deps", str(wheel)],
            )
            run("pip-check", [runtime, "-I", "-B", "-m", "pip", "check"])
            run(
                "probe",
                [
                    runtime,
                    "-I",
                    "-B",
                    str(ROOT / "scripts/probe_installed_wheel.py"),
                    str(wheel),
                    str(work / "probe.sqlite"),
                ],
            )
            report["probe"] = json.loads((output / "probe.log").read_text())
            report.update(status="passed", stage="complete")
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
