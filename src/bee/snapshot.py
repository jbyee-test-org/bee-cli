"""스냅샷 I/O — 엔트리 로드 + 작성. (POC thin-core 캐리, G8·G9 적응)

스냅샷(Rendered Manifests Pattern)의 1모듈 엔트리 (G8 레이아웃):

    envs/<env>/<module>/
      module.yaml              # 사본 동봉 (G9) — 리졸버 입력·orient 메타·pull 의 출처
      provenance.yaml          # 렌더 출처: repoUrl·moduleCommit·imageDigest·chartVersion·dependsOn
      <kind>-<name>.yaml ×N    # 렌더 매니페스트 — **리소스별 파일 분할** (G8: diff = 실질 변경)
      db/migration|seed/       # Phase 3 슬롯 (마이그레이션 어휘)
      contracts/               # Phase 3 슬롯 (API 계약 조회)

POC 대비 적응: manifests.yaml 단일 파일 → 리소스별 분할(G8). provenance 는 필수 기록(G8),
renderedAt 없음(무변경 publish 가 diff 를 만들면 안 됨 — 시각은 git 이 안다).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

RESERVED = {"module.yaml", "provenance.yaml"}


@dataclass(frozen=True)
class SnapshotModule:
    name: str
    module_yaml: Path
    manifests: tuple[Path, ...]          # 리소스별 파일들 (비어 있을 수 있음)
    provenance: Path | None = None
    db_dir: Path | None = None           # Phase 3 슬롯
    contracts_dir: Path | None = None    # Phase 3 슬롯


def load_snapshot(env_dir: Path) -> dict[str, SnapshotModule]:
    """`envs/<env>` 디렉토리에서 모듈별 스냅샷 엔트리를 로드."""
    out: dict[str, SnapshotModule] = {}
    if not env_dir.exists():
        return out
    for mdir in sorted(p for p in env_dir.iterdir() if p.is_dir()):
        module_yaml = mdir / "module.yaml"
        if not module_yaml.exists():
            continue
        data = yaml.safe_load(module_yaml.read_text(encoding="utf-8")) or {}
        name = (data.get("metadata") or {}).get("name") or mdir.name
        manifests = tuple(sorted(
            p for p in mdir.glob("*.yaml") if p.name not in RESERVED
        ))
        provenance = mdir / "provenance.yaml"
        db_dir = mdir / "db"
        contracts_dir = mdir / "contracts"
        out[name] = SnapshotModule(
            name=name,
            module_yaml=module_yaml,
            manifests=manifests,
            provenance=provenance if provenance.exists() else None,
            db_dir=db_dir if db_dir.is_dir() else None,
            contracts_dir=contracts_dir if contracts_dir.is_dir() else None,
        )
    return out


def _split_filename(doc: dict, index: int, used: set[str]) -> str:
    """렌더 문서 → 리소스별 파일명 `<kind>-<name>.yaml` (충돌 시 인덱스)."""
    kind = str(doc.get("kind") or "resource").lower()
    name = str((doc.get("metadata") or {}).get("name") or index).lower()
    base = f"{kind}-{name}"
    fname = f"{base}.yaml"
    n = 1
    while fname in used or fname in RESERVED:
        fname = f"{base}-{n}.yaml"
        n += 1
    used.add(fname)
    return fname


def write_entry(
    env_dir: Path,
    name: str,
    module_yaml_src: Path,
    manifests: str,
    *,
    provenance: dict | None = None,
    db_src: Path | None = None,
    include_seed: bool = False,
    contracts_src: Path | None = None,
) -> Path:
    """렌더 결과로 `envs/<env>/<name>/` 엔트리를 (재)작성 — 리소스별 파일 분할(G8).

    provenance(repoUrl·moduleCommit·imageDigest·chartVersion·dependsOn — G8)는 publish 가
    같은 원본에서 동시 생성한다(G9 — module.yaml 사본과의 중복은 허용, 불일치 불가능).
    db/contracts 동봉은 Phase 3 어휘와 함께 활성화(슬롯 유지).
    """
    dest = env_dir / name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(module_yaml_src, dest / "module.yaml")

    # 기존 매니페스트 파일 제거 후 리소스별 재작성
    for f in dest.glob("*.yaml"):
        if f.name not in RESERVED:
            f.unlink()
    used: set[str] = set()
    docs = [d for d in yaml.safe_load_all(manifests) if d]
    for i, doc in enumerate(docs):
        fname = _split_filename(doc, i, used)
        (dest / fname).write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    if provenance is not None:
        (dest / "provenance.yaml").write_text(
            yaml.safe_dump(provenance, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    db_dest = dest / "db"
    if db_dest.exists():
        shutil.rmtree(db_dest)
    if db_src is not None:
        for sub in ("migration", "seed") if include_seed else ("migration",):
            src = db_src / sub
            if src.is_dir():
                shutil.copytree(src, db_dest / sub)

    contracts_dest = dest / "contracts"
    if contracts_dest.exists():
        shutil.rmtree(contracts_dest)
    if contracts_src is not None and contracts_src.is_dir():
        shutil.copytree(contracts_src, contracts_dest)
    return dest
