# bee-cli — thin CLI (기계적 배치)

> 단일 기준은 워크스페이스 `GENESIS.md`. G5 고정 3 레포 중 하나.
> **엔진=chart(helm) · 데이터=values · 검증=CI** — CLI 에 derive/gate 를 추가하지 마라(규칙 1·2).

## 설치 (전역, 네트워크)

```sh
uv tool install git+https://github.com/jbyee-test-org/bee-cli          # 최신 main
uv tool install git+https://github.com/jbyee-test-org/bee-cli@v0.4.0   # 핀 (재현성, 규칙 8)
```

`bee` 가 `~/.local/bin` 에 깔린다(PATH 에 있으면 즉시 사용). 워크스페이스 어디서든 `bee <cmd>`.
갱신은 `uv tool upgrade bee-cli`(또는 `uv tool install --reinstall git+…`), 제거는
`uv tool uninstall bee-cli`. 공개 레포라 인증 불필요.

> **개발 중**(이 레포를 고치며)에는 전역 설치본 대신 편집 반영되는 `uv run` 을 쓴다:
> ```sh
> uv run --project repos/bee-cli bee doctor      # 워크스페이스 루트에서
> ```

## 커맨드 표면

```
render · build · up · down · status · publish · pull · new · doctor      + REPL (bee -i)
```

| 커맨드 | 하는 일 |
|---|---|
| `render` | 모듈 렌더(helm template 위임) |
| `build` | docker build → kind load (편집 표면) |
| `up` | 서브그래프 배포(deps-first): local=빌드+render+apply, 나머지=snapshot backdrop |
| `down` | 워크로드 내림(ns·데이터 보존, 규칙 9) |
| `status` | 스냅샷 pin vs HEAD — 내 서브그래프 변경만 |
| `publish` | 공유 env 렌더 + 스냅샷 엔트리(CI 가 headless 재사용) |
| `pull` | 스냅샷 backdrop 모듈 → 편집 표면 |
| `new` | starter 복사 + 이름 치환 + 등록 |
| `doctor` | 환경 진단(읽기 전용) — 도구·바인딩·클러스터·pin. 게이트 아님(규칙 2) |

워크스페이스(`bee.workspace.yaml`)를 cwd 상위로 탐색 → coreInfra 바인딩으로 chart 해석(G6,
경로 또는 `chartRef: oci://…`). 버전 대조(모듈 pin vs chart 실버전 vs platform 지원 범위)는
**경고만** — 차단은 CI.

## 구조 (thin 코어 = POC 캐리, salvage/thin-core 출처)

| 파일 | 역할 |
|---|---|
| `cli.py` | Typer 앱 — 전 커맨드 + 좌표 주입(namespace·migrationWave·dbMigrationsHash) |
| `workspace.py` | 편집 표면 + baseline + 바인딩 (규칙 5). ruamel round-trip 저장(주석 보존) |
| `resolver.py` | dependsOn 서브그래프 위상정렬·깊이 (규칙 6) — 관대 파싱(G9) |
| `snapshot.py` | 스냅샷 엔트리 I/O (규칙 7) — 리소스별 분할(G8)·module.yaml 사본(G9) |
| `kube.py` | kubectl/docker/git 위임 (얇은 래퍼) |
| `repl.py` | 대화형 REPL (textual, k9s 풍) |
