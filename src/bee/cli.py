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
    actual = _yaml_at(chart / "Chart.yaml").get("version") if isinstance(chart, Path) else None
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


def _chart_source(ws: wsm.Workspace, root: Path):
    """chart 해석(G6) — coreInfra.chartRef(oci://…)가 있으면 OCI(모듈 pin 이 --version), 없으면 경로."""
    return ws.chart_ref if ws.chart_ref else wsm.chart_dir(ws, root)


def _pin(mdir: Path) -> str | None:
    return ((_yaml_at(mdir / "module.yaml").get("spec") or {}).get("chart") or {}).get("version")


def _render(chart, mdir: Path, env: str, name: str, set_values: tuple[str, ...] = ()) -> str:
    values = mdir / f"values-{env}.yaml"
    if not values.exists():
        _fail(f"{name}: values-{env}.yaml 없음: {mdir}")
    cmd = ["helm", "template", name, str(chart), "-f", str(mdir / "module.yaml"), "-f", str(values)]
    if isinstance(chart, str) and chart.startswith("oci://"):
        ver = _pin(mdir)
        if ver:
            cmd += ["--version", str(ver)]
    for sv in set_values:
        cmd += ["--set", sv]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _fail(f"{name}: helm 렌더 실패\n{r.stderr.strip()}")
    return r.stdout


def _image_tag(mdir: Path) -> str:
    m, v = _yaml_at(mdir / "module.yaml"), _yaml_at(mdir / "values-local.yaml")
    image = ((m.get("spec") or {}).get("image") or {}).get("name")
    return f"{v.get('registry')}/{image}:{v.get('imageTag', 'local')}"


def _migrations_cm(name: str, mdir: Path, ns: str) -> str | None:
    """db.migrations SQL → ConfigMap 매니페스트 — kubectl --dry-run 위임(G14②, 변환 0)."""
    db = (_yaml_at(mdir / "module.yaml").get("spec") or {}).get("db") or {}
    mig = db.get("migrations")
    if not mig:
        return None
    sql_dir = mdir / mig
    if not sql_dir.is_dir():
        _fail(f"{name}: 마이그레이션 디렉토리 없음 — {sql_dir}")
    cm = kube.run(["kubectl", "create", "configmap", f"{name}-migrations", "-n", ns,
                   f"--from-file={sql_dir}", "--dry-run=client", "-o", "yaml"]).stdout
    # Flyway Job(wave 1)이 마운트하므로 CM 은 wave 0 — kubectl annotate 위임(변환 0)
    return kube.run(["kubectl", "annotate", "--local", "-f", "-", "-o", "yaml",
                     "argocd.argoproj.io/sync-wave=0"], input_text=cm).stdout


def _has_db(name: str, overrides: dict, snaps: dict) -> bool:
    return bool((_module_data(name, overrides, snaps).get("spec") or {}).get("db"))


def _all_specs(overrides: dict[str, Path], snaps: dict) -> dict[str, "resolver.ModuleSpec"]:
    """local + snapshot 의 전체 module spec — depth 계산용 그래프."""
    specs: dict = {}
    for n, sm in snaps.items():
        s = resolver.load_module_file(sm.module_yaml)
        if s:
            specs[n] = s
    specs.update(resolver.load_modules(overrides))
    return specs


def _migration_wave(name: str, specs: dict) -> int:
    """의존성 깊이 → migrationWave (1 + depth). deps-first 를 ArgoCD sync-wave 로(G14③)."""
    return 1 + resolver.depth(specs, name)


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


def _snapshot_path(root: Path, ws: wsm.Workspace) -> Path:
    repo = ws.snapshot_repo or ""
    if not repo:
        _fail("snapshot 경로 필요: bee.workspace.yaml 의 snapshot.repo")
    p = Path(repo) if Path(repo).is_absolute() else (root / repo)
    if not p.exists():
        _fail(f"snapshot 경로 없음(로컬만 지원, git URL 은 후속): {p}")
    return p


def _write_lock(root: Path, ws: wsm.Workspace, overrides: dict[str, Path]) -> str:
    """up 이 snapshot SHA + local 커밋을 pin (규칙 8). Phase 1 은 기록 — refresh 의미는 Phase 2."""
    snap_path = _snapshot_path(root, ws)
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
    chart = _chart_source(ws, root)
    pyaml = wsm.platform_yaml_path(ws, root)
    for name in res.order:
        if name not in overrides and name not in snaps:
            continue  # missing 은 plan 에서 이미 경고
        ns = _namespace(name, _module_data(name, overrides, snaps), products)
        if name in overrides:
            mdir = overrides[name]
            if not no_build:
                tag = _image_tag(mdir)
                kube.docker_build(tag, mdir)
                kube.kind_load(tag, ctx.removeprefix("kind-"))
            _chart_warnings(mdir / "module.yaml", chart, pyaml)
            sets = (f"namespace={ns}",)
            if _has_db(name, overrides, snaps):
                sets += (f"migrationWave={_migration_wave(name, _all_specs(overrides, snaps))}",)
            manifests, src = _render(chart, mdir, "local", name, set_values=sets), "local"
        else:
            sm = snaps[name]
            manifests, src = "\n---\n".join(p.read_text(encoding="utf-8") for p in sm.manifests), "snapshot"
        kube.ensure_namespace(ctx, ns)
        if _has_db(name, overrides, snaps):  # Job 은 불변 필드 — 재적용 전 교체(G14)
            kube.run(["kubectl", "--context", ctx, "-n", ns, "delete", "job",
                      f"{name}-migrate", "--ignore-not-found"])
        if name in overrides:
            cm = _migrations_cm(name, overrides[name], ns)
            if cm:
                kube.apply(ctx, ns, cm)
        kube.apply(ctx, ns, manifests)
        _ok(f"{name} ({src}) → apply -n {ns}")
    commit = _write_lock(root, ws, overrides)
    typer.echo(f"  pin: snapshot@{commit[:7] or '(없음)'} → {wsm.LOCK_FILE}")


def down_impl(root: Path, ws: wsm.Workspace, *, remote_ok: bool = False) -> None:
    ctx = _cluster(ws, remote_ok)
    overrides, snaps, res = plan(ws, root)
    products = _products(ws, root)
    chart = _chart_source(ws, root)
    for name in reversed(res.order):  # 의존 역순으로 내림
        if name not in overrides and name not in snaps:
            continue
        ns = _namespace(name, _module_data(name, overrides, snaps), products)
        if name in overrides:
            manifests = _render(chart, overrides[name], "local", name, set_values=(f"namespace={ns}",))
        else:
            manifests = "\n---\n".join(p.read_text(encoding="utf-8") for p in snaps[name].manifests)
        kube.delete(ctx, ns, manifests)
        _ok(f"{name} → delete (워크로드만 — ns·데이터 보존, 규칙 9)")


def status_impl(root: Path, ws: wsm.Workspace) -> None:
    lock_f = root / wsm.LOCK_FILE
    if not lock_f.exists():
        typer.secho("  pin 없음 — 첫 `bee up` 이 snapshot pin 을 기록한다(규칙 8)", dim=True)
        return
    lock = _yaml_at(lock_f)
    pin = (lock.get("snapshot") or {}).get("commit") or ""
    snap_path = _snapshot_path(root, ws)
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


def publish_impl(env: str, targets: list[str] | None, root: Path, ws: wsm.Workspace, *,
                 digest: str = "", push: bool = False) -> None:
    """렌더(digest pin) + 스냅샷 엔트리 기록 + 커밋 — 기계적 기록만, 검증은 CI 게이트1(규칙 2).

    공유 env 전용(규칙 7). CI 가 headless 로 재사용하는 경로(G5) — dev 는 직접 커밋(G8).
    """
    if env == "local":
        _fail("publish 는 공유 env 전용 — 로컬 상태는 스냅샷에 기록하지 않는다(규칙 7). "
              "로컬 구성 공유는 워크스페이스 파일로.")
    overrides = wsm.override_dirs(ws, root)
    names = targets or sorted(overrides)
    unknown = [n for n in names if n not in overrides]
    if unknown:
        _fail(f"publish 는 편집 표면(from-local) 전용(규칙 5): {', '.join(unknown)}")
    if digest and len(names) != 1:
        _fail("--digest 는 모듈 1개와 함께만 사용한다 (모듈별 digest 가 다르다)")
    chart = _chart_source(ws, root)
    pyaml = wsm.platform_yaml_path(ws, root)
    products = _products(ws, root)
    snap_path = _snapshot_path(root, ws)
    env_dir = snap_path / "envs" / env
    snaps = snap_mod.load_snapshot(env_dir)  # depth 그래프 = 스냅샷의 의존 모듈 + 이번 모듈
    for name in names:
        mdir = overrides[name]
        _chart_warnings(mdir / "module.yaml", chart, pyaml)
        ns = _namespace(name, _yaml_at(mdir / "module.yaml"), products)
        sets = (f"namespace={ns}",) + ((f"imageDigest={digest}",) if digest else ())
        if (_yaml_at(mdir / "module.yaml").get("spec") or {}).get("db"):
            sets += (f"migrationWave={_migration_wave(name, _all_specs({name: mdir}, snaps))}",)
        manifests = _render(chart, mdir, env, name, set_values=sets)
        spec = _yaml_at(mdir / "module.yaml").get("spec") or {}
        prov = {
            "module": name,
            "repoUrl": kube.git(["remote", "get-url", "origin"], mdir) or str(mdir),
            "moduleCommit": kube.git(["rev-parse", "HEAD"], mdir),
            "imageDigest": digest,
            "chartVersion": str((spec.get("chart") or {}).get("version") or ""),
            "dependsOn": list(spec.get("dependsOn") or []),
        }
        cm = _migrations_cm(name, mdir, ns)
        if cm:
            manifests = manifests + "\n---\n" + cm
        db_dir = mdir / "db"
        snap_mod.write_entry(env_dir, name, mdir / "module.yaml", manifests, provenance=prov,
                             db_src=db_dir if db_dir.is_dir() else None)
        _ok(f"{name} → envs/{env}/{name}  "
            + ("(digest pin)" if digest else "(digest 없음 — 게이트1이 차단한다)"))
    kube.run(["git", "-C", str(snap_path), "add", f"envs/{env}"])
    if not kube.git(["status", "--porcelain", "--", f"envs/{env}"], snap_path):
        typer.secho("  무변경 — 커밋 생략 (diff = 실질 변경, G8)", dim=True)
        return
    kube.run(["git", "-C", str(snap_path), "commit", "-q", "-m", f"publish({env}): {' '.join(names)}"])
    _ok(f"snapshot 커밋 {kube.git(['rev-parse', '--short', 'HEAD'], snap_path)}")
    if push:
        kube.run(["git", "-C", str(snap_path), "push", "-q"])
        _ok("snapshot push — 적용은 CD(ArgoCD)의 몫(G5: CLI 의 공유환경 출력은 git 까지)")


def pull_impl(modules: list[str], root: Path, ws: wsm.Workspace) -> None:
    """스냅샷(backdrop) 모듈 → 편집 표면: clone(provenance.repoUrl) + 워크스페이스 등록.

    멤버십 변경이 전부(규칙 5) — derive 0. 목적지 `repos/<name>` 가 이미 있으면
    clone 생략(멱등)하고 등록만 한다. URL 출처 = provenance(G8 카탈로그 통합).
    """
    env_dir = wsm.resolve_snapshot_env_dir(ws, root)
    snaps = snap_mod.load_snapshot(env_dir)
    changed = False
    for name in modules:
        if name in ws.locals:
            _warn(f"{name}: 이미 편집 표면(from-local) — 건너뜀")
            continue
        if name not in snaps:
            _fail(f"{name}: 스냅샷(envs/{ws.env})에 없음 — pull 대상은 backdrop 모듈")
        dest = root / "repos" / name
        if dest.exists():
            typer.secho(f"  {name}: 경로 존재 — clone 생략(멱등), 등록만: {dest}", dim=True)
        else:
            sm = snaps[name]
            url = (_yaml_at(sm.provenance).get("repoUrl") or "") if sm.provenance else ""
            if not url.startswith(("http", "git@")):
                _fail(f"{name}: provenance.repoUrl 없음/비원격({url!r}) — clone 불가")
            kube.run(["git", "clone", "--quiet", url, str(dest)])
            _ok(f"{name}: clone {url} → {dest}")
        ws.locals[name] = wsm.LocalOverride(name=name, path=Path("repos") / name)
        changed = True
        _ok(f"{name}: 워크스페이스 local: 등록 — 소스=멤버십(규칙 5), 이제 from-local")
    if changed:
        wsm.save_workspace(root, ws)
        typer.secho("  (bee.workspace.yaml 갱신 — 등록 해제는 local: 항목 제거)", dim=True)


def new_impl(name: str, root: Path, ws: wsm.Workspace) -> None:
    """starter 복사 + 이름 치환 + 워크스페이스 등록 (G5). 치환은 이름만 —
    변형(언어/유형)은 조건 분기가 아니라 starter 디렉토리 추가로."""
    import shutil

    if name in ws.locals:
        _fail(f"{name}: 이미 편집 표면에 등록됨")
    dest = root / "repos" / name
    if dest.exists():
        _fail(f"{name}: 경로 이미 존재 — {dest}")
    starter = wsm.core_infra_dir(ws, root) / "starter" / "default"
    if not starter.is_dir():
        _fail(f"starter 없음: {starter} — core-infra/starter/default 확인")
    shutil.copytree(starter, dest)
    for f in dest.rglob("*"):
        if f.is_file():
            try:
                f.write_text(f.read_text(encoding="utf-8").replace("__MODULE__", name), encoding="utf-8")
            except UnicodeDecodeError:
                pass  # 바이너리는 치환 대상 아님
    _ok(f"{name}: starter 복사 + 치환 → {dest}")
    kube.run(["git", "-C", str(dest), "init", "-q"])
    kube.run(["git", "-C", str(dest), "add", "-A"])
    kube.run(["git", "-C", str(dest), "commit", "-q", "-m", f"init: {name} — bee new (starter)"])
    ws.locals[name] = wsm.LocalOverride(name=name, path=Path("repos") / name)
    wsm.save_workspace(root, ws)
    _ok(f"{name}: git init + 워크스페이스 등록 — 소스=멤버십(규칙 5)")
    typer.secho(f"  다음: bee render {name} · bee up {name} · 리모트는 gh repo create 후 push", dim=True)


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
    chart = chart_path if chart_path else _chart_source(ws, root)
    overrides = wsm.override_dirs(ws, root)
    if module not in overrides:
        known = ", ".join(sorted(overrides)) or "(없음)"
        _fail(f"모듈 없음: {module!r} — 워크스페이스 local 등록이 멤버십이다(규칙 5). 등록됨: {known}")
    mdir = overrides[module]
    _chart_warnings(mdir / "module.yaml", chart, wsm.platform_yaml_path(ws, root))
    ns = _namespace(module, _yaml_at(mdir / "module.yaml"), _products(ws, root))
    sys.stdout.write(_render(chart, mdir, env, module, set_values=(f"namespace={ns}",)))


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
def publish(
    env: str = typer.Argument(..., help="공유 env (dev/staging/prod) — local 금지(규칙 7)"),
    modules: list[str] = typer.Argument(None, help="기본: 편집 표면 전체"),
    digest: str = typer.Option("", "--digest", help="이미지 digest (CI 가 주입 — 모듈 1개와만)"),
    push: bool = typer.Option(False, "--push", help="커밋 후 원격 push"),
):
    """스냅샷 레포 커밋 — 이미지 push 는 build/CI 몫(분리). 검증은 CI 게이트1(규칙 2)."""
    root, ws = load_ctx()
    publish_impl(env, list(modules) if modules else None, root, ws, digest=digest, push=push)


@app.command()
def new(name: str = typer.Argument(..., help="신규 모듈 이름")):
    """신규 모듈 — starter 복사 + 이름 치환 + 워크스페이스 등록(G5). pull 의 쌍둥이(신규 진입)."""
    root, ws = load_ctx()
    new_impl(name, root, ws)


@app.command()
def pull(modules: list[str] = typer.Argument(..., help="스냅샷(backdrop) 모듈 → 편집 표면")):
    """스냅샷 모듈을 편집 표면으로 — clone(provenance.repoUrl) + 워크스페이스 등록(규칙 5)."""
    root, ws = load_ctx()
    pull_impl(list(modules), root, ws)


@app.command()
def status():
    """스냅샷 pin vs HEAD — 내 서브그래프 변경만 보고(규칙 8). 자동폴링 없음."""
    root, ws = load_ctx()
    status_impl(root, ws)


if __name__ == "__main__":
    app()
