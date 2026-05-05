"""
CLI entry point for llm-browser.

Usage examples:
  llm ask claude "explain async generators in python"
  llm ask gpt "what is RLHF"
  llm ask gemini "summarise the CAP theorem"
  echo "write a haiku about linux" | llm ask claude -
  llm ask claude "summarise this" -f report.txt
  llm ask claude "what's in these files?" -f a.py -f b.py
  llm login claude
  llm list
  llm serve                  # start daemon in foreground
  llm daemon start           # start daemon in background
  llm daemon stop            # stop daemon
  llm daemon status          # check if daemon is running
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from llm_browser import client
from llm_browser.browser import browser_session
from llm_browser.config import PID_PATH, SERVER_LOG, SOCKET_PATH
from llm_browser.db import get_chat, get_chats, init_db
from llm_browser.providers import get_provider, list_providers
from llm_browser.providers.base import LimitReachedError
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    name="llm",
    help="Query LLMs via your browser — no API key needed.",
    no_args_is_help=True,
)
daemon_app = typer.Typer(help="Manage the background browser daemon.")
app.add_typer(daemon_app, name="daemon")

console = Console()
err = Console(stderr=True)

init_db()


# ──────────────────────────────────────────────────────────────────────
# File injection helper
# ──────────────────────────────────────────────────────────────────────

def _resolve_query(query: Optional[str]) -> str:
    """Return the query string, reading from stdin when appropriate."""
    if query is None or query == "-":
        if sys.stdin.isatty():
            err.print("[red]Error:[/red] no query provided and stdin is a terminal")
            raise typer.Exit(1)
        text = sys.stdin.read().strip()
        if not text:
            err.print("[red]Error:[/red] empty query on stdin")
            raise typer.Exit(1)
        return text
    return query


def _inject_files(query: str, files: list[Path]) -> str:
    """Prepend each file's content to *query* wrapped in <file> tags."""
    if not files:
        return query
    parts: list[str] = []
    for f in files:
        if not f.exists():
            err.print(f"[red]Error:[/red] file not found: {f}")
            raise typer.Exit(1)
        try:
            content = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            err.print(
                f"[red]Error:[/red] {f.name} appears to be a binary file. "
                "Only text files are supported."
            )
            raise typer.Exit(1)
        parts.append(f'<file name="{f.name}">\n{content}\n</file>')
    return "\n\n".join(parts) + "\n\n" + query


# ──────────────────────────────────────────────────────────────────────
# llm list
# ──────────────────────────────────────────────────────────────────────

@app.command("list")
def list_cmd() -> None:
    """List all available providers."""
    console.print("\n[bold]Available providers:[/bold]\n")
    for p in list_providers():
        console.print(f"  [cyan]{p.meta.name:<12}[/cyan] {p.meta.display_name}")
    console.print()


# ──────────────────────────────────────────────────────────────────────
# llm login <provider>
# ──────────────────────────────────────────────────────────────────────

@app.command("login")
def login_cmd(
    provider_name: str = typer.Argument(..., help="Provider to log in to"),
) -> None:
    """
    Open the browser and let you log in to a provider.
    Your session is saved for future queries.
    """
    asyncio.run(_login(provider_name))


async def _login(provider_name: str) -> None:
    provider = get_provider(provider_name)
    console.print(
        f"\n[yellow]Opening [bold]{provider.meta.display_name}[/bold] "
        f"— please log in, then press [bold]Enter[/bold] here.[/yellow]\n"
    )
    async with browser_session(headless=False) as session:
        await session.page.goto(provider.meta.url, wait_until="domcontentloaded")
        await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter once logged in…")
    console.print("[green]Session saved.[/green]\n")


# ──────────────────────────────────────────────────────────────────────
# llm ask <provider> <query>
# ──────────────────────────────────────────────────────────────────────

@app.command("ask")
def ask_cmd(
    provider_name: str = typer.Argument(..., help="Provider: claude | chatgpt | gemini"),
    query: Optional[str] = typer.Argument(
        None,
        help='Query text, "-" to read from stdin, or omit when piping',
    ),
    files: Optional[list[Path]] = typer.Option(
        None,
        "--file",
        "-f",
        help="File(s) to attach — content is injected before the query. Repeatable.",
        exists=False,  # we validate manually for better error messages
    ),
    dom: bool = typer.Option(
        False,
        "--dom",
        help="Force DOM extraction mode (skip network interception)",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print plain text instead of rendered Markdown",
    ),
) -> None:
    """
    Ask a question and stream the response to your terminal.

    Requires the daemon to be running: llm daemon start

    Pipe input directly — no "-" needed:

      echo "what is 2+2" | llm ask claude

    Attach files whose content will be included in the query:

      llm ask claude "summarise this" -f report.txt
      llm ask claude "explain the diff" -f old.py -f new.py
    """
    q = _resolve_query(query)
    q = _inject_files(q, files or [])

    asyncio.run(
        _ask(
            provider_name=provider_name,
            query=q,
            force_dom=dom,
            raw=raw,
        )
    )


async def _ask(
    *,
    provider_name: str,
    query: str,
    force_dom: bool,
    raw: bool,
) -> None:
    if not await client.is_server_running():
        err.print(
            "[red]Error:[/red] daemon is not running.\n"
            "Start it with: [bold]llm daemon start[/bold]"
        )
        raise typer.Exit(1)

    try:
        get_provider(provider_name)  # validate name early
    except ValueError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(
        f"\n[dim]Provider:[/dim] [cyan]{provider_name}[/cyan]  "
        f"[dim]Mode:[/dim] [cyan]{'DOM' if force_dom else 'network→DOM'}[/cyan]  "
        f"[dim]via daemon[/dim]\n"
    )

    full_response = ""
    try:
        if raw:
            async for chunk in client.ask(provider_name, query, force_dom=force_dom):
                print(chunk, end="", flush=True)
                full_response += chunk
            print()
        else:
            with Live(console=console, refresh_per_second=10, vertical_overflow="visible") as live:
                async for chunk in client.ask(provider_name, query, force_dom=force_dom):
                    full_response += chunk
                    live.update(Markdown(full_response))
    except LimitReachedError as exc:
        err.print(f"\n[bold red]Limit reached:[/bold red] {exc}\n")
        raise typer.Exit(1) from exc
    except RuntimeError as exc:
        err.print(f"\n[red]Error:[/red] {exc}\n")
        raise typer.Exit(1) from exc

    console.print()


# ──────────────────────────────────────────────────────────────────────
# llm compare [--provider P]… <query>
# ──────────────────────────────────────────────────────────────────────

@app.command("compare")
def compare_cmd(
    query: Optional[str] = typer.Argument(
        None,
        help='Query to send, "-" to read from stdin, or omit when piping',
    ),
    provider: Optional[list[str]] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Provider to include (repeat for multiple). Default: all providers.",
    ),
    files: Optional[list[Path]] = typer.Option(
        None,
        "--file",
        "-f",
        help="File(s) to attach — content is injected before the query. Repeatable.",
        exists=False,
    ),
    dom: bool = typer.Option(False, "--dom", help="Force DOM extraction mode"),
    raw: bool = typer.Option(False, "--raw", help="Print plain text instead of Markdown"),
) -> None:
    """
    Send the same query to multiple providers in parallel and compare responses.

    Requires the daemon to be running: llm daemon start

    Examples:

      llm compare "what is RLHF"

      llm compare -p claude -p chatgpt "explain transformers"

      echo "write a haiku" | llm compare

      llm compare "review this code" -f main.py
    """
    q = _resolve_query(query)
    q = _inject_files(q, files or [])

    provider_names = provider or [p.meta.name for p in list_providers()]

    # Validate all names up front
    for name in provider_names:
        try:
            get_provider(name)
        except ValueError as exc:
            err.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

    asyncio.run(
        _compare(
            provider_names=provider_names,
            query=q,
            force_dom=dom,
            raw=raw,
        )
    )


async def _compare(
    *,
    provider_names: list[str],
    query: str,
    force_dom: bool,
    raw: bool,
) -> None:
    if not await client.is_server_running():
        err.print(
            "[red]Error:[/red] daemon is not running.\n"
            "Start it with: [bold]llm daemon start[/bold]"
        )
        raise typer.Exit(1)

    q_preview = query[:72] + "…" if len(query) > 72 else query
    names = ", ".join(f"[cyan]{n}[/cyan]" for n in provider_names)
    console.print(f"\n[dim]Query:[/dim] [bold]{q_preview}[/bold]")
    console.print(f"[dim]Providers ({len(provider_names)}):[/dim] {names}  [dim]via daemon[/dim]\n")

    # Buffer text and timing per provider; render panel when each finishes.
    buffers: dict[str, str] = {name: "" for name in provider_names}
    completed = 0
    total = len(provider_names)

    async for pname, chunk, elapsed_ms, error in client.compare(
        provider_names, query, force_dom=force_dom
    ):
        if pname == "" and chunk is None and elapsed_ms is None and error is None:
            # all_done sentinel
            break

        if chunk is not None:
            buffers[pname] = buffers.get(pname, "") + chunk
        elif elapsed_ms is not None:
            # provider finished successfully
            completed += 1
            text = buffers.get(pname, "")
            subtitle = f"[dim]{elapsed_ms / 1000:.1f}s  ·  {completed}/{total}[/dim]"
            title = f"[bold cyan]{pname}[/bold cyan]  {subtitle}"
            content = text if raw else Markdown(text)
            console.print(Panel(content, title=title, border_style="cyan"))
            console.print()
        elif error is not None:
            completed += 1
            subtitle = f"[dim]{completed}/{total}[/dim]"
            title = f"[bold cyan]{pname}[/bold cyan]  {subtitle}"
            console.print(Panel(f"[red]{error}[/red]", title=title, border_style="red"))
            console.print()


# ──────────────────────────────────────────────────────────────────────
# llm history  /  llm show <id>
# ──────────────────────────────────────────────────────────────────────

@app.command("history")
def history_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of chats to show"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Filter by provider"
    ),
) -> None:
    """Show recent chat history."""
    chats = get_chats(limit=limit, provider=provider)
    if not chats:
        console.print("[dim]No chats saved yet.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("ID", style="dim", width=5)
    table.add_column("Provider", style="cyan", width=10)
    table.add_column("When", style="dim", width=19)
    table.add_column("Query", no_wrap=False)

    for c in chats:
        preview = c.query.replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:79] + "…"
        table.add_row(str(c.id), c.provider, c.created_at, preview)

    console.print()
    console.print(table)
    console.print(f"\n[dim]Use [bold]llm show <id>[/bold] to read a full response.[/dim]\n")


@app.command("show")
def show_cmd(
    chat_id: int = typer.Argument(..., help="Chat ID from llm history"),
    raw: bool = typer.Option(False, "--raw", help="Print plain text instead of Markdown"),
) -> None:
    """Display the full query and response for a saved chat."""
    chat = get_chat(chat_id)
    if not chat:
        err.print(f"[red]Error:[/red] no chat with id {chat_id}")
        raise typer.Exit(1)

    dur = f"{chat.duration_ms / 1000:.1f}s" if chat.duration_ms else "—"
    console.print(
        f"\n[dim]#{chat.id}  {chat.provider}  {chat.created_at}  {dur}[/dim]\n"
    )
    console.print(Panel(chat.query, title="Query", border_style="dim"))
    console.print()
    content = chat.response if raw else Markdown(chat.response)
    console.print(Panel(content, title="Response", border_style="cyan"))
    console.print()


# ──────────────────────────────────────────────────────────────────────
# llm serve  (foreground daemon)
# ──────────────────────────────────────────────────────────────────────

@app.command("serve")
def serve_cmd(
    headless: bool = typer.Option(False, "--headless", help="Run browser headlessly"),
    slow: bool = typer.Option(False, "--slow", help="Add 50ms delay between actions"),
) -> None:
    """Start the browser daemon in the foreground. Press Ctrl-C to stop."""
    from llm_browser.server import LLMServer

    server = LLMServer(headless=headless, slow_mo=50 if slow else 0)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        def _shutdown() -> None:
            stopped.set()

        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT, _shutdown)

        serve_task = asyncio.create_task(server.start())
        await stopped.wait()
        serve_task.cancel()
        await server.stop()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass


# ──────────────────────────────────────────────────────────────────────
# llm daemon start | stop | status
# ──────────────────────────────────────────────────────────────────────

@daemon_app.command("start")
def daemon_start(
    headless: bool = typer.Option(False, "--headless"),
    slow: bool = typer.Option(False, "--slow"),
) -> None:
    """Start the browser daemon in the background."""
    if asyncio.run(client.is_server_running()):
        pid = PID_PATH.read_text().strip() if PID_PATH.exists() else "?"
        console.print(f"[yellow]Daemon already running[/yellow] (PID {pid})")
        return

    args = [sys.executable, "-m", "llm_browser.server"]
    if headless:
        args.append("--headless")
    if slow:
        args.append("--slow")

    log_file = SERVER_LOG.open("w")
    proc = subprocess.Popen(
        args,
        start_new_session=True,
        stdout=log_file,
        stderr=log_file,
    )

    # Wait briefly for the socket to appear
    for _ in range(20):
        time.sleep(0.2)
        if SOCKET_PATH.exists():
            break

    if SOCKET_PATH.exists():
        console.print(f"[green]Daemon started[/green] (PID {proc.pid})")
        console.print(f"[dim]Log: {SERVER_LOG}[/dim]")
    else:
        console.print(f"[red]Daemon failed to start.[/red] Check log: {SERVER_LOG}")
        raise typer.Exit(1)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the background daemon."""
    if not PID_PATH.exists():
        console.print("[dim]Daemon is not running (no PID file).[/dim]")
        return

    pid_str = PID_PATH.read_text().strip()
    try:
        pid = int(pid_str)
        os.kill(pid, signal.SIGTERM)
        # Wait for socket to disappear
        for _ in range(20):
            time.sleep(0.2)
            if not SOCKET_PATH.exists():
                break
        console.print(f"[green]Daemon stopped[/green] (PID {pid})")
    except ProcessLookupError:
        console.print("[dim]Daemon process not found — cleaning up stale PID file.[/dim]")
        PID_PATH.unlink(missing_ok=True)
        SOCKET_PATH.unlink(missing_ok=True)
    except ValueError:
        console.print(f"[red]Error:[/red] invalid PID file contents: {pid_str!r}")
        raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status() -> None:
    """Check whether the daemon is running."""
    running = asyncio.run(client.is_server_running())
    if running:
        pid = PID_PATH.read_text().strip() if PID_PATH.exists() else "?"
        console.print(f"[green]running[/green] (PID {pid})")
    else:
        console.print("[dim]not running[/dim]")


# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app()


if __name__ == "__main__":
    main()
