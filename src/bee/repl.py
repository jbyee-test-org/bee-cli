"""bee REPL — 대화형 인너루프. 위젯은 prototype 캐리(termios | 번호 폴백), 모델은 실데이터.

UX 계약(GENESIS REPL 모델): 매 액션 후 오리엔트 복귀 · 변경 액션=가이드 프롬프트(무엇+동등명령
+확인) · 조회는 즉시 실행 · 색=의미(local=cyan · snapshot=gray · 위험=red/yellow).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from bee import cli, kube, resolver
from bee import snapshot as snap_mod
from bee import workspace as wsm

LOCAL, SNAP = typer.colors.CYAN, typer.colors.BRIGHT_BLACK
OK, WARN, DANGER = typer.colors.GREEN, typer.colors.YELLOW, typer.colors.RED


def s(text, fg=None, dim=False, bold=False, reverse=False):
    return typer.style(text, fg=fg, dim=dim, bold=bold, reverse=reverse)


# ── 키 입력 위젯 (prototype 캐리) ──────────────────────────────────────────────
def _tty():
    try:
        import termios  # noqa: F401

        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _key():
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return {"\x1b[A": "up", "\x1b[B": "down", "\r": "enter", "\n": "enter",
            " ": "space", "\x03": "quit"}.get(ch, ch.lower())


def menu(title, options):
    """단일 선택. options=[(key,label)] (key='--' = 섹션 헤더). 방향키 | 타이핑 폴백."""
    keys = [k for k, _ in options if k != "--"]
    if not _tty():
        try:
            return input(f"\n{title} [{' '.join(keys)}] ▸ ").strip().lower()
        except EOFError:
            return "q"
    pick = [i for i, (k, _) in enumerate(options) if k != "--"]
    cur = 0

    def draw(first):
        if not first:
            sys.stdout.write(f"\x1b[{len(options) + 2}A")
        typer.echo(s(title, bold=True) + "\x1b[K")
        for i, (k, label) in enumerate(options):
            if k == "--":
                typer.echo("  " + s(label, dim=True) + "\x1b[K")
                continue
            if pick[cur] == i:
                typer.echo("  " + s("❯", fg=LOCAL, bold=True) + " " + s(f" {label} ", reverse=True) + "\x1b[K")
            else:
                fg = DANGER if k == "wipe" else WARN if k == "down" else None
                typer.echo("    " + s(label, fg=fg) + "\x1b[K")
        typer.echo(s("  ↑↓ 이동 · ⏎ 선택 · q 취소", dim=True) + "\x1b[K")

    draw(True)
    while True:
        k = _key()
        if k == "up":
            cur = (cur - 1) % len(pick)
        elif k == "down":
            cur = (cur + 1) % len(pick)
        elif k == "enter":
            typer.echo()
            return options[pick[cur]][0]
        elif k == "quit":
            typer.echo()
            return "q"
        draw(False)


def checklist(title, items):
    """다중 선택 items=[(name,group,note,colkey)]. 방향키+space | 번호 폴백."""
    if not _tty():
        return _checklist_numbered(title, items)
    rows, item_idx = [], []
    last = None
    for it in items:
        if it[1] != last:
            rows.append(("h", it[1]))
            last = it[1]
        item_idx.append(len(rows))
        rows.append(("i",) + it)
    sel, cur = set(), 0

    def draw(first):
        if not first:
            sys.stdout.write(f"\x1b[{len(rows) + 2}A")
        typer.echo(s(title, bold=True) + "\x1b[K")
        for r, row in enumerate(rows):
            if row[0] == "h":
                typer.echo("  " + s(row[1], fg=LOCAL if "local" in row[1] else SNAP) + "\x1b[K")
                continue
            _, name, _, note, colk = row
            here = item_idx[cur] == r
            box = s("◉", fg=OK) if name in sel else s("◯", dim=True)
            nm = s(name, fg=LOCAL, bold=True) if here else s(name, fg=LOCAL if colk == "local" else SNAP)
            ptr = s("❯", fg=LOCAL, bold=True) if here else " "
            typer.echo(f"  {ptr} {box} {note}  {nm}" + "\x1b[K")
        typer.echo(s("  ↑↓ · space 토글 · a 전체 · ⏎ 확정", dim=True) + "\x1b[K")

    draw(True)
    while True:
        k = _key()
        if k == "up":
            cur = (cur - 1) % len(item_idx)
        elif k == "down":
            cur = (cur + 1) % len(item_idx)
        elif k == "space":
            sel ^= {rows[item_idx[cur]][1]}
        elif k == "a":
            sel = {rows[i][1] for i in item_idx}
        elif k == "enter":
            typer.echo()
            return sel
        elif k == "quit":
            typer.echo()
            return set()
        draw(False)


def _checklist_numbered(title, items):
    sel = set()
    while True:
        typer.echo("\n" + s(title, bold=True) + "  " + s("[번호 토글 · a=all · ⏎ 확정]", dim=True))
        idx, last = {}, None
        for i, it in enumerate(items, 1):
            name, group, note, colk = it
            if group != last:
                typer.echo("  " + s(group, fg=LOCAL if "local" in group else SNAP))
                last = group
            idx[i] = name
            box = s("◉", fg=OK) if name in sel else s("◯", dim=True)
            typer.echo(f"    {i}) {box} {note}  {s(name, fg=LOCAL if colk == 'local' else SNAP)}")
        try:
            cc = input("  ▸ ").strip().lower()
        except EOFError:
            return sel
        if cc == "":
            return sel
        if cc == "a":
            sel = set(idx.values())
        elif cc.isdigit() and int(cc) in idx:
            sel ^= {idx[int(cc)]}


def confirm(equiv):
    typer.echo("  " + s("= " + equiv, dim=True))
    try:
        return typer.confirm("  진행", default=True)
    except (EOFError, typer.Abort):
        return False


# ── 실데이터 모델 ──────────────────────────────────────────────────────────────
class Model:
    def __init__(self, root: Path, ws: wsm.Workspace):
        self.root, self.ws = root, ws
        self.overrides = wsm.override_dirs(ws, root)
        env_dir = wsm.resolve_snapshot_env_dir(ws, root)
        self.snaps = snap_mod.load_snapshot(env_dir)
        specs: dict[str, resolver.ModuleSpec] = {}
        for n, sm in self.snaps.items():
            sp = resolver.load_module_file(sm.module_yaml)
            if sp:
                specs[n] = sp
        specs.update(resolver.load_modules(self.overrides))
        roots = sorted(self.overrides)
        self.order: list[str] = []
        self.missing: list[str] = []
        if roots:
            res = resolver.resolve_workspace(specs, roots)
            self.order, self.missing = res.order, res.missing
        self.backdrop = [n for n in self.order if n not in self.overrides]
        self.products = cli._products(ws, root)
        # 배포 상태 — 모듈별 namespace 의 Deployment 존재 여부
        self.deployed: set[str] = set()
        ctx = ws.cluster_context or ""
        ns_seen: dict[str, set[str]] = {}
        for name in self.order:
            data = cli._module_data(name, self.overrides, self.snaps)
            ns = self.products.get((data.get("spec") or {}).get("product")) or ""
            if not ns or not ctx:
                continue
            if ns not in ns_seen:
                ns_seen[ns] = kube.deployed_names(ctx, ns)
            if name in ns_seen[ns]:
                self.deployed.add(name)
        # snapshot pin (lock)
        lock = cli._yaml_at(root / wsm.LOCK_FILE)
        self.pin = ((lock.get("snapshot") or {}).get("commit") or "")[:7]

    def badge(self, name):
        return s("● up  ", fg=OK) if name in self.deployed else s("○ idle", dim=True)


def orient(m: Model):
    up_n = len(m.deployed)
    typer.echo()
    typer.echo(f"  {s('❯', dim=True)} {s(m.ws.platform or '(플랫폼 미지정)', bold=True)} · "
               f"{s('local', fg=LOCAL)}        {s('cluster: ' + (m.ws.cluster_context or '?'), dim=True)}")
    typer.echo("  " + s("─" * 62, dim=True))
    typer.echo(f"  {s('snapshot', dim=True)} {s('envs/' + m.ws.env + ' @' + (m.pin or '미pin'), dim=True)}    "
               f"{s('plan', dim=True)} {s('● ' + str(up_n) + ' up', fg=OK)} "
               f"{s('· ' + str(len(m.order) - up_n) + ' idle', dim=True)}")
    typer.echo(f"\n  {s('편집 표면', fg=LOCAL)} {s('(from-local — 멤버십, 규칙 5)', dim=True)}")
    for name in sorted(m.overrides):
        prod = (cli._module_data(name, m.overrides, m.snaps).get("spec") or {}).get("product", "?")
        ns = m.products.get(prod, "?")
        typer.echo(f"    {m.badge(name)}  {s('✎ ' + name, fg=LOCAL)}   {s(f'{prod}→{ns}', fg=typer.colors.MAGENTA)}")
    typer.echo(f"  {s('backdrop', fg=SNAP)} {s('(from-snapshot · dependsOn 폐포, 규칙 6·7)', dim=True)}")
    for name in m.backdrop:
        typer.echo(f"    {m.badge(name)}  {s('· ' + name, fg=SNAP)}")
    if m.missing:
        typer.echo("    " + s("⚠ 누락: " + ", ".join(m.missing), fg=WARN))


# ── 액션 ──────────────────────────────────────────────────────────────────────
def act_up(m: Model):
    items = [(n, "from-local", m.badge(n), "local") for n in sorted(m.overrides)] \
        + [(n, "from-snapshot", m.badge(n), "snap") for n in m.backdrop]
    sel = checklist("up ▸ pick  (● up = 이미 떠있음)", items)
    if not sel:
        typer.echo("  (선택 없음)")
        return
    locals_sel = sorted(n for n in sel if n in m.overrides)
    roots = locals_sel or sorted(sel)
    if confirm(f"bee up {' '.join(roots)}"):
        cli.up_impl(roots, m.root, m.ws)


def act_build(m: Model):
    sel = checklist("build ▸ pick (from-local 만)", [(n, "from-local", "", "local") for n in sorted(m.overrides)])
    if not sel:
        return
    if confirm(f"bee build {' '.join(sorted(sel))}"):
        cli.build_impl(sorted(sel), m.root, m.ws)


def act_down(m: Model):
    typer.echo("  " + s("down", fg=WARN) + " — 워크로드만 내림. " + s("데이터·namespace 보존(규칙 9)", dim=True) + ".")
    if confirm("bee down"):
        cli.down_impl(m.root, m.ws)


def act_status(m: Model):
    try:
        cli.status_impl(m.root, m.ws)
    except typer.Exit:
        pass


def repl():
    root = wsm.find_root(Path.cwd())
    typer.echo(s("bee — 대화형 인너루프. ↑↓·space·⏎ | 비-TTY 번호 폴백.", dim=True))
    options = [
        ("--", "인너루프"),
        ("up", "up       배포 (pick + 자동 cascade)"),
        ("build", "build    이미지 (from-local → kind)"),
        ("down", "down     워크로드 내림 (데이터 보존)"),
        ("--", "조회"),
        ("status", "status   snapshot pin vs HEAD (내 서브그래프만)"),
        ("--", ""),
        ("publish", "publish  (Phase 2)"),
        ("pull", "pull     (Phase 2)"),
        ("q", "q        quit"),
    ]
    while True:
        ws = wsm.load_workspace(root)
        try:
            m = Model(root, ws)
        except (wsm.WorkspaceError, resolver.DependencyError) as e:
            typer.secho(f"⚠ {e}", fg=WARN, err=True)
            return
        orient(m)
        ch = menu("액션", options)
        if ch in ("q", "quit", "exit"):
            break
        handlers = {"up": act_up, "build": act_build, "down": act_down, "status": act_status}
        fn = handlers.get(ch)
        try:
            if fn:
                fn(m)
            elif ch in ("publish", "pull"):
                typer.echo("  " + s(f"{ch}: Phase 2 마일스톤 — 아직 없음", dim=True))
            elif ch:
                typer.echo("  " + s("? 모르는 액션: " + ch, dim=True))
        except typer.Exit:
            pass
        except kube.ToolError as e:
            typer.secho(f"⚠ 도구 실패:\n{e}", fg=DANGER, err=True)
    typer.echo(s("bye", dim=True))
