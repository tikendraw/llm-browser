# llm-browser

Query Claude, ChatGPT, and Gemini from the terminal using your **free browser accounts** — no API key or subscription needed.

## How it works

- Uses **Playwright** with a **persistent Chromium profile** (your cookies are saved between runs)
- Two extraction modes per provider:
  - **Network interception** — captures the SSE stream directly (fast, accurate)
  - **DOM fallback** — polls the rendered page until the response stabilises (robust)
- Your browser stays open; the tool drives a new tab when needed

---

## Setup

```bash
# 1. Clone / copy the project
cd llm-browser

# 2. Install with uv (recommended — you already use it)
uv venv
uv pip install -e .

# 3. Install the Playwright browser (one-time)
uv run playwright install chromium

# 4. Activate the venv
source .venv/bin/activate
```

---

## First-time login (one per provider)

Your session is saved in `~/.config/llm-browser/browser-profile/`.  
After logging in once, all future `ask` calls reuse it silently.

```bash
llm login claude
llm login chatgpt
llm login gemini
```

---

## Usage

```bash
# Ask a question
llm ask claude "explain async generators in python"
llm ask gpt   "what is RLHF?"
llm ask gemini "summarise the CAP theorem"

# Read query from stdin (great for piping)
echo "write a haiku about linux" | llm ask claude -
cat my_code.py | llm ask claude - "review this code"

# Force DOM mode (if network interception is flaky)
llm ask claude --dom "what is 42?"

# Plain text output (no markdown rendering)
llm ask claude --raw "list 5 python tips"

# See all providers
llm list
```

---

## Provider aliases

| Alias | Provider |
|-------|----------|
| `claude`, `c` | claude.ai |
| `chatgpt`, `gpt`, `openai` | chatgpt.com |
| `gemini`, `g`, `bard` | gemini.google.com |

---

## Adding a new provider

1. Create `llm_browser/providers/myprovider.py` subclassing `BaseProvider`
2. Implement: `is_logged_in`, `navigate_to_chat`, `submit_query`, `network_stream`, `dom_extract`
3. Register it in `llm_browser/providers/__init__.py`

```python
from llm_browser.providers.myprovider import MyProvider

_PROVIDERS = {
    ...
    "myprovider": MyProvider(),
}
```

---

## File layout

```
llm-browser/
├── pyproject.toml
└── llm_browser/
    ├── __init__.py
    ├── cli.py          ← typer CLI (entry point: `llm`)
    ├── browser.py      ← persistent session manager
    ├── config.py       ← paths, timeouts, constants
    └── providers/
        ├── __init__.py ← registry + get_provider()
        ├── base.py     ← abstract BaseProvider
        ├── claude.py
        ├── chatgpt.py
        └── gemini.py
```

---

## Notes

- **Bot detection**: The persistent profile + real Chrome UA makes detection unlikely for personal use, but if a provider blocks you, try `--slow`.
- **DOM selectors**: Sites update their HTML frequently. If a provider breaks, inspect the page and update the selectors in the provider file — they're isolated and easy to change.
- **Headless mode**: `--headless` is available but some providers (especially ChatGPT) actively block headless browsers. Leave it off (the default).
- **Session storage**: `~/.config/llm-browser/browser-profile/` — delete this folder to reset all sessions.