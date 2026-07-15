# AI Agent Desktop

A local desktop app (PyQt6) that lets an AI model read, write, and run
commands inside a project folder on your machine — with automatic failover
between models if one is down, rate-limited, or erroring out.

## Choosing which model answers

The "Model:" dropdown in the header lets you force a specific model to go
first for your next message. It still automatically falls back to your
other configured models if that one fails -- "Auto" just picks by priority
order, while selecting a specific model temporarily bumps it to the front.

## Project memory (so you don't have to repeat context)

Replaying the *entire* raw conversation on every message would work, but
gets expensive fast -- a few file writes and you're resending tens of
thousands of tokens just so the model remembers "we already made
calculator.py". Instead, every time a task finishes, the app distills it
into one short line and saves it to `.ai_agent_memory.json` inside your
project folder. That compact list gets folded into the system prompt on
every future request, so the agent has continuity without you re-explaining
and without paying full-transcript prices every turn.

Delete that file any time to reset the agent's memory of a project.

## Confirmations happen in the chat, not a popup

Accept/Decline for file writes and shell commands now show up as buttons
inside the chat log itself (not a separate dialog), so a long script never
gets cut off or hidden behind the window -- it just scrolls like the rest
of the conversation. Toggle **auto-confirm** for writes/commands under
**File → Model settings** if you'd rather skip the prompts entirely (only
recommended for a folder under git version control).

## Theme notes

The app forces Qt's "Fusion" style plus an explicit light QPalette
app-wide (see `ui/theme.py` / `main.py`). This matters because some widgets
(combobox popups, checkboxes, tooltips) pull colors from the OS palette for
parts a stylesheet alone doesn't cover -- on a system in Windows Dark Mode,
that previously caused invisible white-on-white text in dropdowns. Forcing
Fusion + a pinned light palette makes the UI look the same regardless of
the OS theme.

## Setup

```bash
cd ai_agent_desktop
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On first launch you'll be asked to pick a project folder. Then go to
**File → Model settings** to add one or more models.

## Adding a model

Each model needs:
- **Provider type**: `openai_compatible` or `anthropic`
- **Base URL**: the API root
  - OpenAI: `https://api.openai.com/v1`
  - Groq: `https://api.groq.com/openai/v1`
  - Together AI: `https://api.together.xyz/v1`
  - OpenRouter: `https://openrouter.ai/api/v1`
  - Local Ollama: `http://localhost:11434/v1`
  - Anthropic: `https://api.anthropic.com`
- **API key**
- **Model ID**: e.g. `gpt-4.1`, `llama-3.3-70b-versatile`, `claude-sonnet-5`
- **Priority**: lower number = tried first. Add a second, cheaper/different
  model with a higher priority number as your fallback.

Because almost every provider except Anthropic now speaks the "OpenAI
chat completions" protocol, `openai_compatible` covers the large majority
of models you'd want to add — you're not limited to OpenAI itself.

## How automatic failover works (`core/failover.py`)

`ModelRouter.call()` tries your models in priority order. If a model:
- times out,
- can't be reached,
- returns a 429 (rate limit) or 5xx (server error),
- or gives back a malformed response,

...the router logs the failure, puts that model on a 60-second cooldown,
and immediately tries the next one — the agent loop doesn't need to know
this happened. A bad API key (401) or malformed request (4xx) is treated
as non-retryable but the router still moves on to the next model, since
your goal is "keep working," not "figure out why one model is broken."

Once a model succeeds, the router remembers it and prefers it next turn,
so you're not stuck on a fallback forever if your primary comes back.

## How the agentic loop works (`core/agent.py`)

The agent is given five tools: `list_dir`, `read_file`, `write_file`,
`run_command`, and `task_complete`. It's given your instruction, the model
decides what to inspect/edit, results are fed back, and this repeats
(up to `max_agent_iterations`, default 25) until the model calls
`task_complete`.

All file/command operations are sandboxed to the project root
(`core/tools.py` — `ProjectTools._resolve()` rejects any path that
escapes it).

## Safety notes — please read before turning off confirmations

- By default, every file write and every shell command triggers a
  confirmation dialog (`_confirm_write` / `_confirm_command` in
  `ui/main_window.py`). You can flip `auto_confirm_writes` /
  `auto_confirm_commands` to `True` in the config file to skip these,
  but that means the model can modify or run anything in that folder
  without you seeing it first — only do this in a folder you don't
  mind an LLM having full run of (ideally one under git version control).
- **Strongly recommended**: only point this at folders under git, and
  commit before running the agent, so any change is one `git checkout`
  away from being undone.
- API keys are currently stored in plaintext at
  `~/.ai_agent_desktop/config.json`. For anything beyond solo local use,
  swap `core/config.py` to use the `keyring` package instead — see the
  comment at the top of that file.

## Extending it

- **More providers**: add a new `call_xxx()` function in
  `core/providers.py` following the same normalized
  `{"content", "tool_calls", "raw"}` return shape, and register it in
  `PROVIDER_FUNCS`.
- **Diff-based edits instead of full overwrites**: replace `write_file`
  in `core/tools.py` with a patch/diff tool, and show a real diff view in
  the confirmation dialog instead of a plain text preview.
- **Streaming responses**: currently each model call blocks until the
  full response is back. Both OpenAI-compatible and Anthropic APIs
  support SSE streaming if you want token-by-token output.
