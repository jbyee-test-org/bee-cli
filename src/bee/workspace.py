"""워크스페이스 모델 — bee.workspace.yaml + lock. (POC thin-core 캐리, G5·G7·G9 적응)

워크스페이스 = "어떤 모듈을 local override 로 둘지"만 선언한다. 여기 없는 모듈은
모두 snapshot(baseline)에서 온다 — **소스 = 멤버십**(규칙 5). 공유환경 CD 엔 이 선택지가
없다(snapshot-only).

    bee.workspace.yaml
      version: 1
      snapshot: { repo: <경로|URL>, env: dev, ref: main }   # backdrop baseline (규칙 7 — 경계 dev)
      coreInfra: { path: <경로|URL>, platform: bitcert }     # chart·starter·platform.yaml 1-바인딩 (G5)
      cluster: { context: kind-bee-local }                   # 인너루프 클러스터 (G7 — 공유환경 금지)
      local:
        hello: { path: repos/hello }
        other: { repo: <URL>, ref: main }                    # path 없으면 CLI 가 clone (후속)

    .bee/workspace.lock.yaml                                 # up 이 pin (규칙 8)
      snapshot: { repo, env, ref, commit }
      local: { <name>: { path, repo, commit, dirty } }

POC 대비 적응: 개명(ann→bee, G4) · platform/descriptor 2-노브 → coreInfra 1-바인딩(G5).
snapshot.env(backdrop 기준, 기본 dev)와 렌더 env(`render -e`, 기본 local)는 **별개 노브**다.
$BEE_CORE_INFRA / $BEE_SNAPSHOT 환경변수가 있으면 미지정 시 기본값으로 쓴다.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WORKSPACE_FILE = "bee.workspace.yaml"
SECRETS_FILE = "bee.secrets.local.yaml"   # 워크스페이스-스코프 시크릿(빌드 토큰 등). gitignore — 커밋 금지(G31).
LOCK_FILE = ".bee/workspace.lock.yaml"
PLATFORMS_DIR = "platforms"  # core-infra 안의 디스크립터 홈: <core-infra>/platforms/<name>/platform.yaml
CHART_DIR = "chart"          # core-infra 안의 공용 차트


class WorkspaceError(RuntimeError):
    """워크스페이스 로드/구성 오류."""


@dataclass
class LocalOverride:
    name: str
    path: Path | None = None
    repo: str | None = None
    ref: str | None = None


@dataclass
class Workspace:
    env: str = "dev"                     # snapshot(backdrop) env — 렌더 env 와 별개
    snapshot_repo: str | None = None     # 로컬 경로(메인테이너) 또는 git URL(소비 — 원격 fetch)
    snapshot_ref: str = "main"
    core_infra: str | None = None        # 로컬 경로(메인테이너 — 직접 편집) (G5 — 1-바인딩)
    core_infra_repo: str | None = None   # git URL(소비 — 원격 fetch, work tree 미보유). path 와 양자택일
    core_infra_ref: str = "main"         # core_infra_repo 의 ref(브랜치=floating·40-hex=pin, 규칙 8)
    platform: str | None = None          # platforms/<name>/platform.yaml
    chart_ref: str | None = None         # oci://… — 있으면 OCI 소비(G6), 모듈 pin 이 --version
    cluster_context: str | None = None   # 인너루프 클러스터 kubectl 컨텍스트 (G7)
    locals: dict[str, LocalOverride] = field(default_factory=dict)
    # 빌드 사설 registry(G30) — [{name, index, tokenEnv}]. bee build 가 token 을 BuildKit secret
    # (id=<name>_token, env=tokenEnv)으로 주입 · doctor 가 도달+tokenEnv 점검. 빌드는 bee 경계 밖이나
    # bee build 가 토큰을 *전달*(소유 아님 — secret mount)하므로 제네릭하게 선언받는다.
    build_registries: list[dict] = field(default_factory=list)

    # ── 직렬화 ────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        local_out: dict[str, dict] = {}
        for name, ov in self.locals.items():
            entry: dict = {}
            if ov.path is not None:
                entry["path"] = str(ov.path)
            if ov.repo:
                entry["repo"] = ov.repo
            if ov.ref:
                entry["ref"] = ov.ref
            local_out[name] = entry
        doc: dict = {"version": 1, "snapshot": {"env": self.env, "ref": self.snapshot_ref}}
        if self.snapshot_repo:
            doc["snapshot"]["repo"] = self.snapshot_repo
        if self.core_infra or self.core_infra_repo or self.platform:
            ci: dict = {}
            if self.core_infra_repo:
                ci["repo"] = self.core_infra_repo
                ci["ref"] = self.core_infra_ref
            elif self.core_infra:
                ci["path"] = self.core_infra
            if self.platform:
                ci["platform"] = self.platform
            if self.chart_ref:
                ci["chartRef"] = self.chart_ref
            doc["coreInfra"] = ci
        if self.cluster_context:
            doc["cluster"] = {"context": self.cluster_context}
        if self.build_registries:
            doc["buildRegistries"] = self.build_registries
        doc["local"] = local_out
        return doc

    @classmethod
    def from_dict(cls, data: dict) -> "Workspace":
        snap = data.get("snapshot") or {}
        ci = data.get("coreInfra") or {}
        locals_: dict[str, LocalOverride] = {}
        for name, raw in (data.get("local") or {}).items():
            raw = raw or {}
            locals_[name] = LocalOverride(
                name=name,
                path=Path(raw["path"]) if raw.get("path") else None,
                repo=raw.get("repo"),
                ref=raw.get("ref"),
            )
        return cls(
            env=snap.get("env", "dev"),
            snapshot_repo=snap.get("repo"),
            snapshot_ref=snap.get("ref", "main"),
            core_infra=ci.get("path"),
            core_infra_repo=ci.get("repo"),
            core_infra_ref=ci.get("ref", "main"),
            platform=ci.get("platform"),
            chart_ref=ci.get("chartRef"),
            cluster_context=(data.get("cluster") or {}).get("context"),
            locals=locals_,
            build_registries=list(data.get("buildRegistries") or []),
        )


def find_root(start: Path) -> Path:
    """cwd 부터 상위로 bee.workspace.yaml 탐색 → 워크스페이스 루트."""
    for p in [start, *start.parents]:
        if (p / WORKSPACE_FILE).exists():
            return p
    raise WorkspaceError(f"{WORKSPACE_FILE} 없음 — 워크스페이스 안에서 실행하라 (cwd: {start})")


def load_workspace(root: Path) -> Workspace:
    f = root / WORKSPACE_FILE
    if not f.exists():
        raise WorkspaceError(f"{WORKSPACE_FILE} 없음: {root}")
    return Workspace.from_dict(yaml.safe_load(f.read_text(encoding="utf-8")) or {})


def load_workspace_secrets(root: Path) -> dict:
    """워크스페이스-스코프 시크릿(G31) — `bee.secrets.local.yaml`({ENV_VAR: value} 맵, gitignore).
    빌드 토큰 등 워크스페이스 레벨 자격증명. 없으면 {}(빌드는 실제 env 로 폴백). **모듈** 시크릿은
    모듈별 `secrets.local.yaml`(별개 — 중앙화 안 함)."""
    f = root / SECRETS_FILE
    if not f.exists():
        return {}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


def _merge_into(dst: dict, src: dict) -> None:
    """src 값을 dst 에 머지 — dst(round-trip 로드)의 주석·순서·flow 스타일 보존.
    dst 에만 있는 키는 제거(예: local 등록 해제). 매핑은 재귀, 그 외는 치환."""
    for k in [k for k in dst if k not in src]:
        del dst[k]
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_into(dst[k], v)
        else:
            dst[k] = v


def save_workspace(root: Path, ws: Workspace) -> Path:
    """기존 파일을 ruamel round-trip 으로 읽어 변경분만 머지 — 사용자 주석 보존(미결 6/G16).
    pull·new 의 변경은 사실상 local: 항목 추가뿐이므로 그 외 라인의 주석은 그대로 남는다."""
    from io import StringIO

    from ruamel.yaml import YAML

    f = root / WORKSPACE_FILE
    new = ws.to_dict()
    rt = YAML()
    rt.indent(mapping=2, sequence=4, offset=2)
    if f.exists():
        doc = rt.load(f.read_text(encoding="utf-8")) or {}
        _merge_into(doc, new)
    else:
        doc = new
    buf = StringIO()
    rt.dump(doc, buf)
    f.write_text(buf.getvalue(), encoding="utf-8")
    return f


# ── 바인딩 해석 ───────────────────────────────────────────────────────────────
# 두 모드: **로컬 경로**(메인테이너 — 직접 편집, work tree 보유) · **git URL**(소비 — 원격에서
# pin 해 읽음, work tree 미보유). 소비 아티팩트(chart·snapshot·platform·substrate·starter)는
# 작업 트리에 두지 않는다 = "편집 대상만 로컬"(chart→OCI G6 모델을 나머지로 확장).
CACHE_DIR = ".bee/cache"   # 원격 소비물 fetch 캐시(gitignore — work dir 아님). commit-keyed=재현(규칙 8).


def _is_url(s: str | None) -> bool:
    return bool(s) and s.startswith(("http://", "https://", "git@", "ssh://"))


def _fetch_cached(name: str, url: str, ref: str, root: Path) -> Path:
    """원격 소비 아티팩트를 pin 된 commit 으로 `.bee/cache/<name>@<commit>` 에 fetch — work tree 에
    두지 않는다(정석). ref 이 40-hex=명시 pin(네트워크 0 if 캐시됨) · 브랜치=ls-remote 해석(floating).
    캐시는 commit-keyed → 재현(규칙 8). OCI/helm 캐시와 동형 — bee 가 투명 관리."""
    cache_root = root / CACHE_DIR
    if re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        commit = ref
    else:
        r = subprocess.run(["git", "ls-remote", url, ref], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise WorkspaceError(
                f"{name}: 원격 ref 해석 실패 — `git ls-remote {url} {ref}`\n{r.stderr.strip()}"
            )
        commit = r.stdout.split()[0]
    dest = cache_root / f"{name}@{commit}"
    if dest.exists():
        return dest
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp = cache_root / f".{name}.tmp"
    if tmp.exists():
        subprocess.run(["rm", "-rf", str(tmp)], check=True)
    c = subprocess.run(["git", "clone", "--quiet", url, str(tmp)], capture_output=True, text=True)
    if c.returncode != 0:
        raise WorkspaceError(f"{name}: clone 실패 — {url}\n{c.stderr.strip()}")
    subprocess.run(["git", "-C", str(tmp), "checkout", "--quiet", commit], check=True)
    tmp.rename(dest)
    return dest


def core_infra_dir(ws: Workspace, root: Path) -> Path:
    if _is_url(ws.core_infra_repo):
        return _fetch_cached("core-infra", ws.core_infra_repo, ws.core_infra_ref, root)
    raw = ws.core_infra or os.environ.get("BEE_CORE_INFRA")
    if not raw:
        raise WorkspaceError(
            "coreInfra 바인딩 필요: bee.workspace.yaml 의 coreInfra.path(로컬) 또는 coreInfra.repo(원격 URL) "
            "또는 $BEE_CORE_INFRA"
        )
    p = Path(raw) if Path(raw).is_absolute() else (root / raw)
    if not p.exists():
        raise WorkspaceError(f"coreInfra 경로 없음: {p} — 로컬 path 또는 coreInfra.repo(원격 URL) 지정")
    return p


def chart_dir(ws: Workspace, root: Path) -> Path:
    p = core_infra_dir(ws, root) / CHART_DIR
    if not (p / "Chart.yaml").exists():
        raise WorkspaceError(f"공용 차트 없음: {p}")
    return p


def platform_yaml_path(ws: Workspace, root: Path) -> Path | None:
    """플랫폼 디스크립터 경로. platform 미선언이면 None(검사 생략), 선언했는데 없으면 오류."""
    if not ws.platform:
        return None
    p = core_infra_dir(ws, root) / PLATFORMS_DIR / ws.platform / "platform.yaml"
    if not p.exists():
        raise WorkspaceError(f"디스크립터 없음: {p} — coreInfra.platform 확인")
    return p


def snapshot_repo_dir(ws: Workspace, root: Path) -> Path:
    """snapshot 레포 루트 — URL 이면 원격 fetch 캐시(소비), 경로면 로컬(메인테이너)."""
    repo = ws.snapshot_repo or os.environ.get("BEE_SNAPSHOT")
    if not repo:
        raise WorkspaceError("snapshot 바인딩 필요: bee.workspace.yaml 의 snapshot.repo(경로 또는 URL) 또는 $BEE_SNAPSHOT")
    if _is_url(repo):
        return _fetch_cached("snapshot", repo, ws.snapshot_ref, root)
    p = (root / repo) if not Path(repo).is_absolute() else Path(repo)
    if not p.exists():
        raise WorkspaceError(f"snapshot 경로 없음: {p} — 로컬 경로 또는 snapshot.repo(원격 URL) 지정")
    return p


def resolve_snapshot_env_dir(ws: Workspace, root: Path) -> Path:
    """워크스페이스의 snapshot 설정 → `envs/<env>` 디렉토리 (로컬 경로 또는 원격 캐시)."""
    p = snapshot_repo_dir(ws, root)
    cand = p / "envs" / ws.env
    return cand if cand.exists() else p


def override_dirs(ws: Workspace, root: Path) -> dict[str, Path]:
    """override 모듈 디렉토리 맵. path 가 있으면 사용(상대경로는 root 기준)."""
    out: dict[str, Path] = {}
    for name, ov in ws.locals.items():
        if ov.path is None:
            raise WorkspaceError(
                f"local override {name!r} 에 path 가 없다 — repo clone 은 후속. path 를 지정하라"
            )
        p = ov.path if ov.path.is_absolute() else (root / ov.path)
        if not (p / "module.yaml").exists():
            raise WorkspaceError(f"override {name!r} 경로에 module.yaml 없음: {p}")
        out[name] = p
    return out
