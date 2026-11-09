# Licenses

Debate Hall's own code is **AGPL-3.0-only** (`LICENSE`). Every source file says so in its first lines.

The art in `docs/art/` (the repository picture and its SVG source) is **CC BY-SA 4.0**: see `docs/art/LICENSE`.

`hall/web/qr.js` comes from AI-Beach (`beach/web/qr.js`): same authors, same license.

## Python dependencies (installed by you, not shipped here)

| Package | License | Used for |
|---|---|---|
| fastapi | MIT | the web server's routes |
| starlette (with fastapi) | BSD-3-Clause | |
| pydantic (with fastapi) | MIT | |
| opentelemetry-api (with recent fastapi) | Apache-2.0 | not used by the hall |
| uvicorn | BSD-3-Clause | serving the hall |
| markdown-it-py | MIT | turning the paper into HTML, with raw HTML switched off |
| mdurl (with markdown-it-py) | MIT | |
| httpx | BSD-3-Clause | tests only |

Nothing is loaded from the internet at runtime: the pages use only files from this repository.
