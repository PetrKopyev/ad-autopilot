import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "demo.sh"


@pytest.fixture
def stand(tmp_path):
    (tmp_path / "deploy").mkdir()
    shutil.copy(SCRIPT, tmp_path / "deploy" / "demo.sh")
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("<!doctype html>")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/docker-args"\n')
    docker.chmod(0o755)

    def run(env_file: str, **env: str) -> subprocess.CompletedProcess:
        (tmp_path / ".env.demo").write_text(env_file)
        return subprocess.run(
            ["sh", str(tmp_path / "deploy" / "demo.sh"), "--build"],
            capture_output=True,
            text=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env},
        )

    return tmp_path, run


@pytest.mark.parametrize("env_file", ["APP_DOMAIN=localhost\n", 'APP_DOMAIN="localhost"\r\n'])
def test_demo_starts_separate_compose_project(stand, env_file):
    root, run = stand
    result = run("COMPOSE_PROJECT_NAME=autopilot-demo\n" + env_file)
    assert result.returncode == 0, result.stderr
    args = (root / "docker-args").read_text().split()
    assert args[:3] == ["compose", "-p", "autopilot-demo"] and args[-3:] == ["up", "-d", "--build"]


@pytest.mark.parametrize(
    "env_file,env",
    [("APP_DOMAIN=autopilot.example\n", {}), ("APP_DOMAIN=localhost\n", {"APP_DOMAIN": "autopilot.example"}), ("", {})],
)
def test_demo_refuses_public_domain(stand, env_file, env):
    root, run = stand
    result = run(env_file, **env)
    assert result.returncode == 1 and "Демо только локально" in result.stderr
    assert not (root / "docker-args").exists()


def test_demo_example_uses_its_own_project_and_localhost():
    example = (SCRIPT.parents[1] / ".env.demo.example").read_text().splitlines()
    assert {"COMPOSE_PROJECT_NAME=autopilot-demo", "APP_DOMAIN=localhost", "DEMO_LOGIN=1"} <= set(example)
