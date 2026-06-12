"""클러스터·이미지 위임 — docker/kind/kubectl 호출만. 로직 없음(기계적 배치, 규칙 1·2)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    """외부 도구 실패."""


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ToolError(f"$ {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


def docker_build(tag: str, context_dir: Path) -> None:
    run(["docker", "build", "-q", "-t", tag, str(context_dir)])


def kind_load(tag: str, cluster: str) -> None:
    run(["kind", "load", "docker-image", tag, "--name", cluster])


def ensure_namespace(ctx: str, ns: str) -> None:
    y = run(["kubectl", "--context", ctx, "create", "namespace", ns,
             "--dry-run=client", "-o", "yaml"]).stdout
    run(["kubectl", "--context", ctx, "apply", "-f", "-"], input_text=y)


def apply(ctx: str, ns: str, manifests: str) -> str:
    return run(["kubectl", "--context", ctx, "apply", "-n", ns, "-f", "-"],
               input_text=manifests).stdout


def delete(ctx: str, ns: str, manifests: str) -> str:
    return run(["kubectl", "--context", ctx, "delete", "-n", ns,
                "--ignore-not-found", "-f", "-"], input_text=manifests).stdout


def deployed_names(ctx: str, ns: str) -> set[str]:
    r = run(["kubectl", "--context", ctx, "get", "deploy", "-n", ns, "-o", "name"], check=False)
    return {line.split("/", 1)[1] for line in r.stdout.split() if "/" in line}


def git(args: list[str], cwd: Path) -> str:
    r = run(["git", "-C", str(cwd), *args], check=False)
    return r.stdout.strip() if r.returncode == 0 else ""
