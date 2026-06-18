"""bee — thin CLI (기계적 배치). 엔진=chart(helm) · 데이터=values · 검증=CI.

GENESIS 규칙: 파생은 차트가 한다(1) · 검증은 CI 가 한다 — CLI 는 전 경로 **경고만**(2·G6) ·
좌표는 values 데이터(3) · 소스=멤버십(5) · 서브그래프만(6) · pin+명시 갱신(8).
CLI 에 derive/gate 를 추가하지 마라.
"""

from __future__ import annotations

import hashlib
import os
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

OK, ERR, WARN = typer.colors.GREEN, typer.colors.RED, typer.colors.YELLOW

# substrate(infra) 서브커맨드 — 적용·점검(G26). doctor 처럼 setup/preflight 성격(REPL 비결선).
substrate_app = typer.Typer(no_args_is_help=True, help="substrate(infra) 적용 — 정적+helm 위임(G26)")
app.add_typer(substrate_app, name="substrate")


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version as _pkg_version

        typer.echo(f"bee {_pkg_version('bee-cli')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    interactive: bool = typer.Option(False, "-i", "--interactive", help="대화형 REPL"),
    version: bool = typer.Option(
        False, "--version", "-V", help="버전 출력 후 종료", callback=_version_callback, is_eager=True
    ),
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


def _render(chart, mdir: Path, env: str, name: str, set_values: tuple[str, ...] = (),
            platform_values: dict | None = None) -> str:
    import tempfile
    values = mdir / f"values-{env}.yaml"
    if not values.exists():
        _fail(f"{name}: values-{env}.yaml 없음: {mdir}")
    # platform 데이터(G37 resources 프로파일 · G36/G28③ provides[provider 바인딩]) = .Values.{resources,provides}
    # — **낮은 우선순위 -f**(values-<env> 가 override, 규칙 3). chart 가 spec.uses / provider 코덱으로 룩업·렌더
    # (rule 1 — 파생은 차트). bee 는 기계적 전달만(배치 아닌 derive 0).
    pre, tmp = [], None
    if platform_values:
        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(platform_values, f, allow_unicode=True)
        pre = ["-f", tmp]
    cmd = ["helm", "template", name, str(chart), *pre, "-f", str(mdir / "module.yaml"), "-f", str(values)]
    if isinstance(chart, str) and chart.startswith("oci://"):
        ver = _pin(mdir)
        if ver:
            cmd += ["--version", str(ver)]
    for sv in set_values:
        cmd += ["--set", sv]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp:
            os.unlink(tmp)
    if r.returncode != 0:
        _fail(f"{name}: helm 렌더 실패\n{r.stderr.strip()}")
    return r.stdout


def _platform_values(ws: wsm.Workspace, root: Path) -> dict:
    """chart 가 룩업할 **저우선 default** 데이터(`_render` 의 가장 낮은 -f — 모듈 values 가 override):
    resources(G37 — spec.uses 룩업) · provides(G36/G28③ provider 코덱 dispatch) · **registry(G54 —
    워크스페이스 imageRegistry, env 불변)**. bee 는 *전달*만(derive 0); 룩업·렌더는 chart(rule 1).

    registry 가 **저우선**인 이유: 보통 모듈은 ws.imageRegistry 를 쓰지만, 공개 백엔드 이미지를 쓰는
    특수 모듈(예: nginx-probe = `traefik/whoami`)은 values-<env>.registry 로 *override* 한다.
    그래서 --set(고우선)이 아니라 default 로 깔고 values 가 이긴다."""
    try:
        p = wsm.platform_yaml_path(ws, root)
    except wsm.WorkspaceError:
        p = None
    spec = (_yaml_at(p).get("spec") or {}) if p else {}
    out: dict = {}
    if spec.get("resources"):
        out["resources"] = spec["resources"]
    if (spec.get("substrate") or {}).get("provides"):
        out["provides"] = spec["substrate"]["provides"]
    if ws.image_registry:
        out["registry"] = ws.image_registry   # 저우선 default — 모듈 values 가 override 가능(probe 등)
    return out


def _image_ref(ws: wsm.Workspace, mdir: Path) -> tuple[str | None, str | None]:
    """(registry, image) — registry 는 **워크스페이스 imageRegistry**(G54 — env 불변 좌표;
    G52 same-artifact 가 사다리 전체 동일 registry 를 요구), image 는 module.yaml spec.image.name.
    빌드 push 타깃 · render 이미지 ref 양쪽 공용. (구: values-<env>.registry — env별 중복이라 회수.)"""
    image = ((_yaml_at(mdir / "module.yaml").get("spec") or {}).get("image") or {}).get("name")
    return ws.image_registry, image


def _image_tag(ws: wsm.Workspace, mdir: Path) -> str:
    registry, image = _image_ref(ws, mdir)
    v = _yaml_at(mdir / "values-local.yaml")
    return f"{registry}/{image}:{v.get('imageTag', 'local')}"


def _build_secrets(ws: wsm.Workspace, root: Path) -> tuple[tuple[str, str, str | None], ...]:
    """워크스페이스 buildRegistries(G30) → docker BuildKit secret ((id, env_var, value), …).
    id=`<name>_token` — Dockerfile 의 `--mount=type=secret,id=<name>_token` 과 매칭. value(토큰)는
    **`bee.secrets.local.yaml`[env_var]**(워크스페이스 시크릿, G31) 우선, 없으면 실제 env(CI). 둘 다
    없으면 None(doctor 가 경고·빌드는 명확히 실패). 빌드는 빌더 책임 — bee 는 토큰을 *전달*만(레이어 미박힘)."""
    secrets = wsm.load_workspace_secrets(root)
    out = []
    for r in ws.build_registries:
        name, tenv = r.get("name"), r.get("tokenEnv")
        if name and tenv:
            out.append((f"{name}_token", tenv, secrets.get(tenv) or os.environ.get(tenv)))
    return tuple(out)


def _has_image(mdir: Path) -> bool:
    """image 선언 여부. 없으면 workload 없는 schema 모듈(G21) — build·Deployment 생략."""
    return bool(((_yaml_at(mdir / "module.yaml").get("spec") or {}).get("image") or {}).get("name"))


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
    # Flyway Job(음수 wave)이 마운트하므로 CM 은 -200 — kubectl annotate 위임(변환 0)
    return kube.run(["kubectl", "annotate", "--local", "-f", "-", "-o", "yaml",
                     "argocd.argoproj.io/sync-wave=-200"], input_text=cm).stdout


def _has_db(name: str, overrides: dict, snaps: dict) -> bool:
    return bool((_module_data(name, overrides, snaps).get("spec") or {}).get("db"))


def _migrations_hash(mdir: Path) -> str:
    """마이그레이션 페이로드 content-hash[:12] (G18②) — Flyway Job 이름 접미사가 되어
    **내용 변경 시에만** 재실행을 트리거(무변경=같은 해시=no-op, 재실행 루프 없음).
    입력: db spec(grants·schema 선언 포함) + SQL 파일 내용 + chart pin(템플릿 로직 변경 반영).
    이건 파생이 아니라 좌표(migrationWave·namespace 동류) — 매니페스트는 차트가 만든다(규칙 1)."""
    m = _yaml_at(mdir / "module.yaml")
    db = (m.get("spec") or {}).get("db") or {}
    h = hashlib.sha256()
    h.update(yaml.safe_dump(db, sort_keys=True, allow_unicode=True).encode("utf-8"))
    h.update((_pin(mdir) or "").encode("utf-8"))  # chart pin — 템플릿(grants 파생) 변경도 재실행
    mig = db.get("migrations")
    if mig and (mdir / mig).is_dir():
        for f in sorted((mdir / mig).rglob("*")):
            if f.is_file():
                h.update(f.relative_to(mdir / mig).as_posix().encode("utf-8"))
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _module_uses(spec: dict) -> set[str]:
    """모듈이 *사용*하는 substrate capability — 논리 어휘(db/routing)에서 **파생**(G36).
    events 는 접속형으로 걷어내(G42) 어휘 없음 → capability 파생 대상 아님. btc 등 접속형도 어휘 미덮(G41)."""
    s = spec or {}
    return {cap for cap in ("db", "routing") if s.get(cap)}


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
    """의존성 깊이 → 음수 migrationWave (-100 + depth). 앱(wave 0) 보다 먼저,
    Ingress health 게이트와 무관. deps-first(의존 모듈 먼저)를 ArgoCD sync-wave 로(G14③)."""
    return -100 + resolver.depth(specs, name)


def _grant_warnings(name: str, overrides: dict[str, Path], snaps: dict) -> None:
    """cross-schema grant 의 owner(db.schema)가 dependsOn-폐포에 없으면 경고(G18①ⓘ 불변식).

    불변식: grant 대상 schema 는 자기 또는 dependsOn-조상이 소유(db.schema 선언)해야 한다.
    그래야 음수 wave 의 deps-first 가 owner 의 CREATE→member 의 GRANT 순서를 보장 —
    즉 **module-granular 순서로 충분**(전역 V-합집합 불필요, G14③). 위반은 grant Job 이
    schema 부재로 실패한다. 차단이 아니라 경고(규칙 2 — 하드 게이트는 CI 의 env-wide 체크)."""
    db = (_module_data(name, overrides, snaps).get("spec") or {}).get("db") or {}
    grants = db.get("grants") or []
    if not grants:
        return
    specs = _all_specs(overrides, snaps)
    closure = resolver._reachable(specs, name)  # 자기 + dependsOn 조상
    owned = {
        s for n in closure if n in overrides or n in snaps
        for s in [((_module_data(n, overrides, snaps).get("spec") or {}).get("db") or {}).get("schema")]
        if s
    }
    for g in grants:
        s = g.get("schema")
        if s and s not in owned:
            _warn(f"{name}: grant schema {s!r} 의 owner(db.schema)가 dependsOn-폐포에 없음 — "
                  f"소유 모듈이 `db.schema: {s}` 를 선언하고 dependsOn 에 두라(G18① 불변식). "
                  f"미충족 시 grant Job 이 schema 부재로 실패. 차단은 CI.")


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
    """snapshot 레포 루트 — 로컬 경로 또는 원격 fetch 캐시(소비). lock 기록·publish 가 사용."""
    try:
        return wsm.snapshot_repo_dir(ws, root)
    except wsm.WorkspaceError as e:
        _fail(str(e))


def _write_lock(root: Path, ws: wsm.Workspace, overrides: dict[str, Path]) -> str:
    """up 이 snapshot + **coreInfra**(G50 대칭) SHA + local 커밋을 pin (규칙 8).
    **재현성 앵커**: ref 가 브랜치여도(미병합 통합, G50) 해석된 commit 을 lock 에 박는다 —
    브랜치는 fetch 출처일 뿐, lock 의 commit 이 재현 기준(브랜치 움직여도 명시 재-up 까지 고정)."""
    snap_path = _snapshot_path(root, ws)
    commit = kube.git(["rev-parse", "HEAD"], snap_path)
    lock: dict = {
        "snapshot": {"repo": ws.snapshot_repo, "env": ws.env, "ref": ws.snapshot_ref, "commit": commit},
        "local": {},
    }
    try:  # coreInfra 핀(G50 ② — substrate/chart 도 브랜치 ref 활성 시 commit 앵커). path/URL 공용.
        ci_commit = kube.git(["rev-parse", "HEAD"], wsm.core_infra_dir(ws, root))
        lock["coreInfra"] = {"repo": ws.core_infra_repo or ws.core_infra,
                             "ref": ws.core_infra_ref, "commit": ci_commit}
    except Exception:
        pass  # coreInfra 미해석(OCI-only 등) — 핀 생략(snapshot 만)
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


# ── ref 스코프 (G50 — 인너루프 미병합 통합) ─────────────────────────────────────
def _ls_remote(url: str | None, ref: str) -> str:
    """원격 ref → commit(40-hex). SHA 면 그대로 · 브랜치면 ls-remote. 실패/오프라인=‘?’."""
    import re
    if not url:
        return "?"
    if re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        return ref
    r = kube.run(["git", "ls-remote", url, ref], check=False)
    return r.stdout.split()[0] if r.stdout.strip() else "?"


def _branch_scope(ws: wsm.Workspace) -> list[tuple[str, str]]:
    """현재 브랜치-스코프(ref≠main, URL 바인딩) 목록 → [(label, ref)]. 가시성 공용(doctor·orient)."""
    out = []
    for label, repo, ref in (("snapshot", ws.snapshot_repo, ws.snapshot_ref),
                             ("coreInfra", ws.core_infra_repo, ws.core_infra_ref)):
        if wsm._is_url(repo) and ref and ref != "main":
            out.append((label, ref))
    return out


def _ref_show(ws: wsm.Workspace) -> None:
    typer.secho("ref 스코프 (G50 — 인너루프 미병합 통합)", bold=True)
    any_branch = False
    for label, repo, ref in (("snapshot", ws.snapshot_repo, ws.snapshot_ref),
                             ("coreInfra", ws.core_infra_repo or ws.core_infra, ws.core_infra_ref)):
        if not wsm._is_url(repo):
            typer.secho(f"  {label:9} {ref or 'main':14} [로컬 경로 — ref 무관(git checkout)]", dim=True)
            continue
        commit = _ls_remote(repo, ref)
        if ref == "main":
            typer.secho(f"  {label:9} {ref:14} → {commit[:7]}", fg=OK)
        else:
            any_branch = True
            main_c = _ls_remote(repo, "main")
            delta = "" if commit == main_c else f"  (main {main_c[:7]} 과 다름)"
            typer.secho(f"  {label:9} {ref:14} → {commit[:7]}  ⚠ 브랜치 스코프{delta}", fg=WARN)
    if any_branch:
        typer.secho("  ⚠ 인너루프 전용 — 공유 ArgoCD 는 main 고정(규칙 7). 재현 앵커=lock commit(규칙 8). 복귀: bee ref --reset", fg=WARN)
    else:
        typer.secho("  공유 baseline(main) — 미병합 통합 없음", dim=True)


def ref_impl(ref: str | None, root: Path, ws: wsm.Workspace, *,
             snapshot: bool = False, core_infra: bool = False, reset: bool = False) -> None:
    """ref 스코프(G50) — substrate/snapshot 을 main 아닌 브랜치 ref 에서 *인너루프* 소비(미병합 통합).
    **불가침**: ① 인너루프 전용(공유 ArgoCD=main 고정, 규칙 7) · ② 재현=lock commit(브랜치는 fetch 출처) ·
    ③ ephemeral(--reset 원턴 복귀) · ④ branch ⊥ env(ref 는 dev 라인, env=dir 배포대상 — 안 겹침)."""
    if reset:
        ws.snapshot_ref = ws.core_infra_ref = "main"
        wsm.save_workspace(root, ws)
        _ok("ref → main 복귀(snapshot·coreInfra) — baseline. 적용·재-pin: `bee up`/`bee substrate up`.")
        _ref_show(ws)
        return
    if not ref:
        _ref_show(ws)
        return
    targets = [t for t, on in (("snapshot", snapshot), ("coreInfra", core_infra)) if on]
    if not targets:
        _fail("대상 명시 — -s/--snapshot · -c/--core-infra (둘 다 가능). ref 는 *미병합 통합* 대상(G50).")
    for t in targets:
        repo = ws.snapshot_repo if t == "snapshot" else ws.core_infra_repo
        if not wsm._is_url(repo):
            _fail(f"{t} 바인딩이 로컬 경로 — ref 스코프는 **URL 소비 모드 전용**(G50). "
                  f"경로 메인테이너는 git checkout 으로 브랜치 전환(솔로는 그걸로 충분).")
    if snapshot:
        ws.snapshot_ref = ref
    if core_infra:
        ws.core_infra_ref = ref
    wsm.save_workspace(root, ws)
    _ok(f"ref 설정: {' · '.join(targets)} → {ref} (미병합 통합 — 인너루프 전용 G50)")
    _warn("공유 배포 영향 없음(ArgoCD=main 고정, 규칙 7). 적용: `bee up`/`bee substrate up`(lock 이 commit 앵커). 복귀: `bee ref --reset`.")
    _ref_show(ws)


# ── impl (커맨드·REPL 공용) ────────────────────────────────────────────────────
def build_impl(names: list[str], root: Path, ws: wsm.Workspace) -> None:
    overrides = wsm.override_dirs(ws, root)
    cluster = _cluster(ws).removeprefix("kind-")
    for name in names:
        if name not in overrides:
            _fail(f"{name!r} 는 from-local 이 아니다 — build 는 편집 표면 전용(규칙 5)")
        if not _has_image(overrides[name]):
            typer.secho(f"  {name}: image 없음 — schema 모듈(G21), build 생략", dim=True)
            continue
        tag = _image_tag(ws, overrides[name])
        kube.docker_build(tag, overrides[name], _build_secrets(ws, root))
        kube.kind_load(tag, cluster)
        _ok(f"{name}: docker build → kind load ({tag})")


def build_push_impl(names: list[str], root: Path, ws: wsm.Workspace) -> None:
    """아웃터 이미지 준비(G31/C) — **워크스페이스 imageRegistry**(G54 — env 불변)로 build+push → digest 출력.
    빌드는 빌더 책임(G30)이라 CI 가 아니라 토큰 가진 로컬이 한다. **정합 가드**(소스↔이미지↔스냅샷, #2):
    커밋된 클린 트리만 빌드하고 `sha-<commit>` 으로 태깅 → CI 가 모듈@commit 체크아웃해 publish 하면
    provenance(moduleCommit=commit, imageDigest=digest)가 닫힌다. 매니페스트는 digest pin(태그 무관).
    (env 인자 없음 — 빌드 1회 → digest 가 사다리 전체 복사, G52. registry 도 env 불변, G54.)"""
    overrides = wsm.override_dirs(ws, root)
    secrets = _build_secrets(ws, root)
    for name in names:
        if name not in overrides:
            _fail(f"{name!r} 는 from-local 이 아니다 — build 는 편집 표면 전용(규칙 5)")
        mdir = overrides[name]
        if not _has_image(mdir):
            typer.secho(f"  {name}: image 없음 — schema 모듈(G21), push 생략(digest 없이 publish)", dim=True)
            continue
        # 정합 가드: 모듈은 **자체 독립 git**(G3)이어야 하고(상위 레포로 walk-up 금지), 클린·커밋 상태여야 한다.
        commit = kube.git(["rev-parse", "HEAD"], mdir)
        toplevel = kube.git(["rev-parse", "--show-toplevel"], mdir)
        if not commit or not toplevel or Path(toplevel).resolve() != mdir.resolve():
            _fail(f"{name}: 모듈 자체 git 레포 아님 — --push 는 모듈별 독립 git(G3)의 커밋된 소스 필요"
                  f"(CI 가 모듈@commit 체크아웃 → moduleCommit↔imageDigest 정합, #2). repos/{name} 에서 git init + 커밋")
        if kube.git(["status", "--porcelain"], mdir):
            _fail(f"{name}: 작업 트리 dirty — --push 는 커밋된 소스만(소스↔이미지↔스냅샷 정합, #2). 커밋 후 재시도")
        registry, image = _image_ref(ws, mdir)
        if not registry:
            _fail(f"{name}: 워크스페이스 imageRegistry 없음 — push 타깃 좌표 필요(G54). "
                  f"bee.workspace.yaml 에 `imageRegistry: <registry>` 추가.")
        tag = f"{registry}/{image}:sha-{commit[:7]}"
        kube.docker_build(tag, mdir, secrets)
        digest = kube.docker_push(tag)
        _ok(f"{name}: build+push → {tag}")
        typer.secho(f"     digest: {digest}", fg=OK, bold=True)
        # 다음 = bee snap -e dev(엔트리 — 원격 모드면 bee 가 CI dispatch 흡수, 사용자는 gh 안 침, G53).
        typer.secho(f"     다음: bee snap -e dev {name} --digest {digest}", dim=True)


def _apply_local_secrets(name: str, mdir: Path, ns: str, ctx: str) -> None:
    """인너루프 secret 충족(G27) — `secrets.local.yaml`(gitignore) → {name}-secrets Secret apply.
    아웃터는 ESO(ExternalSecret, env!=local). 이건 합성이 아니라 **적용**(kubectl create secret 위임,
    G26 정신) — bee 는 로컬 값을 Secret 으로 깔 뿐 파생하지 않는다. 계약은 module.yaml 의 spec.secrets."""
    f = mdir / "secrets.local.yaml"
    if not f.exists():
        return
    data = _yaml_at(f)
    if not data:
        return
    args = ["kubectl", "--context", ctx, "-n", ns, "create", "secret", "generic", f"{name}-secrets",
            "--dry-run=client", "-o", "yaml"]
    for k, v in data.items():
        args.append(f"--from-literal={k}={v}")
    kube.apply(ctx, ns, kube.run(args).stdout)
    typer.secho(f"  {name}: 로컬 secret apply ({len(data)} keys, 인너루프 — 아웃터는 ESO)", dim=True)


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
        mig_hash: str | None = None
        if name in overrides:
            mdir = overrides[name]
            if not no_build and _has_image(mdir):   # image 없으면 schema 모듈(G21) — build 생략
                tag = _image_tag(ws, mdir)
                kube.docker_build(tag, mdir, _build_secrets(ws, root))
                kube.kind_load(tag, ctx.removeprefix("kind-"))
            _chart_warnings(mdir / "module.yaml", chart, pyaml)
            sets = (f"namespace={ns}",)
            if _has_db(name, overrides, snaps):
                mig_hash = _migrations_hash(mdir)
                sets += (f"migrationWave={_migration_wave(name, _all_specs(overrides, snaps))}",
                         f"dbMigrationsHash={mig_hash}")
                _grant_warnings(name, overrides, snaps)
            manifests, src = _render(chart, mdir, "local", name, set_values=sets,
                                     platform_values=_platform_values(ws, root)), "local"
        else:
            sm = snaps[name]
            manifests, src = "\n---\n".join(p.read_text(encoding="utf-8") for p in sm.manifests), "snapshot"
        kube.ensure_namespace(ctx, ns)
        if name in overrides:
            _apply_local_secrets(name, overrides[name], ns, ctx)   # 인너루프 secret(G27) — 아웃터는 ESO
            cm = _migrations_cm(name, overrides[name], ns)
            if cm:
                kube.apply(ctx, ns, cm)
        kube.apply(ctx, ns, manifests)
        if mig_hash:  # 스테일 해시 Job prune — 내용 변경 시 옛 Job 제거(현 해시 Job 은 보존, G18②).
            kube.run(["kubectl", "--context", ctx, "-n", ns, "delete", "job",
                      "-l", f"bee.dev/module={name},bee.dev/mighash!={mig_hash}",
                      "--ignore-not-found"])
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
            manifests = _render(chart, overrides[name], "local", name, set_values=(f"namespace={ns}",),
                                platform_values=_platform_values(ws, root))
        else:
            manifests = "\n---\n".join(p.read_text(encoding="utf-8") for p in snaps[name].manifests)
        kube.delete(ctx, ns, manifests)
        _ok(f"{name} → delete (워크로드만 — ns·데이터 보존, 규칙 9)")


def substrate_up_impl(root: Path, ws: wsm.Workspace, *, remote_ok: bool = False) -> None:
    """substrate 적용(G26) — 인너루프 클러스터에 공유 인프라를 올린다.

    정적 매니페스트(core-infra/substrate/)는 kubectl apply, helm-패키지(Kong)는 helm
    upgrade --install(substrate.helm.yaml 좌표) 위임. **합성 0**(G12③ sharpen: 합성만
    금지 — 적용·검증은 허용. helm/kubectl 이 다 한다, bee 는 derive 0). 공유환경 substrate 는
    여기서 안 한다 — ArgoCD bee-substrate-<env> 독점(G7·G12③). 멱등(반복 안전)."""
    ctx = _cluster(ws, remote_ok)
    ci = wsm.core_infra_dir(ws, root)
    sub = ci / "substrate"
    if not sub.is_dir():
        _fail(f"substrate 디렉토리 없음 — {sub}")

    # 1. 정적 매니페스트(ns·postgres·nats·bitcoind). server-side apply(--force-conflicts) — 재적용 안전.
    #    (이전엔 nack/ CRD 가 client-side annotation 한도를 넘겨 server-side 필수였음 — NACK 은 G42 에서 제거.)
    typer.secho(f"substrate 정적 적용 → kubectl apply -R (ctx={ctx})", bold=True)
    out = kube.run(["kubectl", "--context", ctx, "apply", "--server-side", "--force-conflicts",
                    "-R", "-f", str(sub)]).stdout
    for ln in out.splitlines():
        typer.secho(f"  {ln}", dim=True)

    # 2. helm-패키지 substrate(Kong 등) — substrate.helm.yaml 좌표 위임. upgrade --install = 멱등.
    helm_decl = ci / "substrate.helm.yaml"
    releases = (_yaml_at(helm_decl).get("releases") or []) if helm_decl.exists() else []
    for rel in releases:
        name = rel.get("name")
        repon = rel.get("repoName") or name
        chart, ver, ns = rel.get("chart"), rel.get("version"), rel.get("namespace") or name
        typer.secho(f"substrate helm 적용 → {name} ({repon}/{chart}@{ver}, ns={ns})", bold=True)
        if rel.get("repo"):
            kube.run(["helm", "repo", "add", repon, rel["repo"]], check=False)   # 멱등
            kube.run(["helm", "repo", "update", repon], check=False)
        kube.run(["helm", "--kube-context", ctx, "upgrade", "--install", name,
                  f"{repon}/{chart}", "--version", str(ver), "-n", ns, "--create-namespace"])
        typer.secho(f"  {name} 적용 완료", dim=True)

    _ok("substrate 적용 완료 — `bee doctor` 로 점검")


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


def _module_repo_slug(mdir: Path) -> str | None:
    """모듈 git origin → 'owner/repo' (gh dispatch 대상). 없으면 None.
    https://github.com/owner/repo(.git) · git@github.com:owner/repo(.git) 둘 다."""
    import re as _re
    url = kube.git(["remote", "get-url", "origin"], mdir)
    if not url:
        return None
    m = _re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


# 모듈 thin caller(starter) 규약 — bee snap(원격 모드)이 dispatch 하는 publish 워크플로 파일명(G53).
PUBLISH_WORKFLOW = "publish-{env}.yaml"


def _digest_preflight(ws: wsm.Workspace, name: str, mdir: Path, digest: str) -> None:
    """phantom pin 방어(G53) — 핀할 digest 가 registry 에 *실재*하는지 best-effort 확인(local-path 모드).
    `docker manifest inspect {registry}/{image}@{digest}`(로컬 docker 자격증명 사용). 실패는 **경고만**(규칙 2 —
    하드 게이트는 CI gate1 의 crane 체크). 인증·네트워크 미비로도 실패할 수 있어 차단하지 않는다(false-negative 회피)."""
    if not digest:
        return
    registry, image = _image_ref(ws, mdir)
    if not registry or not image:
        return
    ref = f"{registry}/{image}@{digest}"
    if kube.run(["docker", "manifest", "inspect", ref], check=False).returncode != 0:
        _warn(f"{name}: digest 미확인 — `docker manifest inspect {ref}` 실패. registry 에 없으면 phantom pin"
              f"(→ ImagePullBackOff). 인증/네트워크 문제일 수도(경고만, 하드 게이트는 CI gate1).")


def snap_impl(env: str, targets: list[str] | None, root: Path, ws: wsm.Workspace, *,
              digest: str = "") -> None:
    """스냅샷 SoT 에 env 엔트리 쓰기 — **배포 아님**(배포=ArgoCD, `bee sync`). 검증은 CI 게이트1(규칙 2).

    **모드 자동 분기(G53 — 사용자는 bee 만, `gh` 직접 안 침)** = snapshot 바인딩 종류:
      · **원격(`snapshot.repo`=URL)** = GitOps → bee 가 모듈 publish 워크플로를 *dispatch*(`gh workflow run`).
        CI 가 render+gate1(+digest 존재 게이트)+push 의 **게이트된 쓰기**를 한다(규칙 2). bee=트리거.
      · **local-path(`snapshot.repo`=경로)** = 솔로 부트스트랩 → bee 가 직접 render+write+commit+push
        (GitOps/CI 없음 — 게이트는 부트스트랩 특성상 생략, digest 존재만 best-effort 프리플라이트).

    **핀 출처 = platform.envs 사다리 자동**: 맨 앞(dev)=`--digest` 빌드 핀 *생성* · 다음(prod)=앞 env
    provenance 의 imageDigest *복사*(전진/승격, 같은 아티팩트). env 별 독립 핀. 공유 env 전용(규칙 7).
    쓰기 게이트: dev=CI직접·prod=PR(G8). (구 publish + promote 통합 — G52; dispatch 흡수 — G53.)
    """
    if env == "local":
        _fail("snap 은 공유 env 전용 — 로컬은 SoT 밖(규칙 7). 인너루프는 bee up.")
    if wsm._is_url(ws.snapshot_repo):
        _snap_dispatch(env, targets, root, ws, digest=digest)
    else:
        _snap_local(env, targets, root, ws, digest=digest)


def _snap_dispatch(env: str, targets: list[str] | None, root: Path, ws: wsm.Workspace, *,
                   digest: str = "") -> None:
    """원격(GitOps) 모드 snap — bee 가 모듈 publish 워크플로 dispatch(G53). 사용자가 `gh workflow run`·
    snapshot 레포를 직접 안 건드린다 — bee 가 트리거하고, **게이트된 쓰기**(render+gate1+push)는 CI 가
    한다(규칙 2 — 검증은 CI). bee 는 *렌더 안 함*(원격 snapshot 캐시는 pin 된 읽기 전용, 규칙 8)."""
    import shutil

    if env != "dev":
        _fail(f"원격 snap 은 현재 dev 만(승격 사다리 맨 앞, 핀 생성) — prod dispatch 는 Tier 2(라이브 prod·app-prod, "
              f"PR 게이트 G8). 로컬 부트스트랩 승격은 snapshot.repo 를 path 로.")
    if not shutil.which("gh"):
        _fail("gh 없음 — 원격 snap 은 GitHub CLI 로 워크플로 dispatch(사용자는 bee 만, G53). "
              "`brew install gh` 후 `gh auth login`.")
    overrides = wsm.override_dirs(ws, root)
    names = targets or sorted(overrides)
    unknown = [n for n in names if n not in overrides]
    if unknown:
        _fail(f"snap 은 편집 표면(from-local) 전용(규칙 5): {', '.join(unknown)} (pull/new 로 등록)")
    if digest and len(names) != 1:
        _fail("--digest 는 모듈 1개와 함께만 (모듈별 digest 가 다르다)")
    wf = PUBLISH_WORKFLOW.format(env=env)
    for name in names:
        mdir = overrides[name]
        slug = _module_repo_slug(mdir)
        if not slug:
            _fail(f"{name}: git origin 없음 — dispatch 대상 레포 미상(repos/{name} 에 origin remote 필요, G3)")
        if _has_image(mdir) and not digest:
            _fail(f"{name}: image 모듈인데 --digest 없음 — 빌드는 로컬(G33). 먼저 "
                  f"`bee build -e {env} {name} --push` → digest → `bee snap -e {env} {name} --digest <D>`.")
        # CI 가 모듈 default 브랜치를 체크아웃한다(workflow_dispatch). 로컬 HEAD 가 origin 에 안 올라가 있으면
        # provenance.moduleCommit 이 어긋난다(정합 #2) — 차단 아닌 경고(push 는 사용자 git 책임).
        if kube.git(["rev-list", "@{u}..HEAD"], mdir):
            _warn(f"{name}: 로컬 커밋이 origin 에 미푸시 — CI 는 origin default 를 체크아웃(moduleCommit 어긋남). "
                  f"`git -C repos/{name} push` 후 dispatch 권장.")
        cmd = ["gh", "-R", slug, "workflow", "run", wf]
        if digest:
            cmd += ["-f", f"digest={digest}"]
        typer.secho(f"CI dispatch → {slug} {wf}"
                    + (f" (digest {digest[:23]}…)" if digest else " (image-less, G21)"), bold=True)
        kube.run(cmd)
        _ok(f"{name}: publish 워크플로 dispatch — CI 가 render+gate1+push(게이트된 쓰기, 규칙 2). "
            f"진행: gh -R {slug} run list")
    typer.secho(f"  배포는 CI push 완료 후 `bee sync {env}`(manual — 배포 게이트, G53).", dim=True)


def _snap_local(env: str, targets: list[str] | None, root: Path, ws: wsm.Workspace, *,
                digest: str = "") -> None:
    """local-path(솔로 부트스트랩) 모드 snap — bee 가 직접 render+write+commit+push(G53).
    GitOps/CI 없음(부트스트랩 특성 — 게이트1 생략). digest 존재는 best-effort 프리플라이트(phantom pin 경고).
    렌더 입력(module.yaml·values-<env>)은 로컬 편집표면(Tier 1; 순수형은 앞-env 스냅샷 직접 렌더 = refinement)."""
    overrides = wsm.override_dirs(ws, root)
    chart = _chart_source(ws, root)
    pyaml = wsm.platform_yaml_path(ws, root)
    products = _products(ws, root)
    snap_path = _snapshot_path(root, ws)
    env_dir = snap_path / "envs" / env
    snaps = snap_mod.load_snapshot(env_dir)
    # 사다리 — 앞 env = 핀 복사 출처(전진/승격), 맨 앞 = 빌드 핀(진입)
    ladder = list((_yaml_at(pyaml).get("spec") or {}).get("envs") or []) if pyaml else []
    prior = ladder[ladder.index(env) - 1] if (env in ladder and ladder.index(env) > 0) else None
    prior_snaps = snap_mod.load_snapshot(snap_path / "envs" / prior) if prior else {}
    names = targets or sorted(overrides)
    unknown = [n for n in names if n not in overrides]
    if unknown:
        _fail(f"snap 은 편집 표면(from-local) 전용(규칙 5): {', '.join(unknown)} (pull/new 로 등록)")
    if digest and len(names) != 1:
        _fail("--digest 는 모듈 1개와 함께만 (모듈별 digest 가 다르다)")
    for name in names:
        mdir = overrides[name]
        _chart_warnings(mdir / "module.yaml", chart, pyaml)
        spec = _yaml_at(mdir / "module.yaml").get("spec") or {}
        if not (mdir / f"values-{env}.yaml").exists():
            _fail(f"{name}: values-{env}.yaml 없음 — {env} 좌표 필요(규칙 3).")
        # 핀 출처 결정 (사다리)
        eff_digest = digest
        repo_url = kube.git(["remote", "get-url", "origin"], mdir) or str(mdir)
        commit = kube.git(["rev-parse", "HEAD"], mdir)
        chart_ver = str((spec.get("chart") or {}).get("version") or "")
        if prior and not digest:   # 전진(승격) — 앞 env 검증된 핀 복사
            if name not in prior_snaps:
                _fail(f"{name}: envs/{prior} 에 없음 — 전진 원천은 검증된 {prior} 모듈(먼저 snap -e {prior}).")
            pp = _yaml_at(prior_snaps[name].provenance) if prior_snaps[name].provenance else {}
            eff_digest = pp.get("imageDigest") or ""
            if chart_ver != str(pp.get("chartVersion") or ""):
                _warn(f"{name}: 로컬 chart {chart_ver} ≠ {prior} {pp.get('chartVersion')} — {prior} 핀 기록(렌더 입력=로컬, Tier 1)")
            commit = str(pp.get("moduleCommit") or commit)
            chart_ver = str(pp.get("chartVersion") or chart_ver)
            repo_url = pp.get("repoUrl") or repo_url
        elif prior and digest:
            _warn(f"{name}: -e {env} 에 --digest 직접 — {prior} 검증 우회(escape hatch)")
        _digest_preflight(ws, name, mdir, eff_digest)   # phantom pin 방어(G53, best-effort)
        ns = _namespace(name, _yaml_at(mdir / "module.yaml"), products)
        sets = (f"namespace={ns}",) + ((f"imageDigest={eff_digest}",) if eff_digest else ())
        if spec.get("db"):
            sets += (f"migrationWave={_migration_wave(name, _all_specs({name: mdir}, snaps))}",
                     f"dbMigrationsHash={_migrations_hash(mdir)}")
            _grant_warnings(name, {name: mdir}, snaps)
        manifests = _render(chart, mdir, env, name, set_values=sets,
                            platform_values=_platform_values(ws, root))
        cm = _migrations_cm(name, mdir, ns)
        if cm:
            manifests = manifests + "\n---\n" + cm
        prov = {
            "module": name, "repoUrl": repo_url, "moduleCommit": commit,
            "imageDigest": eff_digest, "chartVersion": chart_ver,
            "dependsOn": list(spec.get("dependsOn") or []),
        }
        db_dir = mdir / "db"
        contracts_dir = mdir / "contracts"   # 계약 표면(openapi/asyncapi) 운반 — 복사만(파생 0, bee contracts)
        snap_mod.write_entry(env_dir, name, mdir / "module.yaml", manifests, provenance=prov,
                             db_src=db_dir if db_dir.is_dir() else None,
                             contracts_src=contracts_dir if contracts_dir.is_dir() else None)
        mode = (f"전진({prior}→{env}) 핀 복사" if (prior and not digest)
                else "빌드 핀" if eff_digest else "image 없음(G21)")
        _ok(f"{name} → envs/{env}/{name}  ({mode}" + (f": {eff_digest[:23]}…)" if eff_digest else ")"))
    kube.run(["git", "-C", str(snap_path), "add", f"envs/{env}"])
    if not kube.git(["status", "--porcelain", "--", f"envs/{env}"], snap_path):
        typer.secho("  무변경 — 커밋 생략 (diff = 실질 변경, G8)", dim=True)
        return
    kube.run(["git", "-C", str(snap_path), "commit", "-q", "-m", f"snap({env}): {' '.join(names)}"])
    _ok(f"snapshot 커밋 {kube.git(['rev-parse', '--short', 'HEAD'], snap_path)} "
        f"({env} 쓰기 게이트 G8: dev=CI직접·prod=PR — 부트스트랩은 솔로라 직접)")
    # **CI(GitHub Actions)도 local-path 모드**(헤드리스 워크스페이스 snapshot=체크아웃 경로) — 거기선 bee 가
    # push 하면 **gate1 전에 푸시**(규칙 2 "게이트→푸시" 위반)가 된다. CI 에선 커밋만, push 는 워크플로가
    # gate1 *후* 한다. **솔로 부트스트랩(비-CI)**만 bee 가 직접 push(SoT 착지가 목적 — 게이트는 부트스트랩
    # 특성상 없음, --push 플래그 제거 G53). GITHUB_ACTIONS 로 구분(표준 CI 신호).
    if os.environ.get("GITHUB_ACTIONS"):
        typer.secho("  CI 환경 — push 생략(워크플로가 gate1 후 push, 규칙 2). 커밋만.", dim=True)
        return
    # 배포는 별도(snap ⊥ sync) — push 됐다고 적용 아님. manual sync 면 `bee sync` 가 배포 게이트.
    r = kube.run(["git", "-C", str(snap_path), "push", "-q"], check=False)
    if r.returncode == 0:
        _ok(f"snapshot push — 배포는 `bee sync {env}`(manual 게이트, G53) / ArgoCD 단일 경로(G5·G7)")
    else:
        _warn(f"snapshot push 실패(커밋은 됨) — 수동 `git -C {snap_path} push` 후 `bee sync {env}`.\n"
              f"  {(r.stderr or '').strip().splitlines()[0][:80] if (r.stderr or '').strip() else ''}")


def sync_impl(env: str, root: Path, ws: wsm.Workspace, *, context: str = "", prune: bool = True) -> None:
    """배포 반영(G52/G53) — ArgoCD Application `bee-<env>` 에 sync operation 설정(kubectl).
    bee 는 **직접 적용 안 함** — ArgoCD 에게 reconcile 요청하는 얇은 리모컨(G7 — 적용은 ArgoCD 단일 경로).
    **write(snap) ⊥ deploy(sync) 분리.** 컨텍스트 기본 = `kind-bee-<env>`(공유환경 클러스터, --context 로 override).

    **manual sync 포스처(G53)**: app 은 syncPolicy.automated 없음 — `bee sync` 가 *유일한 배포 트리거*
    (= 배포 게이트, snap⊥sync 가 실효). prune=true(기본) → 스냅샷에서 사라진 모듈을 클러스터에서 제거
    (SoT 일치 — automated.prune 이 하던 것을 명시 동사로). 위험하면 `--no-prune`.
    """
    if env == "local":
        _fail("sync 는 공유 env 전용 — 인너루프는 bee up(직접). 공유 배포=ArgoCD(G7).")
    ctx = context or f"kind-bee-{env}"
    app = f"bee-{env}"
    typer.secho(f"ArgoCD sync 요청 → app {app} (ctx={ctx}, prune={'on' if prune else 'off'}) "
                f"— 적용은 ArgoCD(bee 는 리모컨, G7 · manual 배포 게이트 G53)", bold=True)
    # .operation 설정 → ArgoCD application-controller 가 sync 실행. argocd CLI 의존 없이 kubectl 만.
    sync_op: dict = {"revision": "HEAD"}
    if prune:
        sync_op["prune"] = True   # SoT 에서 사라진 리소스 제거(automated.prune 대체 — 명시 갱신, 규칙 8)
    import json as _json
    patch = _json.dumps({"operation": {"initiatedBy": {"username": "bee"}, "sync": sync_op}})
    kube.run(["kubectl", "--context", ctx, "-n", "argocd", "patch", "application", app,
              "--type", "merge", "-p", patch])
    _ok(f"{app} sync 트리거 — 진행 확인: kubectl --context {ctx} -n argocd get app {app} (또는 ArgoCD UI)")


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


def doctor_impl(*, remote_ok: bool = False) -> int:
    """환경 진단(읽기 전용) — 도구·바인딩·클러스터 도달·substrate·pin 정합. **게이트 아님**(규칙 2):
    모듈 계약 검증은 CI·helm 몫. 여기는 '내 환경이 제대로 엮였나'의 프리플라이트.
    substrate 점검(G26 — G12③ sharpen: 합성만 금지, 검증·적용은 허용): 존재·도달만 read-only
    확인(적용은 `bee substrate up`, 합성은 안 함). 반환 = ✗(fail) 개수."""
    import shutil

    tally = {"ok": 0, "warn": 0, "fail": 0}

    def rep(kind: str, msg: str) -> None:
        sym, col = {"ok": ("✓", OK), "warn": ("⚠", WARN), "fail": ("✗", ERR)}[kind]
        typer.secho(f"  {sym} {msg}", fg=col)
        tally[kind] += 1

    typer.secho("bee doctor — 환경 진단 (읽기 전용, 게이트 아님)\n", bold=True)

    # 1. 도구
    typer.secho("도구", bold=True)
    for tool, args, required in [
        ("helm", ["version", "--short"], True),
        ("kubectl", ["version", "--client"], True),
        ("docker", ["--version"], True),
        ("git", ["--version"], True),
        ("uv", ["--version"], False),
        ("gh", ["--version"], False),   # 원격 snap dispatch(G53) — 없으면 local-path 부트스트랩만
    ]:
        if not shutil.which(tool):
            rep("fail" if required else "warn", f"{tool} 없음{'' if required else ' (선택)'}")
            continue
        out = kube.run([tool, *args], check=False).stdout.strip().splitlines()
        rep("ok", f"{tool} {out[0][:46] if out else ''}".strip())

    # 2. 워크스페이스 바인딩
    typer.secho("\n워크스페이스", bold=True)
    try:
        root = wsm.find_root(Path.cwd())
    except Exception as e:
        rep("fail", f"bee.workspace.yaml 못 찾음 — {e}")
        return _doctor_summary(tally)
    try:
        ws = wsm.load_workspace(root)
        rep("ok", f"bee.workspace.yaml — env={ws.env}")
    except Exception as e:
        rep("fail", f"워크스페이스 파싱 실패 — {e}")
        return _doctor_summary(tally)

    try:
        ci = wsm.core_infra_dir(ws, root)
        if ws.chart_ref:
            rep("ok", f"coreInfra: {ci} · chart=OCI {ws.chart_ref}")
        else:
            cv = _yaml_at(wsm.chart_dir(ws, root) / "Chart.yaml").get("version", "?")
            rep("ok", f"coreInfra: {ci} · chart {cv} (path)")
    except Exception as e:
        rep("fail", f"coreInfra/chart — {e}")

    try:
        p = wsm.platform_yaml_path(ws, root)
        if p:
            pdoc = _yaml_at(p)
            pname = (pdoc.get("metadata") or {}).get("name", "?")   # G43: 이름은 디스크립터 metadata.name
            sup = ((pdoc.get("spec") or {}).get("chart") or {}).get("supported", "?")
            rep("ok", f"platform.yaml: {pname} (supported {sup})")
        else:
            rep("warn", "platform.yaml 없음(core-infra 루트) — namespace 룩업·지원 범위 검사 생략")
    except Exception as e:
        rep("fail", f"platform.yaml — {e}")

    try:
        sdir = wsm.resolve_snapshot_env_dir(ws, root)
        snaps = snap_mod.load_snapshot(sdir)
        rep("ok", f"snapshot: {sdir} ({len(snaps)} 모듈 backdrop)")
    except Exception as e:
        rep("fail", f"snapshot — {e}")

    # 이미지 registry(G54 — env 불변 좌표, build push·render 공용). 없으면 image 모듈 build/render 막힘.
    if ws.image_registry:
        rep("ok", f"imageRegistry: {ws.image_registry} (G54 — env 불변)")
    else:
        rep("warn", "imageRegistry 없음 — image 모듈 build --push·render 막힘. "
                    "bee.workspace.yaml 에 `imageRegistry: <registry>`(G54)")

    # ref 스코프(G50) — 브랜치 ref 활성 시 *항상 보이게*(가시성 = "적용 전에 본다"). 인너루프 전용.
    bs = _branch_scope(ws)
    if bs:
        rep("warn", "ref 스코프 활성(인너루프 미병합 통합, G50): "
                    + " · ".join(f"{l}@{r}" for l, r in bs)
                    + " — 공유 ArgoCD 는 main 고정(규칙 7). `bee ref` 로 상세, `bee ref --reset` 로 복귀")
    else:
        rep("ok", "ref 스코프: main baseline (미병합 통합 없음, G50)")

    overrides: dict = {}
    try:
        overrides = wsm.override_dirs(ws, root)
        if not overrides:
            rep("warn", "편집 표면 비어있음 — local 에 모듈 등록(소스=멤버십, 규칙 5)")
        for n, d in sorted(overrides.items()):
            rep("ok", f"local: {n} → {d}")
    except Exception as e:
        rep("fail", f"local override — {e}")

    # 3. 클러스터 (인너루프 직접 적용 대상)
    typer.secho("\n클러스터", bold=True)
    ctx = ws.cluster_context
    if not ctx:
        rep("fail", "cluster.context 없음 — bee.workspace.yaml")
    else:
        kind = ctx.startswith("kind-")
        rep("ok" if kind else "warn",
            f"cluster.context: {ctx} ({'kind' if kind else '비-kind — up/down 시 --remote-ok 필요(G7)'})")
        r = kube.run(["kubectl", "--context", ctx, "get", "nodes", "--no-headers"], check=False)
        if r.returncode == 0:
            ready = sum(1 for ln in r.stdout.splitlines() if " Ready" in ln)
            total = len([ln for ln in r.stdout.splitlines() if ln.strip()])
            rep("ok" if ready == total and total else "warn", f"도달 가능 ({ready}/{total} 노드 Ready)")
        else:
            rep("fail", f"도달 불가 — {r.stderr.strip().splitlines()[0][:60] if r.stderr.strip() else 'context 없음?'}")

    # 4. substrate (G26 — read-only 점검만; 적용=bee substrate up, 합성은 안 함 G12③ sharpen)
    typer.secho("\nsubstrate (인너루프 — read-only; 적용은 `bee substrate up`)", bold=True)
    if not ctx:
        rep("warn", "cluster.context 없음 — substrate 점검 생략")
    else:
        def _chk(args: list[str]) -> bool:
            return kube.run(["kubectl", "--context", ctx, *args], check=False).returncode == 0
        # postgres·nats = bee-substrate ns Deployment / Kong = ingressclass.
        # NATS 는 접속형 substrate(G42): JetStream(`-js`)만 켜져 있으면 충분 — 발행 앱이
        # JetStream API 로 stream 을 직접 소유한다. NACK(CR 브리지) 점검은 G42 에서 제거.
        for ns, dep, label in [("bee-substrate", "postgres", "postgres"), ("bee-substrate", "nats", "NATS")]:
            r = kube.run(["kubectl", "--context", ctx, "-n", ns, "get", "deploy", dep,
                          "-o", "jsonpath={.status.readyReplicas}"], check=False)
            if r.returncode != 0:
                rep("warn", f"{label} 없음(ns {ns}/{dep}) — `bee substrate up` 필요")
            elif (r.stdout.strip() or "0") != "0":
                rep("ok", f"{label} Ready (ns {ns})")
            else:
                rep("warn", f"{label} Not Ready (ns {ns})")
        kong = _chk(["get", "ingressclass", "kong"])
        rep("ok" if kong else "warn",
            "Kong ingressclass" if kong
            else "Kong 없음 — routing 어휘(Ingress) 적용 불가, `bee substrate up` 필요")

    # 4.5 capability/resource 정합 (uses/provides — G36 capability-종류 · G37 리소스-프로파일):
    #     모듈 used(어휘 파생) ⊆ substrate.provides · 모듈 uses(compute/storage) ⊆ resources 프로파일.
    #     선언+검증(프로비저닝 아님 — provider=substrate up·volume=StorageClass, G26). 멀티-provider 코덱은 G28 연기.
    if overrides:
        typer.secho("\ncapability/resource (uses/provides — G36·G37, 선언+검증)", bold=True)
        pspec: dict = {}
        try:
            pp = wsm.platform_yaml_path(ws, root)
            pspec = (_yaml_at(pp).get("spec") or {}) if pp else {}
        except Exception:
            pspec = {}
        provides = (pspec.get("substrate") or {}).get("provides") or {}
        rprof = pspec.get("resources") or {}
        if not provides and not rprof:
            rep("warn", "platform.yaml substrate.provides·resources 미선언 — 검증 생략(G36/G37)")
        else:
            if provides:
                rep("ok", f"platform provides: {', '.join(sorted(provides))}")
            if rprof:
                rep("ok", f"platform resources: compute={{{','.join(sorted(rprof.get('compute') or {}))}}}"
                          f" storage={{{','.join(sorted(rprof.get('storage') or {}))}}}")
            for n, d in sorted(overrides.items()):
                spec = _yaml_at(d / "module.yaml").get("spec") or {}
                uses = _module_uses(spec)
                cmiss = uses - set(provides)
                if cmiss:
                    rep("warn", f"{n}: uses {{{', '.join(sorted(cmiss))}}} ⊄ provides — 플랫폼 미제공(substrate)")
                elif uses:
                    rep("ok", f"{n}: capability {{{', '.join(sorted(uses))}}} ⊆ provides")
                # G44 db 코덱 — db.target ∈ provides.db[].target (multi-target dispatch · 기본값 없음)
                dbspec = spec.get("db") or {}
                if dbspec:
                    dt = dbspec.get("target")
                    dtargets = {e.get("target") for e in (provides.get("db") or []) if isinstance(e, dict)}
                    if not dt:
                        rep("warn", f"{n}: db.target 미선언 — psql|mysql 명시 필수(G44, 기본값 없음)")
                    elif dt not in dtargets:
                        rep("warn", f"{n}: db.target {dt!r} ∉ provides.db {{{', '.join(sorted(t for t in dtargets if t))}}}")
                    else:
                        rep("ok", f"{n}: db.target {dt} ⊆ provides.db")
                # G37 리소스 프로파일
                u = spec.get("uses") or {}
                rmiss, have = [], []
                if u.get("compute"):
                    have.append(f"compute:{u['compute']}")
                    if u["compute"] not in (rprof.get("compute") or {}):
                        rmiss.append(f"compute:{u['compute']}")
                sp = (u.get("storage") or {}).get("profile")
                if sp:
                    have.append(f"storage:{sp}")
                    if sp not in (rprof.get("storage") or {}):
                        rmiss.append(f"storage:{sp}")
                if rmiss:
                    rep("warn", f"{n}: 리소스 프로파일 {{{', '.join(rmiss)}}} 미정의 — platform.resources 확인")
                elif have:
                    rep("ok", f"{n}: resource {{{', '.join(have)}}} ⊆ profiles")

    # 5. build registry (G30/G31 — 도달 + 토큰(bee.secrets.local.yaml 또는 env) 점검. read-only)
    if ws.build_registries:
        wsecrets = wsm.load_workspace_secrets(root)
        typer.secho("\nbuild registry (G30 — 빌드 사설 registry)", bold=True)
        for r in ws.build_registries:
            name, idx, tenv = r.get("name", "?"), r.get("index", ""), r.get("tokenEnv", "")
            url = idx.replace("sparse+", "")
            rc = kube.run(["curl", "-s", "-m", "6", "-o", "/dev/null", "-w", "%{http_code}", url],
                          check=False).stdout.strip() if url else ""
            reachable = bool(rc) and rc != "000"
            rep("ok" if reachable else "warn",
                f"{name}: 도달 {url or '(index 없음)'} (http {rc or '실패'})")
            src = ("bee.secrets.local" if tenv and wsecrets.get(tenv)
                   else "env" if tenv and os.environ.get(tenv) else None)
            rep("ok" if src else "warn",
                f"{name}: 토큰 ${tenv or '?'} " + (f"설정됨({src})" if src else "미설정 — bee.secrets.local.yaml 또는 env"))

    # 6. pin 정합 (G6 — 경고만, 차단은 CI)
    typer.secho("\npin 정합 (G6 — 경고만, 차단은 CI)", bold=True)
    pyaml = None
    try:
        pyaml = wsm.platform_yaml_path(ws, root)
    except Exception:
        pass
    sup = ((_yaml_at(pyaml).get("spec") or {}).get("chart") or {}).get("supported") if pyaml else None
    if not overrides:
        rep("warn", "편집 표면 없음 — pin 점검 생략")
    for n, d in sorted(overrides.items()):
        pin = ((_yaml_at(d / "module.yaml").get("spec") or {}).get("chart") or {}).get("version")
        if not pin:
            rep("warn", f"{n}: chart pin 없음(module.yaml spec.chart.version)")
            continue
        if sup:
            try:
                from packaging.specifiers import SpecifierSet
                from packaging.version import Version

                spec = SpecifierSet(sup if "," in sup else sup.replace(" ", ","))
                if Version(str(pin)) not in spec:
                    rep("warn", f"{n}: chart pin {pin} ∉ supported {sup}")
                    continue
            except Exception:
                pass
        rep("ok", f"{n}: chart pin {pin}" + (f" ∈ {sup}" if sup else ""))

    return _doctor_summary(tally)


def _doctor_summary(tally: dict) -> int:
    typer.secho(f"\n요약: ✓ {tally['ok']} · ⚠ {tally['warn']} · ✗ {tally['fail']}", bold=True)
    if tally["fail"]:
        typer.secho("환경에 막힌 곳이 있다 — 위 ✗ 를 먼저 풀어라.", fg=ERR)
    elif tally["warn"]:
        typer.secho("동작엔 지장 없으나 확인 권장(⚠).", fg=WARN)
    else:
        typer.secho("환경 정상 — bee up 준비됨.", fg=OK)
    return tally["fail"]


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
    sys.stdout.write(_render(chart, mdir, env, module, set_values=(f"namespace={ns}",),
                             platform_values=_platform_values(ws, root)))


@app.command()
def build(
    modules: list[str] = typer.Argument(None, help="기본: 편집 표면 전체"),
    push: bool = typer.Option(False, "--push", help="imageRegistry 로 빌드+푸시 → digest(아웃터 이미지 준비, C). 기본=kind-load(인너)"),
):
    """from-local 이미지 빌드. 기본 = docker build + kind load(인너루프). --push = 워크스페이스
    imageRegistry(G54 — env 불변)로 푸시 + digest 출력(아웃터 — CI 는 그 digest 로 publish 만).
    **env 인자 없음(G54)**: registry 가 워크스페이스(env 불변)라 build 는 env 무관 — 빌드 1회 → digest 가
    사다리 전체 복사(G52). digest → `bee snap -e dev --digest`(엔트리)."""
    root, ws = load_ctx()
    names = list(modules) if modules else sorted(wsm.override_dirs(ws, root))
    if push:
        build_push_impl(names, root, ws)
    else:
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
def snap(
    modules: list[str] = typer.Argument(None, help="기본: 편집 표면 전체"),
    env: str = typer.Option(..., "-e", "--env", help="공유 env (dev/prod — G51). local 금지(규칙 7)"),
    digest: str = typer.Option("", "--digest", help="이미지 digest (dev=빌드 핀. prod 면 우회 — 보통 생략)"),
):
    """스냅샷 SoT 에 env 엔트리 쓰기(G52/G53) — 배포 아님(배포=`bee sync`). 검증은 CI 게이트1(규칙 2).
    **모드 자동(G53)**: snapshot=URL → bee 가 CI publish 워크플로 dispatch(사용자는 gh 안 침) ·
    snapshot=path → bee 가 직접 write+commit+push(솔로 부트스트랩). 핀 출처 = 사다리 자동: dev(맨앞)=
    --digest 빌드 핀(생성) · prod=앞 env 핀 복사(전진). (구 publish+promote+dispatch 통합 — 동사 하나.)"""
    root, ws = load_ctx()
    snap_impl(env, list(modules) if modules else None, root, ws, digest=digest)


@app.command()
def sync(
    env: str = typer.Argument(..., help="공유 env (dev/prod) — ArgoCD app bee-<env> sync"),
    context: str = typer.Option("", "--context", help="kubectl 컨텍스트 (기본 kind-bee-<env>)"),
    prune: bool = typer.Option(True, "--prune/--no-prune", help="SoT 에서 사라진 모듈 제거(기본 on, G53 manual)"),
):
    """배포 반영(G52/G53) — ArgoCD 에 sync 요청(write ⊥ deploy 분리). bee 는 직접 적용 안 함, ArgoCD 리모컨(G7).
    snap 이 스냅샷에 쓰면, sync 가 ArgoCD 에게 그 env 를 클러스터에 반영하라고 요청한다(manual = 배포 게이트)."""
    root, ws = load_ctx()
    sync_impl(env, root, ws, context=context, prune=prune)


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


@app.command()
def ref(
    ref: str = typer.Argument(None, help="브랜치/SHA — 미지정=현재 ref 스코프 상태 표시"),
    snapshot: bool = typer.Option(False, "-s", "--snapshot", help="snapshot.ref 대상"),
    core_infra: bool = typer.Option(False, "-c", "--core-infra", help="coreInfra.ref(substrate/chart) 대상"),
    reset: bool = typer.Option(False, "--reset", help="snapshot·coreInfra 둘 다 main 복귀(원턴, ephemeral G50)"),
):
    """ref 스코프(G50) — **인너루프 미병합 통합**: substrate/snapshot 을 main 아닌 브랜치 ref 에서 소비
    (병합 전 남의 substrate/모듈 변경에 내 모듈을 통합·확인). **인너루프 전용**(공유 ArgoCD=main 고정, 규칙 7) ·
    재현=lock commit(규칙 8) · ephemeral(--reset). URL 소비 모드 전용(경로는 git checkout).

    `bee ref` 상태 · `bee ref <br> -s`/`-c` 설정 · `bee ref --reset` 복귀."""
    root, ws = load_ctx()
    ref_impl(ref, root, ws, snapshot=snapshot, core_infra=core_infra, reset=reset)


@app.command()
def contracts(
    module: str = typer.Argument(..., help="모듈 — 스냅샷(backdrop)에 운반된 계약 표면 노출"),
    kind: str = typer.Option(None, "--kind", help="openapi|asyncapi — 지정 시 내용 출력(cat), 미지정=목록"),
):
    """모듈 계약 표면(openapi/asyncapi) **read-only 노출** — 스냅샷에 운반·보관된 계약(파생·검증 0).

    bee = dumb pipe: 계약을 *보여줄* 뿐 *맞다고* 안 한다 — 정합·신선도는 사람(또는 별도 lint).
    어휘 0(파일 존재로 표현, module.yaml 무관) · 출처 = 모듈 `contracts/`(starter 스캐폴드, publish 가 운반).
    원격 소비(G35)면 `.bee/cache` 스냅샷에서 읽는다. **G42 events 걷어내기의 양성 주인** — events
    계약은 thin 어휘가 아니라 표준 asyncapi 로, bee 는 운반·노출만.
    """
    root, ws = load_ctx()
    env_dir = wsm.resolve_snapshot_env_dir(ws, root)
    cdir = env_dir / module / "contracts"
    if not cdir.is_dir():
        _fail(f"{module}: 계약 없음 — 스냅샷 envs/{ws.env}/{module}/contracts/ 부재. "
              f"통신 프로토콜이 있으면 모듈에 contracts/ 작성(starter 템플릿 참조).")
    files = sorted(p for p in cdir.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml"))
    if kind:
        target = next((p for p in files if p.stem == kind), None)
        if not target:
            avail = ", ".join(p.stem for p in files) or "(없음)"
            _fail(f"{module}: contracts/{kind}.yaml 없음 — 있는 계약: {avail}")
        sys.stdout.write(target.read_text(encoding="utf-8"))
        return
    typer.secho(f"{module} 계약 표면 (envs/{ws.env}, read-only — 운반·노출만):", bold=True)
    if not files:
        typer.secho("  (contracts/ 비어 있음)", dim=True)
    for p in files:
        typer.secho(f"  ✓ {p.stem}", fg=OK)
        typer.secho(f"      {p}", dim=True)
    typer.secho(f"  내용: bee contracts {module} --kind <{'|'.join(p.stem for p in files) or 'openapi|asyncapi'}>", dim=True)


@app.command()
def doctor(
    remote_ok: bool = typer.Option(False, "--remote-ok", help="비-kind 컨텍스트 허용(G7)"),
):
    """환경 진단 — 도구·바인딩·클러스터 도달·pin 정합(읽기 전용). 게이트 아님(규칙 2):
    모듈 계약 검증은 CI·helm 몫. ✗ 가 있으면 종료코드 1."""
    fails = doctor_impl(remote_ok=remote_ok)
    raise typer.Exit(1 if fails else 0)


@substrate_app.command("up")
def substrate_up(
    remote_ok: bool = typer.Option(False, "--remote-ok", help="비-kind 컨텍스트 허용(G7)"),
):
    """substrate 올림(G26) — core-infra/substrate(정적, kubectl) + substrate.helm.yaml(Kong, helm 위임).
    인너루프 전용 — 공유환경은 ArgoCD bee-substrate-<env>(G7·G12③). 합성 0(적용만), 멱등."""
    root, ws = load_ctx()
    substrate_up_impl(root, ws, remote_ok=remote_ok)


# bee 자체 업그레이드 대상 레포(G10 데모 미러 — 실 제품은 설정화 후속).
BEE_CLI_REPO = "https://github.com/jbyee-test-org/bee-cli"


@app.command()
def upgrade(
    ref: str = typer.Argument("main", help="git ref(tag/branch) — 기본 main(최신). 예: v0.5.2"),
):
    """전역 bee 자체 업그레이드(G30) — `uv tool install --force git+<repo>@<ref>`. 릴리스 후 즉시 갱신.
    (uv 필요. 전역 설치본을 교체 — 개발용 `uv run` 와 무관. 워크스페이스 밖에서도 동작.)"""
    import shutil
    if not shutil.which("uv"):
        _fail("uv 없음 — 전역 설치는 uv tool 사용. https://docs.astral.sh/uv")
    spec = f"git+{BEE_CLI_REPO}@{ref}"
    typer.secho(f"bee 업그레이드 → uv tool install --force {spec}", bold=True)
    kube.run(["uv", "tool", "install", "--force", spec])
    _ok(f"전역 bee 갱신됨(@{ref}) — `bee --version` 으로 확인")


# 워크스페이스 스캐폴드 템플릿(G31). 채울 곳은 CHANGEME · 빈 컨테이너로 둔다(주석으로 유도).
_WORKSPACE_TEMPLATE = """\
# bee 워크스페이스 — 편집 표면(local) + baseline(snapshot) + 바인딩(coreInfra·cluster). 규칙 5.
# cwd 상위 탐색으로 발견된다. 시크릿은 여기 두지 않는다 → bee.secrets.local.yaml.
#
# **소비 아티팩트(snapshot·core-infra)는 원격에서 읽는다** — work tree 에 두지 않고 .bee/cache 로
# pin 해 fetch(chart→OCI G6 모델을 나머지로 확장). 편집 대상(내 모듈)만 repos/ 에 로컬 보유.
# 플랫폼 메인테이너(core-infra 직접 편집)면 repo/ref → path: repos/core-infra 로 바꾼다.
version: 1
snapshot:  { repo: "https://github.com/CHANGEME-ORG/bee-snapshot.git", env: dev, ref: main }
coreInfra: { repo: "https://github.com/CHANGEME-ORG/bee-core-infra.git", ref: main, chartRef: "oci://ghcr.io/CHANGEME-ORG/charts/bee-module" }   # core-infra = 1 플랫폼(G43); 이름은 platform.yaml metadata.name
cluster: { context: kind-bee-local }                       # 인너루프 kubectl 컨텍스트(G7 — 공유환경 금지)
# 이미지 registry(G54) — build push 타깃 + render 이미지 ref. **env 불변**(G52 — 빌드 1회 digest 가 사다리 복사).
imageRegistry: "CHANGEME-REGISTRY"   # 예: ghcr.io/<org>/<product> (module image name 이 뒤에 붙는다)
# 빌드 사설 registry(G30 — cargo/npm 등 *빌드 의존*, 이미지 registry 와 별개) — 토큰은 bee.secrets.local.yaml[tokenEnv].
buildRegistries: []
#  - { name: kellnr, index: "sparse+http://HOST:PORT/api/v1/crates/", tokenEnv: CARGO_REGISTRIES_KELLNR_TOKEN }
local: {}   # <module>: { path: repos/<module> } — `bee pull <module>` 가 채운다(편집 표면, 규칙 5)
"""

_SECRETS_TEMPLATE = """\
# 워크스페이스-스코프 시크릿(G31) — 빌드 토큰 등. {ENV_VAR: value} 맵.
# **gitignore — 커밋 금지.** 모듈 시크릿은 모듈별 secrets.local.yaml(별개 — 중앙화 안 함).
# buildRegistries[].tokenEnv 가 여기서 해석된다. 예:
# CARGO_REGISTRIES_KELLNR_TOKEN: "<token>"
"""


@app.command()
def init():
    """워크스페이스 스캐폴드(G31) — bee.workspace.yaml + bee.secrets.local.yaml(gitignore 등록).
    맨손 온보딩(G35): init → URL·platform 채움 → `bee doctor`(원격 소비물 fetch·검증) →
    `bee substrate up` → `bee pull <module>` → `bee up`. core-infra·snapshot 은 원격에서 읽는다(클론 불요)."""
    cwd = Path.cwd()
    for fname, tmpl, hint in [
        (wsm.WORKSPACE_FILE, _WORKSPACE_TEMPLATE, "core-infra/snapshot URL·local 등록을 채워라"),
        (wsm.SECRETS_FILE, _SECRETS_TEMPLATE, "빌드 토큰 등을 채워라(커밋 금지)"),
    ]:
        f = cwd / fname
        if f.exists():
            _warn(f"{fname} 이미 존재 — 건너뜀")
        else:
            f.write_text(tmpl, encoding="utf-8")
            _ok(f"{fname} 생성 — {hint}")
    # .gitignore 에 시크릿 파일 + .bee/(lock·원격 소비 캐시) + repos/(편집 모듈, 각자 독립 git) 등록.
    gi = cwd / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    added = [e for e in (wsm.SECRETS_FILE, ".bee/", "repos/") if e not in lines]
    if added:
        gi.write_text("\n".join([*lines, *added]) + "\n", encoding="utf-8")
        _ok(f".gitignore 등록: {', '.join(added)}")
    else:
        _ok(".gitignore 항목 이미 등록됨(시크릿·캐시·모듈)")


if __name__ == "__main__":
    app()
