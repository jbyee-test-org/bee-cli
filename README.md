# bee-cli — thin CLI (기계적 배치)

> 단일 기준은 워크스페이스 `GENESIS.md`. G5 고정 3 레포 중 하나.
> **엔진=chart(helm) · 데이터=values · 검증=CI** — CLI 에 derive/gate 를 추가하지 마라(규칙 1·2).

## 실행

```sh
uv run --project repos/bee-cli bee render hello            # 워크스페이스 루트에서
uv run --project repos/bee-cli bee render hello -e dev
```

워크스페이스(`bee.workspace.yaml`)를 cwd 상위로 탐색 → coreInfra 바인딩으로 chart 해석(G6).
버전 대조(모듈 pin vs chart 실버전 vs platform 지원 범위)는 **경고만** — 차단은 CI(G6).

## 구조 (thin 코어 = POC 캐리, salvage/thin-core 출처)

| 파일 | 역할 | 캐리 적응 |
|---|---|---|
| `cli.py` | Typer 앱 — Phase 1: `render` | 신규 |
| `workspace.py` | 편집 표면 + baseline + 바인딩 (규칙 5) | 개명 · coreInfra 1-바인딩(G5) |
| `resolver.py` | dependsOn 서브그래프 위상정렬 (규칙 6) | 스키마 검증 제거 — 관대 파싱(G9) |
| `snapshot.py` | 스냅샷 엔트리 I/O (규칙 7) | 리소스별 파일 분할(G8) · module.yaml 사본(G9) |

## next (Phase 1 마일스톤)

`up`(서브그래프 + render + kubectl apply) → `build`(docker + kind load) → backdrop(snapshot) → REPL.
