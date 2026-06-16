"""클러스터·이미지 위임 — docker/kind/kubectl 호출만. 로직 없음(기계적 배치, 규칙 1·2)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    """외부 도구 실패."""


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise ToolError(f"$ {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


def docker_build(tag: str, context_dir: Path, secrets: tuple[tuple[str, str, str | None], ...] = ()) -> None:
    """docker build. secrets=((id, env_var, value), …) → BuildKit `--mount=type=secret`(사설 registry
    토큰 등, G30). value(bee.secrets.local.yaml 또는 실제 env, G31)를 subprocess env 에 넣고 env= 로
    전달 — 토큰은 레이어에 안 박힘. docker 29 는 BuildKit 기본."""
    import os
    sub_env = dict(os.environ)
    cmd = ["docker", "build", "-q", "-t", tag]
    for sid, senv, sval in secrets:
        if sval:
            sub_env[senv] = sval
        cmd += ["--secret", f"id={sid},env={senv}"]
    cmd.append(str(context_dir))
    run(cmd, env=sub_env)


def kind_load(tag: str, cluster: str) -> None:
    run(["kind", "load", "docker-image", tag, "--name", cluster])


def docker_push(tag: str) -> str:
    """push 후 RepoDigests[0] 의 digest(`sha256:…`) 반환 — bee build --push(G31/C) 가
    아웃터 이미지를 registry 에 올리고 digest 를 얻어 `bee publish --digest` 로 잇는다.
    매니페스트는 이 digest 를 pin(태그 무관)."""
    run(["docker", "push", tag])
    r = run(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", tag])
    repo_digest = r.stdout.strip()  # registry/image@sha256:…
    return repo_digest.split("@", 1)[1] if "@" in repo_digest else ""


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
