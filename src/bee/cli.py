"""bee — thin CLI (기계적 배치). 엔진=chart(helm) · 데이터=values · 검증=CI.

GENESIS 규칙: 파생은 차트가 한다(1) · 검증은 CI 가 한다 — CLI 는 전 경로 **경고만**(2·G6) ·
좌표는 values 데이터(3) · 소스=멤버십(5) · 서브그래프만(6) · pin+명시 갱신(8).
CLI 에 derive/gate 를 추가하지 마라.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
import yaml

from bee import kube, resolver
from bee import snapshot as snap_mod
from bee import workspace as wsm

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="bee — thin CLI. 단일 기준: GENESIS.md",
)

OK, ERR = typer.colors.GREEN, typer.colors.RED


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    interactive: bool = typer.Option(False, "-i", "--interactive", help="대화형 REPL"),
):
    """bee — thin CLI. 커맨드: render·build·up·down·status (Phase 1)."""
    if interactive and ctx.invoked_subcommand is None:
        from bee.repl import repl

        repl()
        raise typer.Exit()


def _warn(msg: str) -> None:
    typer.secho(f"⚠ {msg}", fg=typer.colors.YELLOW, err=True)


def _fail(msg: str, code: int = 2):
    typer.secho(msg, fg=ERR, err=True)
    raise typer.Exit(code)


def _ok(msg: str) -> None:
    typer.secho(f"  ✓ {msg}", fg=OK)


def _yaml_at(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_ctx() -> tuple[Path, wsm.Workspace]:
    root = wsm.find_root(Path.cwd())
    return root, wsm.load_workspace(root)


def _cluster(ws: wsm.Workspace, remote_ok: bool = False) -> str:
    ctx = ws.cluster_context
    if not ctx:
        _fail("cluster.context 없음 — bee.workspace.yaml 확인")
    if not ctx.startswith("kind-") and not remote_ok:
        _fail(f"비-kind 컨텍스트 {ctx!r} — 인너루프 클러스터(개발자 전용)인지 확인 후 --remote-ok (G7 얇은 가드)")
    return ctx


def _products(ws: wsm.Workspace, root: Path) -> dict[str, str]:
    p = wsm.platform_yaml_path(ws, root)
    if not p:
        return {}
    prods = ((_yaml_at(p).get("spec") or {}).get("products")) or {}
    return {k: (v or {}).get("namespace") for k, v in prods.items()}


def _namespace(name: str, mdata: dict, products: dict[str, str]) -> str:
    prod = (mdata.get("spec") or {}).get("product")
    ns = products.get(prod)
    if not ns:
        _fail(f"{name}: product {prod!r} 의 namespace 미정의 — platform.yaml products 확인")
    return ns


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


def _render(chart: Path, mdir: Path, env: str, name: str) -> str:
    values = mdir / f"values-{env}.yaml"
    if not values.exists():
        _fail(f"{name}: values-{env}.yaml 없음: {mdir}")
    r = subprocess.run(
        ["helm", "template", name, str(chart), "-f", str(mdir / "module.yaml"), "-f", str(values)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _fail(f"{name}: helm 렌더 실패\n{r.stderr.strip()}")
    return r.stdout


def _image_tag(mdir: Path) -> str:
    m, v = _yaml_at(mdir / "module.yaml"), _yaml_at(mdir / "values-local.yaml")
    image = ((m.get("spec") or {}).get("image") or {}).get("name")
    return f"{v.get('registry')}/{image}:{v.get('imageTag', 'local')}"


def plan(ws: wsm.Workspace, root: Path, roots: list[str] | None = None):
    """locals+snapshot 병합 → 서브그래프(의존 먼저). 멤버십: local 이 이긴다(규칙 5·6·7)."""
    overrides = wsm.override_dirs(ws, root)
    env_dir = wsm.resolve_snapshot_env_dir(ws, root)
    snaps = snap_mod.load_snapshot(env_dir)
    specs: dict[str, resolver.ModuleSpec] = {}
    for n, sm in snaps.items():
        s = resolver.load_module_file(sm.module_yaml)
        if s:
            specs[n] = s
    specs.update(resolver.load_modules(overrides))
    targets = roots or sorted(overrides)
    if not targets:
        _fail("편집 표면이 비었다 — bee.workspace.yaml 의 local 에 모듈 등록")
    res = resolver.resolve_workspace(specs, targets)
    if res.cycle:
        _fail(f"순환 의존: {', '.join(res.cycle)}")
    if res.missing:
        _warn(f"의존 누락(local·snapshot 어디에도 없음): {', '.join(res.missing)} — 건너뜀")
    return overrides, snaps, res


def _module_data(name: str, overrides: dict[str, Path], snaps: dict) -> dict:
    if name in overrides:
        return _yaml_at(overrides[name] / "module.yaml")
    return _yaml_at(snaps[name].module_yaml)


def _write_lock(root: Path, ws: wsm.Workspace, overrides: dict[str, Path]) -> str:
    """up 이 snapshot SHA + local 커밋을 pin (규칙 8). Phase 1 은 기록 — refresh 의미는 Phase 2."""
    snap_repo = ws.snapshot_repo or ""
    snap_path = (root / snap_repo) if not Path(snap_repo).is_absolute() else Path(snap_repo)
    commit = kube.git(["rev-parse", "HEAD"], snap_path)
    lock: dict = {
        "snapshot": {"repo": ws.snapshot_repo, "env": ws.env, "ref": ws.snapshot_ref, "commit": commit},
        "local": {},
    }
    for name, p in sorted(overrides.items()):
        lock["local"][name] = {
            "path": str(p),
            "commit": kube.git(["rev-parse", "HEAD"], p),
            "dirty": bool(kube.git(["status", "--porcelain"], p)),
        }
    f = root / wsm.LOCK_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(lock, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return commit


# ── impl (커맨드·REPL 공용) ────────────────────────────────────────────────────
def build_impl(names: list[str], root: Path, ws: wsm.Workspace) -> None:
    overrides = wsm.override_dirs(ws, root)
    cluster = _cluster(ws).removeprefix("kind-")
    for name in names:
        if name not in overrides:
            _fail(f"{name!r} 는 from-local 이 아니다 — build 는 편집 표면 전용(규칙 5)")
        tag = _image_tag(overrides[name])
        kube.docker_build(tag, overrides[name])
        kube.kind_load(tag, cluster)
        _ok(f"{name}: docker build → kind load ({tag})")


def up_impl(roots: list[str] | None, root: Path, ws: wsm.Workspace, *, no_build: bool = False,
            remote_ok: bool = False) -> None:
    ctx = _cluster(ws, remote_ok)
    overrides, snaps, res = plan(ws, root, roots)
    products = _products(ws, root)
    chart = wsm.chart_dir(ws, root)
    pyaml = wsm.platform_yaml_path(ws, root)
    for name in res.order:
        if name in overrides:
            mdir = overrides[name]
            if not no_build:
                tag = _image_tag(mdir)
                kube.docker_build(tag, mdir)
                kube.kind_load(tag, ctx.removeprefix("kind-"))
            _chart_warnings(mdir / "module.yaml", chart, pyaml)
            manifests, src = _render(chart, mdir, "local", name), "local"
        elif name in snaps:
            sm = snaps[name]
            manifests, src = "\n---\n".join(p.read_text(encoding="utf-8") for p in sm.manifests), "snapshot"
        else:
            continue  # missing 은 plan 에서 이미 경고
        ns = _namespace(name, _module_data(name, overrides, snaps), products)
        kube.ensure_namespace(ctx, ns)
        kube.apply(ctx, ns, manifests)
        _ok(f"{name} ({src}) → apply -n {ns}")
    commit = _write_lock(root, ws, overrides)
    typer.echo(f"  pin: snapshot@{commit[:7] or '(없음)'} → {wsm.LOCK_FILE}")


def down_impl(root: Path, ws: wsm.Workspace, *, remote_ok: bool = False) -> None:
    ctx = _cluster(ws, remote_ok)
    overrides, snaps, res = plan(ws, root)
    products = _products(ws, root)
    chart = wsm.chart_dir(ws, root)
    for name in reversed(res.order):  # 의존 역순으로 내림
        if name in overrides:
            manifests = _render(chart, overrides[name], "local", name)
        elif name in snaps:
            manifests = "\n---\n".join(p.read_text(encoding="utf-8") for p in snaps[name].manifests)
        else:
            continue
        ns = _namespace(name, _module_data(name, overrides, snaps), products)
        kube.delete(ctx, ns, manifests)
        _ok(f"{name} → delete (워크로드만 — ns·데이터 보존, 규칙 9)")


def status_impl(root: Path, ws: wsm.Workspace) -> None:
    lock_f = root / wsm.LOCK_FILE
    if not lock_f.exists():
        _fail("pin 없음 — `bee up` 이 먼저다(규칙 8: 명시 pin)")
    lock = _yaml_at(lock_f)
    pin = (lock.get("snapshot") or {}).get("commit") or ""
    snap_repo = ws.snapshot_repo or ""
    snap_path = (root / snap_repo) if not Path(snap_repo).is_absolute() else Path(snap_repo)
    head = kube.git(["rev-parse", "HEAD"], snap_path)
    if pin == head:
        _ok(f"baseline 최신 — snapshot@{pin[:7]}")
        return
    _, _, res = plan(ws, root)
    changed = []
    for name in res.order:
        diff = kube.git(["diff", "--name-only", f"{pin}..{head}", "--", f"envs/{ws.env}/{name}"], snap_path)
        if diff:
            changed.append(name)
    if changed:
        _warn(f"내 서브그래프 변경: {', '.join(changed)} (pin {pin[:7]} → HEAD {head[:7]}) — 갱신은 `bee up`(명시)")
    else:
        _ok(f"pin {pin[:7]} ≠ HEAD {head[:7]} 이나 내 서브그래프 변경 없음 — 무관 churn 무시(규칙 8)")


# ── 커맨드 ────────────────────────────────────────────────────────────────────
@app.command()
def render(
    module: str,
    env: str = typer.Option("local", "-e", "--env", help="렌더 env — values-<env>.yaml 선택"),
    chart_path: Path = typer.Option(
        None, "--chart-path", help="차트 개발 전용 탈출구(G6) — 기본은 워크스페이스 coreInfra 바인딩",
    ),
):
    """모듈 렌더 — 파생은 chart 가(규칙 1), CLI 는 helm template 위임만."""
    root, ws = load_ctx()
    chart = chart_path if chart_path else wsm.chart_dir(ws, root)
    overrides = wsm.override_dirs(ws, root)
    if module not in overrides:
        known = ", ".join(sorted(overrides)) or "(없음)"
        _fail(f"모듈 없음: {module!r} — 워크스페이스 local 등록이 멤버십이다(규칙 5). 등록됨: {known}")
    mdir = overrides[module]
    _chart_warnings(mdir / "module.yaml", Path(chart), wsm.platform_yaml_path(ws, root))
    sys.stdout.write(_render(Path(chart), mdir, env, module))


@app.command()
def build(modules: list[str] = typer.Argument(None, help="기본: 편집 표면 전체")):
    """from-local 이미지 빌드 — docker build + kind load (Phase 1: local 타겟)."""
    root, ws = load_ctx()
    names = list(modules) if modules else sorted(wsm.override_dirs(ws, root))
    build_impl(names, root, ws)


@app.command()
def up(
    modules: list[str] = typer.Argument(None, help="pick (기본: 편집 표면 전체) — deps 자동 cascade"),
    no_build: bool = typer.Option(False, "--no-build", help="from-local 자동 빌드 생략"),
    remote_ok: bool = typer.Option(False, "--remote-ok", help="비-kind 컨텍스트 명시 허용(G7 가드)"),
):
    """배포 — 서브그래프(의존 먼저): local=자동빌드+render+apply, 나머지=snapshot backdrop."""
    root, ws = load_ctx()
    up_impl(list(modules) if modules else None, root, ws, no_build=no_build, remote_ok=remote_ok)


@app.command()
def down(
    remote_ok: bool = typer.Option(False, "--remote-ok", help="비-kind 컨텍스트 명시 허용(G7 가드)"),
):
    """워크로드 내림 — plan 전체 delete. 데이터·namespace 보존(규칙 9). 부분 down 없음."""
    root, ws = load_ctx()
    down_impl(root, ws, remote_ok=remote_ok)


@app.command()
def status():
    """스냅샷 pin vs HEAD — 내 서브그래프 변경만 보고(규칙 8). 자동폴링 없음."""
    root, ws = load_ctx()
    status_impl(root, ws)


if __name__ == "__main__":
    app()
