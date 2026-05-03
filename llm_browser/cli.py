"""
CLI entry point for llm-browser.

Usage examples:
  llm ask claude "explain async generators in python"
  llm ask gpt "what is RLHF"
  llm ask gemini "summarise the CAP theorem"
  echo "write a haiku about linux" | llm ask claude -
  llm login claude
  llm list
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

import typer
from llm_browser.browser import browser_session
from llm_browser.providers import get_provider, list_providers
from llm_browser.providers.base import BaseProvider, LimitReachedError
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

app = typer.Typer(
    name="llm",
    help="Query LLMs via your browser — no API key needed.",
    no_args_is_help=True,
)
console = Console()
err = Console(stderr=True)


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
    query: str = typer.Argument(
        ...,
        help='Query text, or "-" to read from stdin',
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

    Pass "-" as QUERY to read from stdin:

      echo "what is 2+2" | llm ask claude -
    """
    if query == "-":
        query = sys.stdin.read().strip()
        if not query:
            err.print("[red]Error:[/red] empty query on stdin")
            raise typer.Exit(1)

    asyncio.run(
        _ask(
            provider_name=provider_name,
            query=query,
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

    console.print()


# ──────────────────────────────────────────────────────────────────────
# llm compare [--provider P]… <query>
# ──────────────────────────────────────────────────────────────────────

@app.command("compare")
def compare_cmd(
    query: str = typer.Argument(
        ...,
        help='Query to send, or "-" to read from stdin',
    ),
    provider: Optional[list[str]] = typer.Option(
        None,
        "--provider",
        "-p",
        help="Provider to include (repeat for multiple). Default: all providers.",
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

      echo "write a haiku" | llm compare -
    """
    if query == "-":
        query = sys.stdin.read().strip()
        if not query:
            err.print("[red]Error:[/red] empty query on stdin")
            raise typer.Exit(1)

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
            query=query,
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

                console.print()


# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app()


if __name__ == "__main__":
    main()