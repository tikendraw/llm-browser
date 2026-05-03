- [x] there might be changes in dom content/tag, in such case the llm integration will break, create a utility script that given a full dom content, can extract the necessary elements to identify, generation message, end of generation, and other useful element, so that there is less friction in updation when dom changes.(have a standard for a full chat(with thinking, browsing) dom for understanding the whole layout)
  → implemented as `llm_browser/utils/dom_inspector.py`, CLI: `uv run llm-inspect <full_dom.html>`


- [ ] reuse the same chat if queries run within same session time range. (will reduce the bot suspicition)