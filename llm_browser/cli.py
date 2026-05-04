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
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from llm_browser.browser import browser_session
from llm_browser.db import get_chat, get_chats, init_db, save_chat
from llm_browser.providers import get_provider, list_providers
from llm_browser.providers.base import BaseProvider, LimitReachedError
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
    headless: bool = typer.Option(
        False,
        "--headless",
        help="Run browser in headless mode (may break some sites)",
    ),
    slow: bool = typer.Option(
        False,
        "--slow",
        help="Add 50ms delay between actions (helps with flaky UIs)",
    ),
) -> None:
    """
    Ask a question and stream the response to your terminal.

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
            headless=headless,
            slow_mo=50 if slow else 0,
        )
    )


async def _ask(
    *,
    provider_name: str,
    query: str,
    force_dom: bool,
    raw: bool,
    headless: bool,
    slow_mo: int,
) -> None:
    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    mode_label = "DOM" if force_dom else "network→DOM"
    console.print(
        f"\n[dim]Provider:[/dim] [cyan]{provider.meta.display_name}[/cyan]  "
        f"[dim]Mode:[/dim] [cyan]{mode_label}[/cyan]\n"
    )

    full_response = ""
    start = time.monotonic()

    try:
        async with browser_session(headless=headless, slow_mo=slow_mo) as session:
            logged_in = await session.ensure_logged_in(provider)
            if not logged_in:
                console.print(
                    f"[yellow]Not logged in to {provider.meta.display_name}.[/yellow]\n"
                    f"Please log in using: [bold]llm login {provider_name}[/bold]\n"
                )
                raise typer.Exit(1)

            if raw:
                # Stream plain text directly to stdout
                async for chunk in session.query(provider, query, force_dom=force_dom):
                    print(chunk, end="", flush=True)
                    full_response += chunk
                print()  # newline at end
            else:
                # Render Markdown live as chunks arrive
                with Live(console=console, refresh_per_second=10, vertical_overflow="visible") as live:
                    async for chunk in session.query(provider, query, force_dom=force_dom):
                        full_response += chunk
                        live.update(Markdown(full_response))
    except LimitReachedError as exc:
        err.print(f"\n[bold red]Limit reached:[/bold red] {exc}\n")
        raise typer.Exit(1) from exc

    if full_response:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        save_chat(provider.meta.name, query, full_response, elapsed_ms)

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
    headless: bool = typer.Option(False, "--headless"),
    slow: bool = typer.Option(False, "--slow"),
) -> None:
    """
    Send the same query to multiple providers in parallel and compare responses.

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
    resolved: list[BaseProvider] = []
    for name in provider_names:
        try:
            resolved.append(get_provider(name))
        except ValueError as exc:
            err.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

    asyncio.run(
        _compare(
            providers=resolved,
            query=q,
            force_dom=dom,
            raw=raw,
            headless=headless,
            slow_mo=50 if slow else 0,
        )
    )


async def _run_one(
    provider: BaseProvider,
    context,  # BrowserContext — typed loosely to avoid circular import
    query: str,
    force_dom: bool,
) -> tuple[str, float, Exception | None]:
    """Query one provider on a dedicated page. Returns (text, elapsed_s, error)."""
    page = await context.new_page()
    start = time.monotonic()
    text = ""
    error: Exception | None = None
    try:
        if not await provider.is_logged_in(page):
            raise RuntimeError(f"Not logged in — run: llm login {provider.meta.name}")
        async for chunk in provider.query(page, query, force_dom=force_dom):
            text += chunk
    except Exception as exc:
        error = exc
    finally:
        await page.close()
    return text, time.monotonic() - start, error


async def _compare(
    *,
    providers: list[BaseProvider],
    query: str,
    force_dom: bool,
    raw: bool,
    headless: bool,
    slow_mo: int,
) -> None:
    q_preview = query[:72] + "…" if len(query) > 72 else query
    names = ", ".join(f"[cyan]{p.meta.name}[/cyan]" for p in providers)
    console.print(f"\n[dim]Query:[/dim] [bold]{q_preview}[/bold]")
    console.print(f"[dim]Providers ({len(providers)}):[/dim] {names}\n")

    async with browser_session(headless=headless, slow_mo=slow_mo) as session:
        # Each provider gets its own page so they run truly in parallel.
        task_to_provider = {
            asyncio.create_task(
                _run_one(p, session.context, query, force_dom)
            ): p
            for p in providers
        }

        pending = set(task_to_provider)
        completed = 0

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                completed += 1
                provider = task_to_provider[task]
                text, elapsed, error = task.result()

                subtitle = f"[dim]{elapsed:.1f}s  ·  {completed}/{len(providers)}[/dim]"
                title = f"[bold cyan]{provider.meta.display_name}[/bold cyan]  {subtitle}"

                if error:
                    console.print(
                        Panel(f"[red]{error}[/red]", title=title, border_style="red")
                    )
                else:
                    content = text if raw else Markdown(text)
                    console.print(Panel(content, title=title, border_style="cyan"))
                    save_chat(provider.meta.name, query, text, int(elapsed * 1000))

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

def main() -> None:
    app()


if __name__ == "__main__":
    main()