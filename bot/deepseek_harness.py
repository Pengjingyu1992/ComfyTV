"""DeepSeek Harness desktop provider, spoken over ACP.

Design:

* The ACP runtime shipped *inside* the installed desktop app is reused
  (``/Applications/DeepSeek Harness.app``); ComfyTV never talks to desktop
  private IPC and never needs a second Harness install.
* One ACP child process per turn.  A measured empty-session round trip costs
  ~0.5s, and a per-turn process gives crash isolation between chats.  Context
  survives across turns through the persisted session, resumed with
  ``session/resume`` — ``dsh-acp`` restores the log without replaying updates.
* Context is carried by the *session id*, never by the chat's ``resume_token``
  semantics alone: branching a chat in ComfyTV copies ``resume_token``, so two
  chats would otherwise drive one upstream session (see ``api/bot.py``, which
  refuses branching for this provider).
* Tools: the ComfyTV MCP server is mounted per session and is the *only* tool
  surface.  The composed profile is narrowed per turn through ``--patch``
  (which is applied after the profile layer *and* after ``$DSH_HOME``'s
  machine-local ``cordis.patch.yml``, so a home patch cannot re-open tools).
* Auth: the desktop account route (``deepseek-account``) is chosen explicitly
  from the model options the runtime returns.  It never silently falls back to
  the API-key route (``deepseek-official``) — the shipped ACP template defaults
  to that route, so falling back would quietly change who pays.
* ComfyTV does not copy or write desktop credentials.  The runtime manages its
  own credential store and may delete an account grant if its issuer does not
  match the configured origin, so nothing here points a separate DSH_HOME at
  the desktop credential file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ._acp import AcpError, AcpProcessError, AcpTransport
from ._cli_common import (
    PROBE_CACHE_S,
    TOOL_RESULT_CAP,
    TURN_IDLE_TIMEOUT_S,
    TURN_MAX_S,
    base_spawn_env,
    kill_process_tree,
)
from .providers import (
    AgentProvider,
    BotEvent,
    EmitFn,
    ProviderCaps,
    ProviderStatus,
    TurnHandle,
    TurnRequest,
    TurnResult,
)

_log = logging.getLogger(__name__)

PROVIDER_ID = "deepseek-harness"

APP_BUNDLE_NAME = "DeepSeek Harness.app"
APP_EXECUTABLE_REL = Path("Contents/MacOS/DeepSeek Harness")
#: The runtime lives *inside* the asar archive.  Plain Python cannot see
#: through `app.asar/...` (``Path.exists()`` is False even though Electron's
#: Node resolves it), so discovery validates the archive file itself and then
#: trusts the documented path within it.
APP_ASAR_REL = Path("Contents/Resources/app.asar")
APP_LAUNCHER_REL = Path(
    "Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js")

#: Profile baked for the bot.  Provisioned from the shipped ``acp`` template.
BOT_PROFILE = "comfytv-acp"
TEMPLATE_PROFILE = "acp"
#: Preset name installed by our overlay; ``approval: never`` means the runtime
#: never raises ``session/request_permission`` for the tools we leave mounted.
BOT_PERMISSION_PRESET = "comfytv-mcp-only"

SETTING_APP_PATH = "bot-deepseek-harness-app-path"
SETTING_AUTH_MODE = "bot-deepseek-harness-auth-mode"
SETTING_MODEL = f"bot-model-{PROVIDER_ID}"

AUTH_DESKTOP = "desktop-account"
AUTH_API_KEY = "api-key"
#: ACP model option route -> ComfyTV auth mode.
ROUTE_FOR_AUTH = {AUTH_DESKTOP: "deepseek-account", AUTH_API_KEY: "deepseek-official"}

ACP_PROTOCOL_VERSION = 1
MODEL_CONFIG_ID = "model"
IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
MCP_SERVER_NAME = "comfytv"

PROFILE_BOOT_TIMEOUT_S = 60.0
VERSION_TIMEOUT_S = 15.0
INITIALIZE_TIMEOUT_S = 30.0
SESSION_TIMEOUT_S = 120.0
CLOSE_TIMEOUT_S = 30.0
#: Grace period after ``session/cancel`` before the process group is killed.
CANCEL_GRACE_S = 8.0
#: A permission request normally follows the tool_call that names the tool; if
#: it arrives first, wait this long for the cache to catch up before failing
#: closed.
PERMISSION_TOOL_WAIT_S = 2.0
_PERMISSION_POLL_S = 0.05

_SAFE_SCALAR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")

#: Loader entry ids disabled for every bot turn.  Ids come from
#: ``dsh --profile acp --dump-config`` on the installed runtime.  Everything
#: here is either a built-in tool entry point or a plugin that only serves one,
#: so the agent's only reachable tools are the ComfyTV MCP server's.
_DISABLED_ENTRY_IDS: tuple[str, ...] = (
    # shell / filesystem / background jobs
    "tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search",
    # skills: ComfyTV skills are read through the mounted MCP server instead
    "tool-skill", "skill", "skill-filesystem", "skill-badge",
    # sub-agents
    "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-subagent-list-agents",
    "subagent", "subagent-spawn-in-process", "subagent-fork-in-process",
    # workflow execution engine behind tool-workflow
    "tool-workflow", "workflow-ptc", "ptc-runtime",
    # todo / goals
    "tool-todo", "tool-goal", "tool-ralph",
    # web
    "tool-web", "web-search-deepseek",
    # plugin management
    "tool-plugin-manager", "plugin-manager",
    # interactive questions need elicitation, which ACP does not expose
    "user-questions",
    # plan mode is not reachable over ACP (the protocol does not expose modes),
    # but the plugin still registers an `exit_plan_mode` tool, so it must go too
    "plan-mode",
    # repository instruction injection
    "agent-instructions",
)


# --------------------------------------------------------------------- settings


def _setting(key: str, default: Any = "") -> Any:
    """Read a ComfyTV setting without importing storage at module import time."""
    try:
        from .. import storage
        value = storage.get_setting(key)
    except Exception:
        return default
    return default if value is None else value


def _auth_mode() -> str:
    value = str(_setting(SETTING_AUTH_MODE, AUTH_DESKTOP) or "").strip()
    return value if value in ROUTE_FOR_AUTH else AUTH_DESKTOP


def _saved_model_value() -> str:
    return str(_setting(SETTING_MODEL, "") or "").strip()


# --------------------------------------------------------------- app discovery


def _app_root(path: Path) -> Optional[Path]:
    """Walk up from *path* to the enclosing ``*.app`` bundle, if there is one."""
    candidate = path
    for _ in range(8):
        if candidate.suffix == ".app" and candidate.is_dir():
            return candidate
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def app_candidates() -> list[Path]:
    out: list[Path] = []
    override = str(_setting(SETTING_APP_PATH, "") or "").strip()
    if override:
        out.append(Path(override).expanduser())
    out.append(Path("/Applications") / APP_BUNDLE_NAME)
    out.append(Path.home() / "Applications" / APP_BUNDLE_NAME)
    return out


def resolve_launcher() -> tuple[Optional[list[str]], str]:
    """Return ``([electron, launcher.js], "")`` or ``(None, reason)``."""
    problem = ""
    for candidate in app_candidates():
        if not candidate.exists():
            continue
        app = _app_root(candidate)
        if app is None:
            problem = f"{candidate} is not inside a .app bundle"
            continue
        executable = app / APP_EXECUTABLE_REL
        asar = app / APP_ASAR_REL
        if not executable.is_file():
            problem = f"{app} has no executable at {APP_EXECUTABLE_REL}"
            continue
        if not asar.is_file():
            problem = f"{app} does not bundle a DeepSeek Harness runtime"
            continue
        return [str(executable), str(app / APP_LAUNCHER_REL)], ""
    found = shutil.which("dsh")
    if found:
        return [found], ""
    if problem:
        return None, problem
    return None, "DeepSeek Harness desktop app was not found"


def spawn_env() -> dict:
    env = base_spawn_env()
    # Only meaningful to the Electron binary; lets it run as plain Node so the
    # bundled launcher can be executed without opening a window.
    env["ELECTRON_RUN_AS_NODE"] = "1"
    return env


def dsh_home() -> Path:
    value = os.environ.get("DSH_HOME")
    return Path(value).expanduser() if value else (Path.home() / ".dsh")


def profile_dir(profile: str = BOT_PROFILE) -> Path:
    return dsh_home() / "profiles" / profile


def bot_home() -> Path:
    try:
        import folder_paths
        user = Path(folder_paths.get_user_directory())
    except Exception:
        user = Path.home()
    path = user / "comfytv" / "bot-home-deepseek-harness"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chat_cwd(chat_id: str) -> Path:
    """Stable per-chat working directory.

    ``dsh-acp`` verifies the canonical workspace on resume and refuses a
    mismatch, so new and resume must use the same path.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")[:64] or "default"
    path = bot_home() / "chats" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------- overlays


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _SAFE_SCALAR.fullmatch(text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _emit_entry(entry: dict) -> list[str]:
    lines = [f"- id: {_yaml_scalar(entry['id'])}"]
    if entry.get("disabled") is not None:
        lines.append(f"  disabled: {_yaml_scalar(bool(entry['disabled']))}")
    config = entry.get("config") or {}
    if config:
        lines.append("  config:")
        for key, value in config.items():
            if isinstance(value, dict):
                lines.append(f"    {_yaml_scalar(key)}:")
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict):
                        lines.append(f"      {_yaml_scalar(sub_key)}:")
                        for leaf_key, leaf_value in sub_value.items():
                            lines.append(
                                f"        {_yaml_scalar(leaf_key)}: "
                                f"{_yaml_scalar(leaf_value)}")
                    else:
                        lines.append(
                            f"      {_yaml_scalar(sub_key)}: "
                            f"{_yaml_scalar(sub_value)}")
            else:
                lines.append(f"    {_yaml_scalar(key)}: {_yaml_scalar(value)}")
    return lines


def overlay_entries(route: Optional[tuple[str, str]] = None) -> list[dict]:
    """Loader patch entries for one bot turn.

    * every built-in tool entry point disabled, so only the per-session ComfyTV
      MCP server can be called;
    * an approval policy of ``never``, so the agent does not raise
      ``session/request_permission`` for the tools that do remain;
    * optionally the provider/model, so ``initialize`` advertises capabilities
      for the model actually in use (the runtime computes them once, from the
      static profile config).
    """
    entries: list[dict] = [
        {
            "id": "permission",
            "config": {
                "presets": {
                    BOT_PERMISSION_PRESET: {
                        "sandbox": "danger-full-access",
                        "approval": "never",
                    },
                },
                "defaultPreset": BOT_PERMISSION_PRESET,
            },
        },
    ]
    entries.extend({"id": entry_id, "disabled": True}
                   for entry_id in _DISABLED_ENTRY_IDS)
    if route is not None:
        provider, model = route
        entries.append({"id": "acp", "config": {"provider": provider,
                                                "model": model}})
    return entries


def render_overlay(route: Optional[tuple[str, str]] = None) -> str:
    header = [
        "# Generated by ComfyTV for the DeepSeek Harness bot provider.",
        "# Applied as a --patch overlay, i.e. after every profile layer.",
        "# Do not edit by hand; it is rewritten on each turn.",
    ]
    lines = list(header)
    for entry in overlay_entries(route):
        lines.extend(_emit_entry(entry))
    return "\n".join(lines) + "\n"


def write_profile_patch(*, force: bool = True) -> None:
    """Keep the profile self-describing for ``--dump-config`` inspection.

    The per-turn ``--patch`` overlay is what actually takes effect; this file
    mirrors the same policy so a human who boots the profile directly sees the
    intended tool scope.  With ``force=False`` it is only rewritten when it
    drifted, so the file cannot misrepresent the live policy after an upgrade.
    """
    target = profile_dir() / "cordis.patch.yml"
    expected = render_overlay(None)
    if not force:
        try:
            if target.read_text(encoding="utf-8") == expected:
                return
        except OSError:
            pass
    target.write_text(expected, encoding="utf-8")


def write_turn_overlay(cwd: Path, route: Optional[tuple[str, str]]) -> Path:
    target = cwd / "comfytv-turn-overlay.yml"
    target.write_text(render_overlay(route), encoding="utf-8")
    return target


async def ensure_profile(launcher: list[str]) -> str:
    """Create the bot profile from the shipped template when missing.

    Returns ``""`` on success or a human-readable failure reason.  Uses
    ``--dump-config`` so provisioning composes the tree and exits instead of
    booting a long-running app.
    """
    if (profile_dir() / "package.json").is_file():
        # Keep the inspection copy of the policy in step with this build
        # (it only writes when the content actually differs).
        try:
            write_profile_patch(force=False)
        except OSError as e:
            _log.warning("[ComfyTV/deepseek-harness] profile patch not "
                         "refreshed: %s", e)
        return ""
    argv = list(launcher) + [
        "--profile", BOT_PROFILE,
        "--from-default-profile", TEMPLATE_PROFILE,
        "--dump-config",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env(),
        )
        _, err = await asyncio.wait_for(proc.communicate(),
                                        timeout=PROFILE_BOOT_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as e:
        return f"could not create the {BOT_PROFILE} profile: {e}"
    if not (profile_dir() / "package.json").is_file():
        detail = (err or b"").decode("utf-8", "replace").strip()
        return (f"could not create the {BOT_PROFILE} profile"
                + (f": {detail[-400:]}" if detail else ""))
    try:
        write_profile_patch()
    except OSError as e:
        _log.warning("[ComfyTV/deepseek-harness] profile patch not written: %s", e)
    return ""


# ---------------------------------------------------------------- model values


def parse_model_value(value: str) -> Optional[tuple[str, str]]:
    """Decode an ACP model option value into ``(provider, model)``.

    The value is opaque to clients: it is only ever echoed back verbatim.  We
    do have to *read* the two routing fields, because the startup ``--patch``
    overlay has to name them so the runtime advertises capabilities for the
    right model.  Reading is not constructing — every value we act on comes
    from the runtime's own option list.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if (isinstance(parsed, list) and len(parsed) == 2
            and all(isinstance(part, str) and part for part in parsed)):
        return parsed[0], parsed[1]
    return None


class _ModelOption:
    __slots__ = ("value", "label", "group")

    def __init__(self, value: str, label: str, group: str = "") -> None:
        self.value = value
        self.label = label
        self.group = group


def _model_options_from(config_options: Any) -> list[_ModelOption]:
    """Flatten the runtime's ``model`` config option into value/label rows."""
    if not isinstance(config_options, list):
        return []
    entry = None
    for option in config_options:
        if not isinstance(option, dict):
            continue
        if option.get("id") == MODEL_CONFIG_ID or option.get("category") == "model":
            entry = option
            break
    if not isinstance(entry, dict):
        return []
    out: list[_ModelOption] = []
    for item in entry.get("options") or []:
        if not isinstance(item, dict):
            continue
        if "group" in item:
            group = str(item.get("group") or "")
            for nested in item.get("options") or []:
                if isinstance(nested, dict) and nested.get("value"):
                    out.append(_ModelOption(str(nested["value"]),
                                            str(nested.get("name") or nested["value"]),
                                            group))
        elif item.get("value"):
            out.append(_ModelOption(str(item["value"]),
                                    str(item.get("name") or item["value"])))
    return out


def _usage_from_acp(usage: Any) -> Optional[dict]:
    """Map ACP usage counters; never invent billing numbers."""
    if not isinstance(usage, dict):
        return None
    mapping = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "totalTokens": "total_tokens",
        "thoughtTokens": "thought_tokens",
        "cachedReadTokens": "cache_read_input_tokens",
        "cachedWriteTokens": "cache_creation_input_tokens",
    }
    out: dict = {}
    for source, target in mapping.items():
        value = usage.get(source)
        if isinstance(value, (int, float)):
            out[target] = int(value)
    return out or None


def _chunk_text(update: dict) -> str:
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        if content.get("type") == "resource_link":
            return f"[resource: {content.get('uri') or content.get('name') or ''}]"
    return ""


def _tool_result_text(content: Any) -> str:
    """Render ACP tool-call content (text blocks and diffs) for display."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "diff":
            parts.append(f"[diff {item.get('path') or ''}]")
            continue
        inner = item.get("content") if kind == "content" else item
        if isinstance(inner, str):
            parts.append(inner)
        elif isinstance(inner, dict):
            inner_type = inner.get("type")
            if inner_type == "text":
                parts.append(str(inner.get("text") or ""))
            elif inner_type in ("image", "audio"):
                parts.append(f"[{inner_type}]")
    return "\n".join(part for part in parts if part).strip()


def _prompt_blocks(turn: TurnRequest, image_supported: bool,
                   model_label: str = "") -> tuple[Optional[list[dict]], str]:
    blocks: list[dict] = []
    for attachment in turn.attachments or []:
        data = attachment.get("data")
        if not data:
            continue
        mime = str(attachment.get("media_type")
                   or attachment.get("mimeType") or "").lower()
        if mime not in IMAGE_MIME_TYPES:
            return None, ("DeepSeek Harness accepts text and image prompts over "
                          f"ACP; this attachment is {mime or 'of an unknown type'}.")
        if not image_supported:
            where = f" ({model_label})" if model_label else ""
            return None, (f"The DeepSeek Harness model in use{where} does not "
                          "accept image input — this runtime reports no image "
                          "support for it. Remove the attachment, or pick "
                          "another model in the model menu.")
        blocks.append({"type": "image",
                       "mimeType": "image/jpeg" if mime == "image/jpg" else mime,
                       "data": data})
    blocks.append({"type": "text", "text": turn.user_text})
    return blocks, ""


# ------------------------------------------------------------------- provider


class DeepSeekHarnessProvider(AgentProvider):
    id = PROVIDER_ID
    label = "DeepSeek Harness"
    #: ACP has no session fork (``dsh-acp`` lists fork as unsupported), and
    #: ComfyTV's branching copies ``resume_token``, so branching would make two
    #: chats write to one Harness session.
    supports_branch = False

    def __init__(self) -> None:
        self._probe_cache: Optional[tuple[float, ProviderStatus]] = None
        self._catalog: list[_ModelOption] = []

    def capabilities(self) -> ProviderCaps:
        # Attachments are advertised because the runtime *can* take images; the
        # concrete model is checked per turn and refused with a clear message
        # when it cannot, rather than dropping the attachment silently.
        return ProviderCaps(stateful=True, tools="mcp", attachments=True)

    # ------------------------------------------------------------ status

    def model_options(self) -> list[dict]:
        """value/label rows for the frontend, from the last live session.

        Empty until a real session has been opened; the UI then falls back to
        its "default model" state instead of inventing a catalog.
        """
        return [{"value": option.value, "label": option.label,
                 **({"group": option.group} if option.group else {})}
                for option in self._catalog]

    async def list_models(self) -> list[str]:
        # The option *values* keep the existing settings contract
        # (`bot-model-<id>` stores one opaque value); labels are exposed
        # separately through model_options().
        return [option.value for option in self._catalog]

    async def _runtime_version(self, launcher: list[str]) -> tuple[str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *launcher, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn_env(),
            )
            out, err = await asyncio.wait_for(proc.communicate(),
                                             timeout=VERSION_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            return "", f"version check failed: {e}"
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()
            return "", f"version check exited with {proc.returncode}: {detail[-200:]}"
        return (out or b"").decode("utf-8", "replace").strip(), ""

    def _account_logged_in(self) -> Optional[bool]:
        """Read-only look at the stored desktop-account grant.

        Never returns or logs the token itself.  ``None`` means "unknown",
        which is the honest answer when the credential store is unreadable.
        """
        path = dsh_home() / ".credentials.yaml"
        if not path.is_file():
            return None
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        records = data.get("records")
        if not isinstance(records, dict):
            return None
        record = records.get("deepseek-account-platform/default")
        if not isinstance(record, dict):
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        return bool(payload.get("token"))

    async def probe(self) -> ProviderStatus:
        now = time.monotonic()
        if self._probe_cache and now - self._probe_cache[0] < PROBE_CACHE_S:
            return self._probe_cache[1]

        launcher, reason = resolve_launcher()
        if launcher is None:
            status = ProviderStatus(available=False, detail=reason)
        else:
            version, version_error = await self._runtime_version(launcher)
            if version_error:
                status = ProviderStatus(available=False, detail=version_error)
            else:
                detail = ""
                if not (profile_dir() / "package.json").is_file():
                    # Provisioning mutates $DSH_HOME, so it happens on the
                    # first real turn rather than during status polling.
                    detail = (f"profile {BOT_PROFILE} is created on first use")
                status = ProviderStatus(available=True, version=version,
                                        logged_in=self._account_logged_in(),
                                        detail=detail)
        self._probe_cache = (now, status)
        return status

    # ------------------------------------------------------------ routing

    def _resolve_route(self, saved_value: str) -> tuple[Optional[tuple[str, str]], str]:
        """Validate the saved model against the configured auth mode.

        Returns ``((provider, model), "")`` or ``(None, reason)``.  An empty
        saved value means "let the live catalog decide" and is returned as
        ``None`` with no error.
        """
        if not saved_value:
            return None, ""
        parsed = parse_model_value(saved_value)
        if parsed is None:
            return None, ("The saved DeepSeek Harness model is not a value this "
                          "runtime issued. Pick a model again in the model menu.")
        provider, model = parsed
        expected = ROUTE_FOR_AUTH[_auth_mode()]
        if provider != expected:
            return None, (
                f"The saved model uses the {provider} route but the configured "
                f"auth mode is {_auth_mode()} ({expected}). Pick a model from "
                "the menu again, or change the auth mode in settings.")
        if self._catalog and saved_value not in {o.value for o in self._catalog}:
            return None, (f"The model {model} is no longer offered by DeepSeek "
                          "Harness. Pick another one in the model menu.")
        return (provider, model), ""

    def _default_option(self) -> tuple[Optional[_ModelOption], str]:
        """First option on the configured route, with its verbatim value.

        Deliberately has no fallback: when the desktop-account route is absent
        or signed out we fail loudly instead of billing the API key.
        """
        expected = ROUTE_FOR_AUTH[_auth_mode()]
        for option in self._catalog:
            if option.group == expected and parse_model_value(option.value):
                return option, ""
        if _auth_mode() == AUTH_DESKTOP:
            return None, (
                "No DeepSeek Account model is available. Sign in to the "
                "DeepSeek Harness desktop app, or switch the DeepSeek Harness "
                "auth mode to api-key in ComfyTV settings — this provider will "
                "not fall back to the API key on its own.")
        return None, ("No DeepSeek API-key model is available. Check the "
                      "credentials in the DeepSeek Harness desktop app.")

    def _default_route(self) -> tuple[Optional[tuple[str, str]], str]:
        option, error = self._default_option()
        if option is None:
            return None, error
        return parse_model_value(option.value), ""

    async def _apply_model(self, session_id: str, transport: AcpTransport,
                           target_value: str) -> str:
        """Apply the chosen model option verbatim.  Returns "" or an error.

        The value is never re-serialized: the runtime matches its own option
        strings character for character, so a rebuilt value (even one that only
        differs by whitespace) is rejected as an unknown model option.
        """
        if not target_value:
            return ""
        if self._catalog:
            known = {option.value for option in self._catalog}
            if target_value not in known:
                route = parse_model_value(target_value)
                label = route[1] if route else target_value
                return (f"The DeepSeek Harness model {label} is no longer "
                        "offered. Pick a model again in the model menu.")
        try:
            await transport.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": MODEL_CONFIG_ID,
                 "value": target_value},
                timeout=SESSION_TIMEOUT_S, label="session/set_config_option")
        except (AcpError, AcpProcessError) as e:
            return self._failure("session/set_config_option", e, transport)
        return ""

    # ------------------------------------------------------------ transport

    async def _handle_request(self, method: str, params: dict,
                              tool_names: dict[str, str]) -> Any:
        if method != "session/request_permission":
            # We advertise no fs/terminal/elicitation capability, so anything
            # else is a protocol surprise; refuse it explicitly.
            raise AcpError(-32601, f"unsupported client method: {method}")
        return await self._permission_outcome(params, tool_names)

    async def _permission_outcome(self, params: dict,
                                  tool_names: dict[str, str]) -> dict:
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
        tool_call = params.get("toolCall") or {}
        tool_call_id = str(tool_call.get("toolCallId") or "")

        name = tool_names.get(tool_call_id, "")
        if not name and tool_call_id:
            deadline = time.monotonic() + PERMISSION_TOOL_WAIT_S
            while time.monotonic() < deadline and not name:
                await asyncio.sleep(_PERMISSION_POLL_S)
                name = tool_names.get(tool_call_id, "")

        # Fail closed: only a confirmed ComfyTV MCP tool is auto-allowed, and
        # the workflow-run decision still belongs to the ComfyTV MCP server.
        if name.startswith("mcp__comfytv__"):
            for option in options:
                if option.get("kind") in ("allow_once", "allow_always"):
                    option_id = option.get("optionId")
                    if option_id:
                        return {"outcome": {"outcome": "selected",
                                            "optionId": option_id}}
        for kind in ("reject_once", "reject_always"):
            for option in options:
                if option.get("kind") == kind and option.get("optionId"):
                    return {"outcome": {"outcome": "selected",
                                        "optionId": option["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    def _make_handlers(self, emit: EmitFn, tool_names: dict[str, str]):
        async def on_notification(method: str, params: dict) -> None:
            if method != "session/update":
                return
            await self._on_update(params.get("update") or {}, emit, tool_names)

        async def on_request(method: str, params: dict) -> Any:
            return await self._handle_request(method, params, tool_names)

        return on_notification, on_request

    async def _on_update(self, update: dict, emit: EmitFn,
                         tool_names: dict[str, str]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _chunk_text(update)
            if text:
                await emit(BotEvent(t="delta", text=text))
            return
        if kind == "agent_thought_chunk":
            # Not surfaced in v1: `_apply_event` has no thinking branch and no
            # provider emits it today.
            return
        if kind == "config_option_update":
            options = _model_options_from(update.get("configOptions"))
            if options:
                self._catalog = options
            return
        if kind == "tool_call":
            tool_call_id = str(update.get("toolCallId") or "")
            name = str(update.get("name") or update.get("title") or "")
            if tool_call_id:
                tool_names[tool_call_id] = name
            raw_input = update.get("rawInput")
            await emit(BotEvent(t="tool_use", name=name, id=tool_call_id,
                                input=raw_input if isinstance(raw_input, dict) else {}))
            return
        if kind == "tool_call_update":
            tool_call_id = str(update.get("toolCallId") or "")
            status = str(update.get("status") or "")
            name = (tool_names.get(tool_call_id)
                    or str(update.get("name") or update.get("title") or ""))
            if tool_call_id and name:
                tool_names.setdefault(tool_call_id, name)
            if status in ("completed", "failed"):
                text = _tool_result_text(update.get("content"))
                await emit(BotEvent(
                    t="tool_result", name=name, id=tool_call_id,
                    text=text[:TOOL_RESULT_CAP],
                    is_error=(status == "failed")))
            return

    # ------------------------------------------------------------ turn

    async def send(self, turn: TurnRequest, emit: EmitFn,
                   handle: TurnHandle) -> TurnResult:
        launcher, reason = resolve_launcher()
        if launcher is None:
            return TurnResult(error=reason)
        provision_error = await ensure_profile(launcher)
        if provision_error:
            return TurnResult(error=provision_error)

        saved_value = turn.model or _saved_model_value()
        route, route_error = self._resolve_route(saved_value)
        if route_error:
            return TurnResult(error=route_error)
        # With no explicit setting, prefer the route the catalog already gave us
        # so the startup overlay — and therefore the capabilities `initialize`
        # advertises — describe the model this turn will actually use.  On the
        # very first turn there is no catalog yet, so the profile default is in
        # effect until session/new reports one (see the model block below).
        if route is None and self._catalog:
            cached_option, cached_error = self._default_option()
            if cached_option is None:
                return TurnResult(error=cached_error)
            route = parse_model_value(cached_option.value)

        cwd = chat_cwd(turn.chat_id)
        try:
            overlay = write_turn_overlay(cwd, route)
        except OSError as e:
            return TurnResult(error=f"could not write the ACP overlay: {e}")
        argv = list(launcher) + ["--profile", BOT_PROFILE, "--patch", str(overlay)]

        tool_names: dict[str, str] = {}
        on_notification, on_request = self._make_handlers(emit, tool_names)
        transport = AcpTransport(argv, cwd=str(cwd), env=spawn_env(),
                                 on_notification=on_notification,
                                 on_request=on_request)
        handle.acp_transport = transport
        session_id = ""
        image_supported = False
        prompt_error = ""
        stop_reason = ""
        usage: Optional[dict] = None
        mcp_servers_needed = bool(turn.mcp_endpoint)

        try:
            try:
                await transport.start()
            except OSError as e:
                return TurnResult(error=f"could not start DeepSeek Harness: {e}")
            handle.process = transport.proc

            # -- initialize ------------------------------------------------
            try:
                init = await transport.request(
                    "initialize",
                    {
                        "protocolVersion": ACP_PROTOCOL_VERSION,
                        "clientInfo": {"name": "comfytv", "version": "1.0"},
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                    },
                    timeout=INITIALIZE_TIMEOUT_S, label="initialize")
            except (AcpError, AcpProcessError) as e:
                return TurnResult(error=self._failure("initialize", e, transport))
            capabilities = (init or {}).get("agentCapabilities") or {}
            prompt_caps = capabilities.get("promptCapabilities") or {}
            image_supported = bool(prompt_caps.get("image"))
            session_caps = capabilities.get("sessionCapabilities") or {}
            # The runtime advertises these as empty objects (`resume: {}`), so
            # presence — not truthiness — is the correct check.
            if "resume" not in session_caps or "close" not in session_caps:
                return TurnResult(error=(
                    "This DeepSeek Harness runtime does not support the ACP "
                    "session/resume and session/close calls ComfyTV needs. "
                    "Update the desktop app."))
            if mcp_servers_needed and not (capabilities.get("mcpCapabilities")
                                           or {}).get("http"):
                return TurnResult(error=(
                    "This DeepSeek Harness runtime does not advertise "
                    "Streamable HTTP MCP support, so the ComfyTV canvas tools "
                    "cannot be mounted."))

            # -- session ---------------------------------------------------
            mcp_servers = []
            if turn.mcp_endpoint:
                mcp_servers.append({"type": "http", "name": MCP_SERVER_NAME,
                                    "url": turn.mcp_endpoint, "headers": []})
            if turn.resume_token:
                try:
                    opened = await transport.request(
                        "session/resume",
                        {"sessionId": turn.resume_token, "cwd": str(cwd),
                         "mcpServers": mcp_servers},
                        timeout=SESSION_TIMEOUT_S, label="session/resume")
                except (AcpError, AcpProcessError) as e:
                    return TurnResult(error=(
                        self._failure("session/resume", e, transport) +
                        " The previous DeepSeek Harness session could not be "
                        "restored, so this turn was not sent. Start a new chat "
                        "to continue."))
                session_id = turn.resume_token
            else:
                try:
                    opened = await transport.request(
                        "session/new",
                        {"cwd": str(cwd), "mcpServers": mcp_servers},
                        timeout=SESSION_TIMEOUT_S, label="session/new")
                except (AcpError, AcpProcessError) as e:
                    detail = self._failure("session/new", e, transport)
                    if saved_value:
                        # The startup overlay named this model, so an unknown
                        # model fails the session before we can report it more
                        # gently.  Point at the actionable fix.
                        detail += (" The saved DeepSeek Harness model may no "
                                   "longer exist — pick one again in the model "
                                   "menu.")
                    return TurnResult(error=detail)
                session_id = str((opened or {}).get("sessionId") or "")
                if not session_id:
                    return TurnResult(error="DeepSeek Harness returned no session id")

            handle.acp_session_id = session_id
            self._catalog = _model_options_from((opened or {}).get("configOptions")) \
                or self._catalog

            # -- persist the session token before prompting ----------------
            # The token is what makes the conversation resumable; losing it
            # would orphan the agent session, so a persistence failure aborts
            # the turn before anything runs.
            try:
                await emit(BotEvent(t="session", id=session_id))
            except Exception as e:
                return TurnResult(error=(
                    "The ComfyTV database could not record the DeepSeek Harness "
                    f"session id, so the turn was not sent: {e}"))

            # -- model selection ------------------------------------------
            # The value handed to set_config_option is always one the runtime
            # issued: the saved value verbatim, or the catalog option selected
            # below.  It is never re-serialized.
            target_value = saved_value
            if not target_value:
                option, option_error = self._default_option()
                if option is None:
                    return TurnResult(error=option_error)
                target_value = option.value
                route = parse_model_value(option.value)
            model_error = await self._apply_model(session_id, transport,
                                                 target_value)
            if model_error:
                return TurnResult(error=model_error)
            if route is not None:
                # Auditable record of which route (and therefore which payer)
                # each turn used.  The route is never chosen silently.
                _log.info("[ComfyTV/deepseek-harness] chat=%s provider=%s "
                          "model=%s image_support=%s",
                          turn.chat_id, route[0], route[1], image_supported)

            # -- prompt ----------------------------------------------------
            blocks, block_error = _prompt_blocks(
                turn, image_supported, route[1] if route else "")
            if block_error:
                return TurnResult(error=block_error)

            try:
                response = await transport.request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": blocks},
                    timeout=TURN_MAX_S, idle_timeout=TURN_IDLE_TIMEOUT_S,
                    label="session/prompt")
            except (AcpError, AcpProcessError) as e:
                if handle.stop_requested:
                    return TurnResult(resume_token=session_id, aborted=True)
                prompt_error = self._failure("session/prompt", e, transport)
            except asyncio.CancelledError:
                handle.stop_requested = True
                raise
            else:
                stop_reason = str((response or {}).get("stopReason") or "")
                usage = _usage_from_acp((response or {}).get("usage"))

        finally:
            await self._teardown(handle, transport, session_id)

        if handle.stop_requested or stop_reason == "cancelled":
            return TurnResult(resume_token=session_id, aborted=True, usage=usage)
        if prompt_error:
            return TurnResult(resume_token=session_id, error=prompt_error,
                              usage=usage)
        if stop_reason == "max_tokens":
            return TurnResult(resume_token=session_id, usage=usage,
                              error="DeepSeek Harness stopped at the output "
                                    "token limit for this turn.")
        if stop_reason == "refusal":
            return TurnResult(resume_token=session_id, usage=usage,
                              error="DeepSeek Harness declined to continue "
                                    "this turn.")
        if stop_reason == "max_turn_requests":
            return TurnResult(resume_token=session_id, usage=usage,
                              error="DeepSeek Harness stopped after the "
                                    "maximum number of model requests.")
        return TurnResult(resume_token=session_id, usage=usage)

    @staticmethod
    def _failure(stage: str, error: Exception, transport: AcpTransport) -> str:
        detail = ""
        if isinstance(error, AcpError) and error.data:
            detail = f" ({json.dumps(error.data, ensure_ascii=False)[:300]})"
        tail = transport.error_detail()
        parts = [f"DeepSeek Harness {stage} failed: {error}{detail}"]
        if tail:
            parts.append(tail)
        return " — ".join(parts)

    async def _teardown(self, handle: TurnHandle, transport: AcpTransport,
                        session_id: str) -> None:
        if session_id:
            try:
                await transport.request("session/close",
                                        {"sessionId": session_id},
                                        timeout=CLOSE_TIMEOUT_S,
                                        label="session/close")
            except Exception as e:
                _log.warning("[ComfyTV/deepseek-harness] session/close failed: %s", e)
        try:
            await transport.close(CLOSE_TIMEOUT_S)
        except Exception:
            _log.exception("[ComfyTV/deepseek-harness] transport close failed")
        if transport.returncode is None:
            await kill_process_tree(handle)
        await transport.dispose()
        handle.acp_transport = None

    async def stop(self, handle: TurnHandle) -> None:
        handle.stop_requested = True
        transport = getattr(handle, "acp_transport", None)
        session_id = getattr(handle, "acp_session_id", "")
        if transport is not None and session_id:
            try:
                await transport.notify("session/cancel",
                                       {"sessionId": session_id})
            except Exception:
                _log.debug("[ComfyTV/deepseek-harness] cancel notify failed",
                           exc_info=True)
            if await transport.wait_exit(CANCEL_GRACE_S):
                return
        await kill_process_tree(handle)
