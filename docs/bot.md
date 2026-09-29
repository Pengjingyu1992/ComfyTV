**English** | [简体中文](bot.zh.md)

# ComfyTV Bot

> A chat agent docked beside your canvas: describe what you want, and it builds nodes, runs workflows, waits for renders, looks at the results and iterates — powered by your locally installed agent CLI, with no API keys stored anywhere.

## What it is

The **ComfyTV Bot** is a panel docked to the right of the canvas — open it with the agent button at the top right of the workflow tabs. The panel is the one the ComfyUI frontend ships for its cloud agent, vendored into ComfyTV (see `src/agent/native/UPSTREAM`) and wired to ComfyTV's own local agent backend. Every message you send spawns an agent turn that can use the full [ComfyTV MCP toolset](mcp.md) — and *only* that toolset: it can read and edit your canvas, run stages, inspect images, and manage your library, but it has no shell, no file system access, and no other tools.

Typical asks:

- *"Add an image stage with Z-Image Turbo, prompt a neon cat at night, 16:9, and run it."*
- *"Use that image as the reference for a 5-second image-to-video, wait for it, and QC the first frame."*
- *"Here's my song and its timed lyrics — trim it into sections and build an audio-driven MV section by section."*
- *"Look at my canvas and tell me why the video stage failed."*
- *"Open the Director timeline and re-take clip 3 with a slower camera."*
- *"I just linked a new workflow — bind seed, width and height for me."*

## No API keys, by design

The bot does not talk to any cloud model API directly and ComfyTV never stores a key. Instead it drives an **agent CLI already installed on your machine** using that CLI's own login — or, with the Local LLM and ComfyUI LLM providers, a **model running on your own hardware**. Six providers ship today:

| Provider | Install | Sign in | Attachments |
| --- | --- | --- | --- |
| [Claude Code](https://claude.com/claude-code) | `npm install -g @anthropic-ai/claude-code` | run `claude`, log in once | images / video / audio |
| [Codex](https://developers.openai.com/codex) | `npm install -g @openai/codex` | `codex login` | images / video / audio |
| [Qwen Code](https://qwenlm.github.io/qwen-code-docs/) | official install script (see its docs) | run `qwen`, then `/auth` | not yet |
| DeepSeek Harness | the DeepSeek Harness desktop app | sign in inside the app | images, on vision models |
| Local LLM | any OpenAI-compatible local server | none — set the endpoint URL in Settings | not yet |
| ComfyUI LLM | a Qwen3- or Gemma-family checkpoint in `models/text_encoders` | none | not yet |

Prerequisites:

1. Install at least one agent CLI and sign in once — or run a local model server and set its URL in Settings.
2. In ComfyTV **Settings → Agent & MCP**, enable **MCP server** and then **ComfyTV Bot** (the bot requires MCP — it's how the agent reaches your canvas).

The bar above the chat picks the engine (and, per engine, the model) for new chats; a chat keeps the engine it started with, so switching the engine starts a new chat. A red dot on the engine chip means it is not installed or not signed in — hover it for the reason.

Provider isolation is per-engine: Claude Code runs with a strict per-turn MCP config and a tool whitelist; Codex runs `codex exec` sandboxed to the bot's working directory with shell and web search disabled, every MCP server except ComfyTV's turned off, and its localhost canvas-tool approvals routed through Codex's automatic reviewer (headless runs cannot prompt); Qwen Code runs against a project-scoped `.qwen/settings.json` inside the bot's working directory (ComfyTV MCP server only, built-in shell/file tools excluded); DeepSeek Harness runs through the ACP runtime bundled in its desktop app with a dedicated profile whose every built-in tool plugin is disabled and whose approval policy never prompts — your global CLI configuration and the app's own settings are never touched.

## Local LLM provider

The Local LLM provider needs no agent CLI at all: ComfyTV runs the agent loop itself against any OpenAI-compatible endpoint — LM Studio, llama.cpp's `llama-server`, vLLM, Ollama and friends. Point **Settings → Agent & MCP → Local LLM endpoint** at the server's base URL (e.g. `http://127.0.0.1:1234/v1`); the model dropdown suggestions come straight from the endpoint's `/models` list. Keyless local endpoints only — consistent with the no-stored-keys rule (a `COMFYTV_LOCAL_LLM_API_KEY` environment variable is honoured for LAN servers that insist on a token, but nothing is ever stored).

Details worth knowing:

- Conversations replay from ComfyTV's own transcript (the endpoint holds no session), so history survives server restarts.
- The bot exposes the core canvas toolset (build / run / wait / look) rather than all tools — small local models drown in the full catalog.
- `wait_stage` is looped provider-side, so the model is only consulted when the render actually finishes.
- If the [LM Studio](https://lmstudio.ai) `lms` CLI is present, the driver model is unloaded from VRAM while a render runs and reloaded afterwards — on a single-GPU machine the canvas gets the whole card during generation.

## ComfyUI LLM provider

The ComfyUI LLM provider goes one step further: no external server either — inference runs **inside ComfyUI itself**, on the same text-encoder stack the core `TextGenerate` node uses. Drop a generation-capable Qwen3- or Gemma-family checkpoint (e.g. Qwen3 8B, or the Gemma 3/4 encoders LTX2 already uses) into `models/text_encoders` and the provider appears; pick the checkpoint under **Settings → Agent & MCP → ComfyUI LLM model** (blank = first found). Qwen3 has native tool-calling training and is the best driver; Gemma follows the same convention by instruction.

Compared to the Local LLM provider:

- Nothing to install or configure — no endpoint URL, no server process.
- VRAM is arbitrated by ComfyUI's model management: while a render runs, the LLM is offloaded automatically and reloaded on the next turn. No `lms`-style juggling.
- Tool calls use the Hermes convention Qwen3 was trained on (`<tool_call>` blocks), rendered and parsed by ComfyTV.
- Turns are served one at a time from an OpenAI-compatible shim at `/comfytv/llm/v1` (non-streaming) — other local apps on your machine may point at it too while the bot is enabled.

## DeepSeek Harness provider

The DeepSeek Harness provider drives the **DeepSeek Harness desktop app** you already installed, through the ACP runtime bundled inside it. ComfyTV never talks to the app's private IPC and never needs a second Harness install: it launches the bundled runtime as a child process, one per turn, and reuses the app's own login. Finder path is discovered automatically (`/Applications/DeepSeek Harness.app`, then `~/Applications`); override it under **Settings → Agent & MCP → DeepSeek Harness app**.

Setup:

1. Install and open the DeepSeek Harness desktop app, and sign in there.
2. In ComfyTV **Settings → Agent & MCP**, choose **DeepSeek Harness sign-in**:
   - **Desktop account** (the default) reuses the account you signed into the app.
   - **API key** uses the `DEEPSEEK_API_KEY` credential the Harness app already stores.

Those two routes are billed to different payers, so the provider never switches between them on its own: if the desktop-account route has no model available you get an error telling you to sign in or change the setting, instead of a silent move to the API key. The model menu names the route next to each entry.

Details worth knowing:

- **Sessions**: each chat gets its own Harness session in the app's store and is resumed per turn, so context survives a ComfyTV restart. Branching a chat is refused for this provider — ACP cannot fork a session, so a branch would otherwise share one with its source.
- **Tool isolation**: every built-in tool plugin (shell, files, web, sub-agents, workflow runner, todos, goals) is disabled for bot turns, and the profile's approval policy never prompts the desktop UI. The agent's only reachable tools are the ComfyTV MCP server's, so workflow runs follow ComfyTV's run-permission setting.
- **Model**: leaving the model blank picks the first model on the configured sign-in route, read from the runtime's own catalog. Model values are opaque strings the runtime issues; ComfyTV stores and replays them exactly, and never invents one.
- **Attachments**: images are sent only when the runtime reports that the selected model accepts image input; otherwise the message is refused before it is sent rather than dropping the image silently. Note that the DeepSeek models this provider can currently reach report **no** image support, so attaching an image to a DeepSeek Harness chat will be refused with an explanatory error — use a provider that accepts images, or the asset library and canvas tools. ACP does not carry video or audio at all.
- **History**: deleting a ComfyTV chat does not delete its upstream Harness session (ACP has no session deletion), so those sessions accumulate in the Harness app's own session store. They are inert; removing them is a Harness-side housekeeping task.
- **Files**: bot working directories live under ComfyUI's user directory in `comfytv/bot-home-deepseek-harness/chats/<chat id>`; the `comfytv-acp` profile is created under your Harness home (`~/.dsh/profiles`) on first use. ComfyTV does not copy or store desktop credentials.

## Using the panel

- **Canvas**: the bot always works on the tab on screen; switch tabs and it follows.
- **Conversations** are persistent: the history screen (clock icon) lists, renames and removes chats; each chat remembers its full context across turns (the CLI resumes the same session).
- **Streaming**: replies stream in live; tool activity shows as an activity trace per turn (e.g. `add_stage`, `wait_stage`) that folds into a summary when the turn ends — nodes appear and run on your canvas as it goes.
- **Mentions**: type **`@`** to reference a node (a stage) on the canvas, or select nodes with the picker to attach them to the message.
- **Attachments**: on providers that support them, attach images / video / audio from ComfyTV's asset library via the **+** button, by dragging asset cards from the Assets tab, or by dropping / pasting files (they are imported into the library first, so the agent can hand them to stages as `asset_refs`). Videos are summarized with a middle frame, audio with a waveform, so the agent can actually *see* what you sent.
- **Skills**: start a message with **`/<name>`** to run it under an installed [Agent Skill](skills.md); the agent reads that skill first and follows its instructions for the task.
- **Run permissions**: the popover next to the send button switches between asking before the agent runs a workflow and running automatically.
- **Stop** aborts the current turn; partial output is kept.
- Closing the panel does not interrupt a running turn — the turn continues server-side and the transcript catches up when you reopen it.

## How it works, briefly

CLI providers spawn a fresh headless process per turn and resume the chat's session. DeepSeek Harness uses its bundled ACP runtime with a dedicated profile and per-turn tool restrictions. ComfyTV's database keeps a display mirror of the transcript. Canvas writes still follow MCP rules — an open ComfyTV page executes them, whether it is in Comfy Desktop or a browser.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No agent button at the top right | **Enable ComfyTV Bot** is off (Settings → Agent & MCP), which itself requires **Enable MCP server**; the button also needs a ComfyUI frontend that ships the agent panel slot (1.53 or newer) |
| Red dot on the engine chip | That agent CLI is not installed or not signed in — install one from the table above and sign in, then reopen the panel |
| Bot says it can't reach the canvas | No ComfyTV page open (or page websocket dropped after a server restart — hard-refresh) |
| Long renders: bot seems idle | It's inside a blocking `wait_stage` — the tool chip shows it; this is normal and cheap |

## See also

- [Agent access (MCP)](mcp.md) — the toolset the bot uses, and how to connect external agents
- [Agent Skills](skills.md) — instruction packs the bot (and external agents) can invoke
- [Sidebar](sidebar.md) — the Settings panel with both switches
