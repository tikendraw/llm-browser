"""
DOM Inspector — maintenance utility for diagnosing provider selector drift.

Run against a full DOM snapshot saved from the browser to identify which CSS
selectors match response containers, text bodies, completion signals, and
streaming indicators.  Use the output to update provider selectors when a
provider changes its UI.

Usage:
    # inspect with automatic sibling answer file discovery
    llm-inspect context_files/claude/full_dom.html

    # specify provider hint and answer file explicitly
    llm-inspect context_files/chatgpt/full_dom.html \\
        --provider chatgpt \\
        --answer context_files/chatgpt/actual_answer.md
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
from playwright.async_api import async_playwright
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich import box

app = typer.Typer(help="Inspect a saved DOM snapshot to identify provider selectors.")
console = Console()

# ---------------------------------------------------------------------------
# Candidate selector batteries
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, list[str]] = {
    "Response container": [
        '[data-message-author-role="assistant"]',
        "message-content",
        "model-response",
        '[role="article"]',
        "[class*='message']",
        "[class*='response']",
        "[class*='claude-response']",
    ],
    "Text body": [
        # Claude (currently used)
        ".font-claude-response .standard-markdown",
        ".font-claude-response",
        # ChatGPT (currently used)
        '[data-message-author-role="assistant"] .markdown.prose',
        '[data-message-author-role="assistant"] .markdown',
        # Gemini (currently used)
        "message-content .markdown.markdown-main-panel",
        "message-content .markdown",
        # Generic fallbacks
        ".markdown.markdown-main-panel",
        ".markdown.prose",
        ".markdown",
        ".prose",
        "[class*='standard-markdown']",
        "[class*='markdown']",
        "[class*='prose']",
    ],
    "Completion signal": [
        # Claude
        '[data-testid="action-bar-copy"]',
        # ChatGPT
        '[data-testid="copy-turn-action-button"]',
        '[data-testid="good-response-turn-action-button"]',
        # Gemini
        ".response-footer.complete",
        ".response-footer",
        # Generic
        '[aria-label="Copy"]',
        '[aria-label*="Copy"]',
        '[aria-label="Good response"]',
        '[aria-label*="Good response"]',
        '[aria-label*="Share"]',
        '[aria-label*="Regenerate"]',
    ],
    "Streaming indicator": [
        '[data-is-streaming="true"]',
        '[data-is-streaming]',
        '[aria-busy="true"]',
        '[aria-busy]',
        'button[aria-label*="Stop"]',
        '[data-testid*="stop"]',
        "[class*='streaming']",
        "[class*='generating']",
        "[class*='loading']",
        "[class*='thinking']",
    ],
    "Input / composer": [
        "#prompt-textarea",
        "rich-textarea",
        'div[contenteditable="true"]',
        'textarea[placeholder]',
        '[role="textbox"]',
        "[class*='composer']",
        "[class*='input']",
    ],
    "Thinking / search pane": [
        "[class*='thinking']",
        "[class*='search']",
        "[class*='tool-use']",
        '[data-testid*="thinking"]',
        '[aria-label*="thinking"]',
        '[aria-label*="Searching"]',
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _overlap_ratio(a: str, b: str) -> float:
    """Fraction of words in `a` that appear in `b`."""
    words_a = set(_normalise(a).lower().split())
    words_b = set(_normalise(b).lower().split())
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)


# ---------------------------------------------------------------------------
# Core inspector
# ---------------------------------------------------------------------------

async def _inspect(html_path: Path, answer_text: str, provider_hint: str) -> None:
    html = html_path.read_text(encoding="utf-8", errors="replace")

    # Write to a real file so Playwright loads it as file:// — inline scripts run
    # and relative resource paths resolve, unlike set_content() which strips them.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(html)
        tmp.close()
        await _run_inspection(Path(tmp.name), html_path, answer_text, provider_hint)
    finally:
        os.unlink(tmp.name)


async def _run_inspection(
    tmp_path: Path, html_path: Path, answer_text: str, provider_hint: str
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"file://{tmp_path}", wait_until="domcontentloaded")
        # Let inline scripts (React hydration, Angular bootstrap, Lit components) run
        await page.wait_for_timeout(3_000)

        console.print(
            Panel(
                f"[bold]File:[/bold] {html_path}\n"
                f"[bold]Provider hint:[/bold] {provider_hint or 'none'}\n"
                f"[bold]Reference answer:[/bold] {'yes (' + str(len(answer_text)) + ' chars)' if answer_text else 'none'}",
                title="[bold cyan]DOM Inspector[/bold cyan]",
                box=box.ROUNDED,
            )
        )

        for category, selectors in CATEGORIES.items():
            table = Table(
                title=f"[bold yellow]{category}[/bold yellow]",
                box=box.SIMPLE,
                show_header=True,
                header_style="bold",
            )
            table.add_column("Selector", style="cyan", no_wrap=True)
            table.add_column("Count", justify="right")
            table.add_column("First text (80 chars)", style="dim")
            if answer_text:
                table.add_column("Answer match %", justify="right")

            any_match = False
            for sel in selectors:
                try:
                    elements = await page.query_selector_all(sel)
                except Exception:
                    elements = []

                count = len(elements)
                if count == 0:
                    row = [escape(sel), "[dim]0[/dim]", ""]
                    if answer_text:
                        row.append("")
                    table.add_row(*row)
                    continue

                any_match = True
                first_text = ""
                try:
                    first_text = _normalise(await elements[0].inner_text())[:80]
                except Exception:
                    pass

                match_pct = ""
                if answer_text and first_text:
                    ratio = _overlap_ratio(first_text, answer_text)
                    colour = "green" if ratio > 0.5 else ("yellow" if ratio > 0.2 else "red")
                    match_pct = f"[{colour}]{ratio:.0%}[/{colour}]"

                count_str = f"[green]{count}[/green]"
                row = [escape(sel), count_str, escape(first_text)]
                if answer_text:
                    row.append(match_pct)
                table.add_row(*row)

            console.print(table)

        # ------------------------------------------------------------------
        # Summary: best candidate per key category
        # ------------------------------------------------------------------
        console.rule("[bold]Summary — recommended selectors[/bold]")
        key_cats = ["Response container", "Text body", "Completion signal", "Streaming indicator"]
        for cat in key_cats:
            best_sel = None
            best_ratio = -1.0
            best_count = 0

            for sel in CATEGORIES[cat]:
                try:
                    els = await page.query_selector_all(sel)
                except Exception:
                    els = []
                if not els:
                    continue
                try:
                    txt = _normalise(await els[-1].inner_text())
                except Exception:
                    txt = ""
                ratio = _overlap_ratio(txt, answer_text) if answer_text else (1.0 if els else 0.0)
                if ratio > best_ratio or (ratio == best_ratio and len(els) > best_count):
                    best_ratio = ratio
                    best_sel = sel
                    best_count = len(els)

            if best_sel:
                console.print(f"  [cyan]{cat}[/cyan]: [bold]{escape(best_sel)}[/bold]  ([green]{best_count} match(es)[/green])")
            else:
                console.print(f"  [cyan]{cat}[/cyan]: [red]no match found[/red]")

        await browser.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _load_answer(html_path: Path, answer_path: Optional[Path]) -> str:
    if answer_path and answer_path.exists():
        return answer_path.read_text(encoding="utf-8", errors="replace")
    # Auto-discover sibling actual_answer.md
    sibling = html_path.parent / "actual_answer.md"
    if sibling.exists():
        return sibling.read_text(encoding="utf-8", errors="replace")
    return ""


@app.command()
def main(
    html_file: Path = typer.Argument(..., help="Path to the saved full DOM HTML file"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider hint (claude/chatgpt/gemini)"),
    answer: Optional[Path] = typer.Option(None, "--answer", "-a", help="Path to actual_answer.md for text-match scoring"),
) -> None:
    """Inspect a saved DOM snapshot and identify provider selectors."""
    if not html_file.exists():
        console.print(f"[red]File not found:[/red] {html_file}")
        raise typer.Exit(1)

    answer_text = _load_answer(html_file, answer)
    provider_hint = provider or html_file.parent.name

    asyncio.run(_inspect(html_file, answer_text, provider_hint))


if __name__ == "__main__":
    app()
