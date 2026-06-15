"""의존성 서브그래프 리졸버 — thin CLI 고유 책임. (POC thin-core 캐리, G9 적응)

`module.yaml`의 `dependsOn` 을 읽어 함께 띄울 **서브그래프만** 계산한다(전체 아님 — 규칙 6).
위상정렬은 의존성-먼저(deps-first) → backdrop 을 의존 순서대로 기동 가능.
순환·누락(미지) 의존성을 감지해 보고한다.

POC 대비 적응(G9): **스키마 검증 제거** — 계약 검증은 chart 의 values.schema.json(helm) +
CI lint 가 한다. 여기는 dependsOn 만 **관대하게 파싱**한다(metadata.name 없는 파일은 무시).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class DependencyError(Exception):
    """리졸버 입력 오류(미지 root 등)."""


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    depends_on: tuple[str, ...]
    path: Path | None = None


@dataclass(frozen=True)
class ResolveResult:
    root: str
    order: list[str]          # 위상정렬(의존성 먼저), 서브그래프
    missing: list[str]        # dependsOn 에 있으나 모듈 집합에 없는 이름
    cycle: list[str]          # 순환에 묶여 정렬 불가한 노드(있으면 문제)


def spec_from_data(data: dict, path: Path | None = None) -> ModuleSpec | None:
    """module.yaml dict → ModuleSpec. 관대 파싱 — name 없으면 None(무시)."""
    if not isinstance(data, dict):
        return None
    name = (data.get("metadata") or {}).get("name")
    if not name:
        return None
    depends = tuple((data.get("spec") or {}).get("dependsOn") or [])
    return ModuleSpec(name=str(name), depends_on=depends, path=path)


def load_module_file(path: Path) -> ModuleSpec | None:
    """단일 module.yaml 로드(관대). 파싱 불가/이름 없음 → None."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return spec_from_data(data, path)


def load_modules(dirs: dict[str, Path]) -> dict[str, ModuleSpec]:
    """{이름: 디렉토리} 맵에서 module.yaml 들을 관대하게 로드."""
    modules: dict[str, ModuleSpec] = {}
    for _, d in sorted(dirs.items()):
        spec = load_module_file(d / "module.yaml")
        if spec is not None:
            modules[spec.name] = spec
    return modules


def depth(modules: dict[str, ModuleSpec], root: str) -> int:
    """dependsOn 그래프에서 root 의 깊이 = 가장 긴 의존 체인(잎=0).

    마이그레이션 순서(구 D10 + G14③ cross-module): 의존 모듈이 먼저 migrate 해야
    cross-schema grant 가 성립한다. 깊이를 sync-wave 로 써서 deps-first 를 ArgoCD 에 전달.
    """
    seen: dict[str, int] = {}

    def visit(n: str, stack: frozenset[str]) -> int:
        if n in seen:
            return seen[n]
        deps = [d for d in modules[n].depends_on if d in modules and d not in stack] if n in modules else []
        d = 0 if not deps else 1 + max(visit(x, stack | {n}) for x in deps)
        seen[n] = d
        return d

    return visit(root, frozenset()) if root in modules else 0


def _reachable(modules: dict[str, ModuleSpec], root: str) -> set[str]:
    """root 에서 dependsOn 을 따라 도달 가능한 노드(자기 자신 포함)."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in modules:
            stack.extend(modules[node].depends_on)
    return seen


def _toposort(modules: dict[str, ModuleSpec], nodes: set[str]) -> tuple[list[str], list[str]]:
    """nodes 부분그래프를 의존성-먼저로 위상정렬. (순서, 순환노드) 반환. 이름순 결정성."""
    indeg = {n: 0 for n in nodes}
    enables: dict[str, list[str]] = {n: [] for n in nodes}  # dep -> [dependents]
    for n in nodes:
        for dep in modules[n].depends_on if n in modules else ():
            if dep in nodes:
                enables[dep].append(n)
                indeg[n] += 1

    ready = sorted(n for n in nodes if indeg[n] == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in sorted(enables[node]):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                ready.append(dependent)
        ready.sort()

    cycle = sorted(nodes - set(order))  # 정렬 못 한 노드 = 순환에 묶임
    return order, cycle


def resolve_workspace(
    modules: dict[str, ModuleSpec],
    roots: list[str],
    *,
    include_all: bool = False,
) -> ResolveResult:
    """여러 root(local override)의 의존 폐포를 위상정렬로 해소.

    roots 자신도 결과(order)에 포함 — `up` 은 override(from-local)와 backdrop(from-snapshot)을
    한 plan 으로 합치기 때문(규칙 5·6).
    """
    unknown = [r for r in roots if r not in modules]
    if unknown:
        known = ", ".join(sorted(modules)) or "(없음)"
        raise DependencyError(f"모듈 없음: {', '.join(unknown)}. 알려진 모듈: {known}")

    if include_all:
        nodes = set(modules)
    else:
        nodes = set()
        for root in roots:
            nodes |= _reachable(modules, root)
    known_nodes = {n for n in nodes if n in modules}

    missing = sorted({
        dep
        for n in known_nodes
        for dep in modules[n].depends_on
        if dep not in modules
    })
    order, cycle = _toposort(modules, known_nodes)
    return ResolveResult(root=",".join(sorted(roots)), order=order, missing=missing, cycle=cycle)
