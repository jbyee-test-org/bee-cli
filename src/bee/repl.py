"""bee REPL — k9s 스타일 풀스크린 TUI (textual) + 비-TTY 텍스트 폴백.

UX 계약(GENESIS REPL 모델):
- 매 액션 후 오리엔트(메인 화면) 복귀 — TUI 에서는 메인 화면 자체가 오리엔트.
- 변경 액션(up·build·down) = 가이드 프롬프트: 무엇을 하는지 + 동등명령 echo(`= bee up web`)
  + 확인. 조회(status)는 즉시 실행. 동등명령으로 배우고 플래그로 졸업.
- 소스 = 멤버십(규칙 5): 편집 표면(local=cyan ✎) vs backdrop(snapshot=gray ·) 구분 표기.
- 색 = 의미: local=cyan · snapshot=gray · 성공=green · down=yellow · 위험=red · 좌표=magenta.

구조:
- TTY → BeeApp(textual 풀스크린): 상단 헤더(platform·env·cluster·pin·plan) + 모듈 테이블
  + 하단 키 힌트. 한 키 액션(u/b/d/s), 방향키 탐색, space 마크. impl 실행은 suspend 로
  화면을 내리고 typer.echo 출력을 그대로 보여준 뒤 복귀(계약 6).
- 비-TTY(파이프/CI) → _fallback_repl: 텍스트 오리엔트 + 단어/번호 입력 폴백(계약 5).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from bee import cli, kube, resolver
from bee import snapshot as snap_mod
from bee import workspace as wsm

LOCAL, SNAP = typer.colors.CYAN, typer.colors.BRIGHT_BLACK
OK, WARN, DANGER = typer.colors.GREEN, typer.colors.YELLOW, typer.colors.RED

LOGO = (
    " _\n"
    "| |__  ___ ___\n"
    "| '_ \\/ -_) -_)\n"
    "|_.__/\\___\\___|"
)


def s(text, fg=None, dim=False, bold=False):
    return typer.style(text, fg=fg, dim=dim, bold=bold)


def _tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
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
        # 좌표(product→ns) + 배포 상태(모듈별 namespace 의 Deployment 존재 여부)
        self.coords: dict[str, str] = {}
        self.deployed: set[str] = set()
        ctx = ws.cluster_context or ""
        ns_seen: dict[str, set[str]] = {}
        for name in self.order:
            data = cli._module_data(name, self.overrides, self.snaps)
            prod = (data.get("spec") or {}).get("product")
            ns = self.products.get(prod) or ""
            self.coords[name] = f"{prod or '?'}→{ns or '?'}"
            if not ns or not ctx:
                continue
            if ns not in ns_seen:
                try:
                    ns_seen[ns] = kube.deployed_names(ctx, ns)
                except Exception:  # kubectl 부재/클러스터 다운 — orient 는 조회일 뿐
                    ns_seen[ns] = set()
            if name in ns_seen[ns]:
                self.deployed.add(name)
        # snapshot pin (lock)
        lock = cli._yaml_at(root / wsm.LOCK_FILE)
        self.pin = ((lock.get("snapshot") or {}).get("commit") or "")[:7]

    def badge(self, name):
        return s("● up  ", fg=OK) if name in self.deployed else s("○ idle", dim=True)


# ── 비-TTY 텍스트 폴백 (계약 5: 파이프 모드에서 깨지지 않아야 함) ─────────────────
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
        typer.echo(f"    {m.badge(name)}  {s('✎ ' + name, fg=LOCAL)}   "
                   f"{s(m.coords.get(name, '?'), fg=typer.colors.MAGENTA)}")
    typer.echo(f"  {s('backdrop', fg=SNAP)} {s('(from-snapshot · dependsOn 폐포, 규칙 6·7)', dim=True)}")
    for name in m.backdrop:
        typer.echo(f"    {m.badge(name)}  {s('· ' + name, fg=SNAP)}   "
                   f"{s(m.coords.get(name, '?'), dim=True)}")
    if m.missing:
        typer.echo("    " + s("⚠ 누락: " + ", ".join(m.missing), fg=WARN))


def _checklist_numbered(title, items):
    """다중 선택 폴백. items=[(name,group,note,colkey)] — 번호 토글 · a=all · ⏎ 확정."""
    sel: set[str] = set()
    while True:
        typer.echo("\n" + s(title, bold=True) + "  " + s("[번호 토글 · a=all · ⏎ 확정]", dim=True))
        idx, last = {}, None
        for i, (name, group, note, colk) in enumerate(items, 1):
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


def _fb_up(m: Model):
    items = [(n, "from-local", m.badge(n), "local") for n in sorted(m.overrides)] \
        + [(n, "from-snapshot", m.badge(n), "snap") for n in m.backdrop]
    sel = _checklist_numbered("up ▸ pick  (● up = 이미 떠있음)", items)
    if not sel:
        typer.echo("  (선택 없음)")
        return
    locals_sel = sorted(n for n in sel if n in m.overrides)
    roots = locals_sel or sorted(sel)
    if confirm(f"bee up {' '.join(roots)}"):
        cli.up_impl(roots, m.root, m.ws)


def _fb_build(m: Model):
    sel = _checklist_numbered("build ▸ pick (from-local 만)",
                              [(n, "from-local", "", "local") for n in sorted(m.overrides)])
    if not sel:
        return
    if confirm(f"bee build {' '.join(sorted(sel))}"):
        cli.build_impl(sorted(sel), m.root, m.ws)


def _fb_down(m: Model):
    typer.echo("  " + s("down", fg=WARN) + " — 워크로드만 내림. "
               + s("데이터·namespace 보존(규칙 9)", dim=True) + ".")
    if confirm("bee down"):
        cli.down_impl(m.root, m.ws)


def _fallback_repl(root: Path):
    typer.echo(s("bee — 비-TTY 텍스트 폴백 (TTY 에서는 풀스크린 TUI). 액션 단어/번호 입력.", dim=True))
    aliases = {"1": "up", "2": "build", "3": "down", "4": "status"}
    while True:
        ws = wsm.load_workspace(root)
        try:
            m = Model(root, ws)
        except (wsm.WorkspaceError, resolver.DependencyError) as e:
            typer.secho(f"⚠ {e}", fg=WARN, err=True)
            return
        orient(m)
        typer.echo("\n  " + s("1 up · 2 build · 3 down · 4 status · publish · pull · q quit", dim=True))
        try:
            ch = input("bee ▸ ").strip().lower()
        except EOFError:
            break
        ch = aliases.get(ch, ch)
        try:
            if ch in ("q", "quit", "exit"):
                break
            elif ch == "up":
                _fb_up(m)
            elif ch == "build":
                _fb_build(m)
            elif ch == "down":
                _fb_down(m)
            elif ch == "status":
                cli.status_impl(root, ws)
            elif ch == "publish":
                names = sorted(m.overrides)
                if names and confirm(f"bee publish dev {' '.join(names)}"):
                    cli.publish_impl("dev", names, root, ws)
            elif ch == "pull":
                items = [(n, "from-snapshot", "", "snap") for n in m.backdrop]
                sel = sorted(_checklist_numbered("pull ▸ backdrop → 편집 표면", items)) if items else []
                if sel and confirm(f"bee pull {' '.join(sel)}"):
                    cli.pull_impl(sel, root, ws)
            elif ch:
                typer.echo("  " + s("? 모르는 액션: " + ch, dim=True))
        except typer.Exit:
            pass
        except kube.ToolError as e:
            typer.secho(f"⚠ 도구 실패:\n{e}", fg=DANGER, err=True)
    typer.echo(s("bye", dim=True))


# ── 풀스크린 TUI (k9s 스타일) ──────────────────────────────────────────────────
class ConfirmScreen(ModalScreen[bool]):
    """가이드 프롬프트 모달 — 무엇을 하는지 + 동등명령 echo + 확인(y/n)."""

    BINDINGS = [
        Binding("y", "yes", "진행"),
        Binding("enter", "yes", "진행", show=False),
        Binding("n", "no", "취소"),
        Binding("escape", "no", "취소", show=False),
    ]

    def __init__(self, title: str, body: str, equiv: str, tone: str = "ok"):
        super().__init__(classes=f"tone-{tone}")
        self._title, self._body, self._equiv = title, body, equiv

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as v:
            v.border_title = f" {self._title} "
            yield Static(self._body, id="c-body")
            yield Static(f"[dim]= {self._equiv}[/]", id="c-equiv")
            yield Static("[b]y[/][dim]/⏎ 진행  ·[/] [b]n[/][dim]/esc 취소[/]", id="c-keys")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class BeeApp(App[None]):
    """k9s 스타일 오리엔트: 헤더(컨텍스트) + 모듈 테이블 + 키 힌트 바."""

    CSS = """
    #topbar { height: 4; padding: 0 1; }
    #ctx { width: 1fr; }
    #logo { width: auto; color: yellow; }
    #modules { height: 1fr; border: round cyan; }
    ConfirmScreen { align: center middle; }
    #dialog { width: 76; height: auto; padding: 1 2; background: $surface; border: round cyan; }
    ConfirmScreen.tone-warn #dialog { border: round yellow; }
    ConfirmScreen.tone-danger #dialog { border: round red; }
    #c-equiv { margin-top: 1; }
    #c-keys { margin-top: 1; }
    """

    BINDINGS = [
        Binding("u", "do_up", "up"),
        Binding("b", "do_build", "build"),
        Binding("d", "do_down", "down"),
        Binding("s", "do_status", "status"),
        Binding("space", "toggle_mark", "mark", key_display="␣"),
        Binding("a", "toggle_all", "all"),
        Binding("r", "reload", "refresh"),
        Binding("p", "publish", "publish"),
        Binding("e", "pull", "pull"),
        Binding("q", "quit", "quit"),
        Binding("j", "row_down", "down", show=False),
        Binding("k", "row_up", "up", show=False),
    ]

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.ws: wsm.Workspace | None = None
        self.model: Model | None = None
        self.marked: set[str] = set()
        self._names: list[str] = []

    # ── 레이아웃 ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("", id="ctx")
            yield Static(LOGO, id="logo")
        yield DataTable(id="modules")
        yield Footer()

    def on_mount(self):
        t = self.query_one("#modules", DataTable)
        t.cursor_type = "row"
        t.add_column(" ", key="sel", width=3)
        t.add_column("STATE", key="st", width=8)
        t.add_column("SOURCE", key="src", width=12)
        t.add_column("MODULE", key="mod", width=24)
        t.add_column("PRODUCT→NS", key="coord")
        t.focus()
        self._load_model(first=True)

    def check_action(self, action: str, parameters):  # 모달 위에서는 앱 단축키 잠금
        return len(self.screen_stack) <= 1

    # ── 모델/화면 갱신 (오리엔트 복귀 = 이 함수) ─────────────────────────────────
    def _load_model(self, first: bool = False):
        try:
            self.ws = wsm.load_workspace(self.root)
            self.model = Model(self.root, self.ws)
        except (wsm.WorkspaceError, resolver.DependencyError) as e:
            if first:
                self.exit(return_code=2, message=f"⚠ {e}")
            else:
                self.notify(str(e), title="워크스페이스 오류", severity="error")
            return
        self._fill_table()
        self._fill_header()

    def _fill_header(self):
        m, marked = self.model, len(self.marked)
        up_n, idle = len(m.deployed), len(m.order) - len(m.deployed)
        pin = "@" + m.pin if m.pin else "미pin"
        self.query_one("#ctx", Static).update(
            f"[dim]platform[/] [b]{m.ws.platform or '(미지정)'}[/]    "
            f"[dim]cluster[/] [b]{m.ws.cluster_context or '?'}[/]\n"
            f"[dim]snapshot[/] envs/{m.ws.env} [b]{pin}[/]    [dim]render env[/] local\n"
            f"[dim]plan[/] [green]● {up_n} up[/][dim] · ○ {idle} idle[/]\n"
            f"[dim]surface[/] [cyan]✎ {len(m.overrides)} local[/][dim] · {len(m.backdrop)} backdrop[/]"
            f"    [dim]marked[/] [b]{marked}[/]"
        )

    def _sel_cell(self, name: str) -> Text:
        return Text("◉", style="green") if name in self.marked else Text("◯", style="dim")

    def _fill_table(self):
        t = self.query_one("#modules", DataTable)
        m = self.model
        cur = t.cursor_row
        t.clear()
        self._names = []
        actionable = set(sorted(m.overrides)) | set(m.backdrop)
        self.marked &= actionable
        for name in sorted(m.overrides):
            st = Text("● up", style="green") if name in m.deployed else Text("○ idle", style="dim")
            t.add_row(self._sel_cell(name), st, Text("✎ local", style="cyan"),
                      Text(name, style="bold cyan"),
                      Text(m.coords.get(name, "?"), style="magenta"), key=name)
            self._names.append(name)
        for name in m.backdrop:
            st = Text("● up", style="green") if name in m.deployed else Text("○ idle", style="dim")
            t.add_row(self._sel_cell(name), st, Text("· snapshot", style="bright_black"),
                      Text(name, style="bright_black"),
                      Text(m.coords.get(name, "?"), style="magenta dim"), key=name)
            self._names.append(name)
        for name in m.missing:
            t.add_row(Text(" "), Text("⚠", style="red"), Text("✗ missing", style="red"),
                      Text(name, style="red dim"),
                      Text("local·snapshot 어디에도 없음", style="dim"), key=name)
            self._names.append(name)
        warn = f" · [red]⚠ 누락 {len(m.missing)}[/]" if m.missing else ""
        t.border_title = (f" 모듈 [{len(self._names)}] · [cyan]✎ 편집표면 {len(m.overrides)}[/]"
                          f" · backdrop {len(m.backdrop)}{warn} ")
        if self._names:
            t.move_cursor(row=min(cur or 0, len(self._names) - 1))

    # ── 탐색/선택 ─────────────────────────────────────────────────────────────
    def _cursor_name(self) -> str | None:
        r = self.query_one("#modules", DataTable).cursor_row
        return self._names[r] if self._names and r is not None and 0 <= r < len(self._names) else None

    def action_row_down(self):
        self.query_one("#modules", DataTable).action_cursor_down()

    def action_row_up(self):
        self.query_one("#modules", DataTable).action_cursor_up()

    def action_toggle_mark(self):
        name = self._cursor_name()
        if not name:
            return
        if name in self.model.missing:
            self.notify("누락 모듈 — 선택 불가", severity="warning")
            return
        self.marked ^= {name}
        self.query_one("#modules", DataTable).update_cell(name, "sel", self._sel_cell(name))
        self._fill_header()

    def action_toggle_all(self):
        actionable = set(sorted(self.model.overrides)) | set(self.model.backdrop)
        self.marked = set() if self.marked >= actionable else set(actionable)
        self._fill_table()
        self._fill_header()

    def _targets(self) -> list[str]:
        sel = [n for n in self._names if n in self.marked]
        if not sel:
            c = self._cursor_name()
            if c and c not in self.model.missing:
                sel = [c]
        return sel

    # ── 액션: 변경 = 가이드 프롬프트 → suspend 실행 → 오리엔트 복귀 ───────────────
    def _run_suspended(self, equiv: str, fn):
        with self.suspend():
            typer.echo("\n" + s("= " + equiv, dim=True))
            try:
                fn()
            except typer.Exit:
                pass
            except kube.ToolError as e:
                typer.secho(f"⚠ 도구 실패:\n{e}", fg=DANGER, err=True)
            except Exception as e:
                typer.secho(f"⚠ {e}", fg=DANGER, err=True)
            try:
                input(s("\n⏎ bee 로 돌아가기 ", dim=True))
            except EOFError:
                pass
        self._load_model()  # 오리엔트 복귀(메인 화면 갱신)

    def action_do_up(self):
        m = self.model
        sel = self._targets()
        if not sel:
            self.notify("대상 없음 — space 로 마크하거나 행 위에서 u", severity="warning")
            return
        locals_sel = sorted(n for n in sel if n in m.overrides)
        roots = locals_sel or sorted(sel)
        lines = []
        for n in sel:
            if n in m.overrides:
                lines.append(f"[cyan]✎ {n}[/] [dim]local — 자동 빌드(docker→kind) + render + apply[/]")
            else:
                lines.append(f"[bright_black]· {n}[/] [dim]snapshot 매니페스트 apply[/]")
        lines.append("")
        lines.append("[dim]dependsOn 폐포 자동 cascade(규칙 6·7) · 완료 시 snapshot pin 기록(규칙 8)[/]")
        equiv = "bee up " + " ".join(roots)
        self.push_screen(
            ConfirmScreen("up — 배포 (pick + 자동 cascade)", "\n".join(lines), equiv),
            lambda ok: self._run_suspended(equiv, lambda: cli.up_impl(roots, self.root, self.ws)) if ok else None,
        )

    def action_do_build(self):
        m = self.model
        names = sorted(n for n in self._targets() if n in m.overrides)
        if not names:
            self.notify("build 는 from-local 전용(규칙 5) — 편집 표면 모듈을 선택", severity="warning")
            return
        body = "\n".join(f"[cyan]✎ {n}[/] [dim]docker build → kind load[/]" for n in names)
        equiv = "bee build " + " ".join(names)
        self.push_screen(
            ConfirmScreen("build — 이미지 (from-local → kind)", body, equiv),
            lambda ok: self._run_suspended(equiv, lambda: cli.build_impl(names, self.root, self.ws)) if ok else None,
        )

    def action_do_down(self):
        body = ("[yellow]plan 전체 워크로드 내림[/] — 의존 역순 delete.\n"
                "[dim]데이터·namespace 보존(규칙 9) · 부분 down 없음[/]")
        equiv = "bee down"
        self.push_screen(
            ConfirmScreen("down — 워크로드 내림", body, equiv, tone="warn"),
            lambda ok: self._run_suspended(equiv, lambda: cli.down_impl(self.root, self.ws)) if ok else None,
        )

    def action_do_status(self):  # 조회 — 즉시 실행(확인 없음)
        self._run_suspended("bee status", lambda: cli.status_impl(self.root, self.ws))

    def action_reload(self):
        self._load_model()
        self.notify("오리엔트 갱신")

    def action_publish(self):
        m = self.model
        names = sorted(n for n in self._targets() if n in m.overrides) or sorted(m.overrides)
        if not names:
            self.notify("publish 는 편집 표면(from-local) 전용(규칙 5)", severity="warning")
            return
        body = "\n".join(f"[cyan]✎ {n}[/] [dim]렌더(values-dev) + 엔트리 + 스냅샷 커밋[/]" for n in names)
        body += ("\n\n[dim]env=dev 고정(다른 env 는 CLI 플래그로 — 졸업 경로) · digest 미주입"
                 " → 게이트1이 차단 · 무변경이면 커밋 생략(G8)[/]")
        equiv = "bee publish dev " + " ".join(names)
        self.push_screen(
            ConfirmScreen("publish — 스냅샷 레포 커밋", body, equiv),
            lambda ok: self._run_suspended(
                equiv, lambda: cli.publish_impl("dev", names, self.root, self.ws)) if ok else None,
        )

    def action_pull(self):
        m = self.model
        names = sorted(n for n in self._targets() if n in m.backdrop)
        if not names:
            self.notify("pull 대상은 backdrop(from-snapshot) 모듈 — 행을 선택", severity="warning")
            return
        lines = []
        for n in names:
            sm = m.snaps.get(n)
            url = (cli._yaml_at(sm.provenance).get("repoUrl") or "?") if sm and sm.provenance else "?"
            lines.append(f"[bright_black]· {n}[/] [dim]{url} → repos/{n}[/]")
        lines.append("")
        lines.append("[dim]clone + 워크스페이스 local: 등록 — 소스=멤버십(규칙 5), 편집 표면 진입[/]")
        equiv = "bee pull " + " ".join(names)
        self.push_screen(
            ConfirmScreen("pull — 편집 시작 (backdrop → 편집 표면)", "\n".join(lines), equiv),
            lambda ok: self._run_suspended(
                equiv, lambda: cli.pull_impl(names, self.root, self.ws)) if ok else None,
        )

    def on_data_table_row_selected(self, event):  # ⏎ = 커서 행 up (가이드 프롬프트 경유)
        self.action_do_up()


# ── 진입점 (시그니처 불변: cli 콜백이 인자 없이 부른다) ─────────────────────────
def repl():
    try:
        root = wsm.find_root(Path.cwd())
    except wsm.WorkspaceError as e:
        typer.secho(f"⚠ {e}", fg=WARN, err=True)
        raise typer.Exit(2)
    if not _tty():
        _fallback_repl(root)
        return
    app = BeeApp(root)
    try:
        app.run()
    except Exception as e:  # 풀스크린 불가 터미널 — 텍스트 폴백으로 강등
        typer.secho(f"⚠ TUI 실패({e}) — 텍스트 폴백으로 전환", fg=WARN, err=True)
        _fallback_repl(root)
        return
    typer.echo(s("bye", dim=True))
