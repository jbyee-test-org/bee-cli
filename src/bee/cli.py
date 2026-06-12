"""bee — thin CLI (기계적 배치). 엔진=chart(helm) · 데이터=values · 검증=CI.

GENESIS 규칙: 파생은 차트가 한다(1) · 검증은 CI 가 한다 — CLI 는 전 경로 **경고만**(2·G6) ·
좌표는 values 데이터(3). CLI 에 derive/gate 를 추가하지 마라.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
import yaml

from bee import workspace as wsm

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="bee — thin CLI. 단일 기준: GENESIS.md",
)


@app.callback()
def main() -> None:
    """bee — thin CLI. 커맨드: render (Phase 1) · up/build/… (후속 마일스톤)."""


def _warn(msg: str) -> None:
    typer.secho(f"⚠ {msg}", fg=typer.colors.YELLOW, err=True)


def _fail(msg: str, code: int = 2) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _yaml_at(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _chart_warnings(module_yaml: Path, chart: Path, platform_yaml: Path | None) -> None:
    """G6 — 버전 대조는 전 경로 경고만. 차단은 CI lint(규칙 2)."""
    pin = ((_yaml_at(module_yaml).get("spec") or {}).get("chart") or {}).get("version")
    actual = _yaml_at(chart / "Chart.yaml").get("version")
    if pin and actual and str(pin) != str(actual):
        _warn(f"모듈 chart pin {pin} ≠ chart 실버전 {actual} — 렌더는 계속, 차단은 CI")
    if pin and platform_yaml:
        supported = ((_yaml_at(platform_yaml).get("spec") or {}).get("chart") or {}).get("supported")
        if supported:
            try:
                from packaging.specifiers import SpecifierSet
                from packaging.version import Version

                spec = SpecifierSet(supported if "," in supported else supported.replace(" ", ","))
                if Version(str(pin)) not in spec:
                    _warn(f"모듈 chart pin {pin} 이 플랫폼 지원 범위 {supported!r} 밖 — 렌더는 계속, 차단은 CI")
            except Exception as e:  # 검사 실패는 검사 생략과 동급 — 경고만
                _warn(f"chart 지원 범위 검사 불가({e}) — 건너뜀")


@app.command()
def render(
    module: str,
    env: str = typer.Option("local", "-e", "--env", help="렌더 env — values-<env>.yaml 선택"),
    chart_path: Path = typer.Option(
        None, "--chart-path",
        help="차트 개발 전용 탈출구(G6) — 기본은 워크스페이스 coreInfra 바인딩",
    ),
):
    """모듈 렌더 — 파생은 chart 가(규칙 1), CLI 는 helm template 위임만."""
    root = wsm.find_root(Path.cwd())
    ws = wsm.load_workspace(root)
    chart = chart_path if chart_path else wsm.chart_dir(ws, root)

    overrides = wsm.override_dirs(ws, root)
    if module not in overrides:
        known = ", ".join(sorted(overrides)) or "(없음)"
        _fail(f"모듈 없음: {module!r} — 워크스페이스 local 등록이 멤버십이다(규칙 5). 등록됨: {known}")
    mdir = overrides[module]
    values = mdir / f"values-{env}.yaml"
    if not values.exists():
        _fail(f"values-{env}.yaml 없음: {mdir}")

    _chart_warnings(mdir / "module.yaml", Path(chart), wsm.platform_yaml_path(ws, root))

    r = subprocess.run(
        ["helm", "template", module, str(chart),
         "-f", str(mdir / "module.yaml"), "-f", str(values)],
        capture_output=True, text=True,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    raise typer.Exit(r.returncode)


if __name__ == "__main__":
    app()
