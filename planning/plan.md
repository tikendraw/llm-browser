- [x] there might be changes in dom content/tag, in such case the llm integration will break, create a utility script that given a full dom content, can extract the necessary elements to identify, generation message, end of generation, and other useful element, so that there is less friction in updation when dom changes.(have a standard for a full chat(with thinking, browsing) dom for understanding the whole layout)
  → implemented as `llm_browser/utils/dom_inspector.py`, CLI: `uv run llm-inspect <full_dom.html>`

- [x] Funtionality to pipe inputs, and attach files.
  → stdin pipe: `echo "..." | llm ask claude -` (was already implemented)
  → file injection: `llm ask claude "summarise" -f file.txt` (repeatable `-f`)

- [x] create a database to have all the chats saved.

- [x] Block images, media, and fonts in browser to reduce unnecessary network traffic — added context-level `route` intercept in `BrowserSession.start()`.

- [x] write a service that keeps the context always open for quick chat rather than opening context on every command.we just post to that service and get the answers. like client server.

- [ ] reuse the same chat if queries run within same session time range. (will reduce the bot suspicition)