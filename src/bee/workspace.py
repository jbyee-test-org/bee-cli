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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WORKSPACE_FILE = "bee.workspace.yaml"
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
    snapshot_repo: str | None = None     # 경로 또는 URL
    snapshot_ref: str = "main"
    core_infra: str | None = None        # 경로 또는 URL (G5 — 1-바인딩)
    platform: str | None = None          # platforms/<name>/platform.yaml
    chart_ref: str | None = None         # oci://… — 있으면 OCI 소비(G6), 모듈 pin 이 --version
    cluster_context: str | None = None   # 인너루프 클러스터 kubectl 컨텍스트 (G7)
    locals: dict[str, LocalOverride] = field(default_factory=dict)

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
        if self.core_infra or self.platform:
            ci: dict = {}
            if self.core_infra:
                ci["path"] = self.core_infra
            if self.platform:
                ci["platform"] = self.platform
            if self.chart_ref:
                ci["chartRef"] = self.chart_ref
            doc["coreInfra"] = ci
        if self.cluster_context:
            doc["cluster"] = {"context": self.cluster_context}
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
            platform=ci.get("platform"),
            chart_ref=ci.get("chartRef"),
            cluster_context=(data.get("cluster") or {}).get("context"),
            locals=locals_,
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


def save_workspace(root: Path, ws: Workspace) -> Path:
    f = root / WORKSPACE_FILE
    f.write_text(
        yaml.safe_dump(ws.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return f


# ── 바인딩 해석 (Phase 1 = 로컬 경로. git URL clone 은 후속) ───────────────────
def core_infra_dir(ws: Workspace, root: Path) -> Path:
    raw = ws.core_infra or os.environ.get("BEE_CORE_INFRA")
    if not raw:
        raise WorkspaceError("coreInfra 경로 필요: bee.workspace.yaml 의 coreInfra.path 또는 $BEE_CORE_INFRA")
    p = Path(raw) if Path(raw).is_absolute() else (root / raw)
    if not p.exists():
        raise WorkspaceError(f"coreInfra 경로 없음(로컬만 지원, git URL 은 후속): {p}")
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


def resolve_snapshot_env_dir(ws: Workspace, root: Path) -> Path:
    """워크스페이스의 snapshot 설정 → `envs/<env>` 디렉토리 (Phase 1 = 로컬 경로)."""
    repo = ws.snapshot_repo or os.environ.get("BEE_SNAPSHOT")
    if not repo:
        raise WorkspaceError("snapshot 경로 필요: bee.workspace.yaml 의 snapshot.repo 또는 $BEE_SNAPSHOT")
    p = (root / repo) if not Path(repo).is_absolute() else Path(repo)
    if not p.exists():
        raise WorkspaceError(f"snapshot 경로 없음(로컬만 지원, git URL 은 후속): {p}")
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
