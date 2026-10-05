"""Central configuration.

Everything is read from the environment (``AGENT_`` prefix) and/or ``.env``.
Nothing in this module touches the filesystem apart from *reading* the optional
``.env`` file via pydantic-settings, and nothing spawns processes.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIM = 1024
"""Fixed vector dimension.  BGE-M3 and DashScope text-embedding-v4 both emit 1024."""


def project_root() -> Path:
    """Repository root (the directory that contains ``src/``)."""
    return Path(__file__).resolve().parents[2]


#: keys from .env that third-party libraries read straight out of the environment
ENV_PASSTHROUGH_PREFIXES = ("HF_", "TRANSFORMERS_", "TORCH_", "SENTENCE_")
ENV_PASSTHROUGH_EXACT = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _load_passthrough_env() -> None:
    """Make .env's HF_*/proxy values visible to huggingface_hub and torch.

    Only fills variables that are not already set, so an explicit environment wins.
    """
    path = project_root() / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - unreadable .env
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        if key.startswith(ENV_PASSTHROUGH_PREFIXES) or key in ENV_PASSTHROUGH_EXACT:
            os.environ[key] = value.strip()


_load_passthrough_env()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        # reject a bad runtime value instead of storing it (/admin/config)
        validate_assignment=True,
        env_file=str(project_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- storage
    var_dir: Path = Field(default_factory=lambda: project_root() / "var")
    db_url: str = "postgresql+asyncpg://agent:agent@127.0.0.1:5432/agentbox?ssl=disable"

    # ------------------------------------------------------------- ai service
    ai_host: str = "0.0.0.0"
    ai_port: int = 8090

    # ----------------------------------------------------------- control plane
    control_host: str = "0.0.0.0"
    control_port: int = 8091
    # The AI service runs inside the VM, where the Windows host is 10.0.2.2.
    control_url: str = "http://10.0.2.2:8091"
    # The CLI runs on the Windows host itself and must use loopback.
    control_url_local: str = "http://127.0.0.1:8091"
    control_secret: str = ""  # shared secret for AI service -> control plane (X-Agent-Token)

    # -------------------------------------------------------------------- llm
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 180.0
    llm_temperature: float = 0.0
    # Model used automatically for a turn that carries images: on this gateway only
    # ``deepseek-flash`` reports ``input_modalities: ["text", "image"]``; the text model
    # accepts the same body but answers with empty content because it never sees the image.
    llm_vision_model: str = "deepseek-flash"
    # Reasoning effort forwarded verbatim when non-empty ("" = leave the gateway default).
    # Pass-through only: measured on this gateway the knob is accepted but its effect is
    # weak and noisy, so it is never used to decide anything here.
    llm_effort: Literal["", "low", "high", "max"] = ""
    # Image input limits: per-image decoded size and images per /chat request.
    llm_max_image_bytes: int = 8_000_000
    llm_max_images: int = 4
    # False = refuse image parts altogether instead of switching to the vision model.
    llm_vision_enabled: bool = True

    # ------------------------------------------------------------- embeddings
    embedding_backend: Literal["local", "dashscope"] = "local"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = EMBEDDING_DIM
    embedding_batch: int = 16
    dashscope_api_key: str = ""
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # -------------------------------------------------------------------- asr
    # Local speech-to-text: POST /asr takes raw audio bytes, runs a small
    # faster-whisper (CTranslate2) model on the AI service's CPU and returns text.
    # Nothing is written to disk; the audio is decoded out of memory (agent/asr.py).
    asr_enabled: bool = True
    # tiny | base | small | medium (or any faster-whisper repo id / local model dir)
    asr_model: str = "base"
    asr_compute_type: str = "int8"
    asr_language: str = ""  # "" = auto-detect
    asr_max_seconds: float = 300.0
    # The weights are expected in HF_HOME already: the service runs under
    # ProtectSystem=strict (ReadWritePaths=/opt/agentbox/app/var), so it cannot
    # write the model cache.  True = read the cache only and fail fast when the
    # model was never downloaded.
    asr_local_only: bool = True

    # ------------------------------------------------------------------ voice
    # Real-time voice mode of the *console* (`agent chat --voice`): record with the
    # microphone, run an energy VAD to find the end of the utterance, POST the WAV to
    # the same /asr endpoint /voice uses, and read the answer back through Windows SAPI.
    # These settings belong to the terminal, not to the AI service (which has no audio
    # device), so /admin/config does not expose them: /speak changes voice_tts for the
    # running console and the default comes from the host environment/.env.
    voice_input_device: str = ""  # "" = sounddevice's default input device; else index or name
    voice_silence_s: float = 1.2  # quiet time that ends the utterance
    voice_max_s: float = 30.0  # hard cap on one utterance
    voice_tts: bool = True  # speak the answer back (Windows SAPI; no new dependency)
    voice_tts_max_chars: int = 300  # truncate long answers before speaking them
    voice_vad_floor: float = 0.012  # absolute RMS floor; below it nothing is "speech"

    # ------------------------------------------------------------------- qemu
    qemu_dir: Path | None = None
    sandbox_dir: Path | None = None
    sandbox_accel: Literal["auto", "kvm", "whpx", "tcg"] = "auto"
    # Empty = automatic per accelerator.  WHPX on Windows needs the qemu64 model.
    sandbox_cpu: str = ""

    # 'off' (default): the sandbox gets no NIC at all.
    # 'full': a slirp user-mode NIC -> the guest reaches the internet (apt, ping,
    # DNS).  This deliberately removes the sandbox's network isolation; every
    # other guarantee (read-only root, non-root user, limits, no host shares)
    # still holds.  Requires iproute2/iputils-ping in the image (they are).
    sandbox_net_mode: Literal["off", "full"] = "off"
    sandbox_pool_size: int = 2
    sandbox_max_vms: int = 4
    sandbox_vm_memory_mb: int = 2048
    sandbox_vm_cpus: int = 4
    # Build-time constant of the image (deploy/sandbox/build-sandbox-image.sh);
    # changing it requires rebuilding the sandbox image.
    sandbox_workspace_mb: int = 4096
    sandbox_boot_timeout_s: float = 180.0
    sandbox_idle_reap_s: float = 1800.0
    sandbox_session_ttl_s: float = 7200.0
    sandbox_job_memory_bytes: int = 5_368_709_120
    sandbox_job_max_processes: int = 256

    # ------------------------------------------------------------ platform VM
    # Extra port forwards from the platform VM to this machine, applied by
    # deploy/windows/run-platform-vm.ps1: comma separated single ports / inclusive
    # ranges, e.g. "2121,30000-30010".  Loopback only (127.0.0.1) -- an explicit
    # bind address is refused; 445/139 are refused too (this host owns SMB).
    # Empty (the default) = off: no extra forward.
    platform_ports: str = ""

    # ------------------------------------------------------- execution limits
    exec_default_timeout_s: float = 120.0
    exec_max_timeout_s: float = 300.0
    max_output_bytes: int = 1_000_000
    tool_source_max_bytes: int = 16_384

    # ------------------------------------------------- self-written tools
    # strict (default): the narrow, reviewable list.  extended: + os/sys/time/
    # pathlib/subprocess/smtplib/email/sqlite3/... (still no ctypes).
    # unrestricted: any module.  All three are mutable at runtime via /config.
    tool_import_profile: Literal["strict", "extended", "unrestricted"] = "strict"
    tool_extra_modules: str = ""  # comma separated, added on top of the profile
    # allow open()/open_code() -- honoured only for a tool that declares fs.read/fs.write
    tool_allow_open: bool = False
    tool_timeout_s: float = 30.0

    # ------------------------------------------------------------ agent loop
    max_steps: int = 24
    max_history_messages: int = 40
    search_k: int = 5

    # ------------------------------------------------- network switch + firewall
    # The sandbox VM itself never gets a NIC.  These settings gate the `net.*` tools,
    # which run *in the AI service* (the process that already talks to the LLM API)
    # and hand the bytes to the sandbox through the normal gateway.
    # ------------------------------------------------------------------- MCP
    # JSON: {"server": {"url": "https://host/mcp", "headers": {...}}}
    mcp_servers: str = ""
    mcp_sync_on_start: bool = False

    # ------------------------------------------------------- permission tier
    # Coarse policy ceiling enforced by the AI service:
    #   safe         sandbox tools only (default)
    #   trusted      + firewalled net.* tools, MCP tools
    #   unrestricted + host.exec on the control-plane machine (needs a phrase)
    permission_tier: Literal["safe", "trusted", "unrestricted"] = "safe"
    # typing this exact phrase (in the console, or as the confirm argument) is
    # required before host.exec will run anything on the host machine
    host_exec_phrase: str = "ENABLE-HOST-EXEC"
    # directory host.exec may work in (default: the repo root); everything it runs
    # must stay inside it
    host_workdir: str = ""
    host_exec_timeout_s: float = 60.0
    # Bringing a *produced* file back out of the sandbox is not host.exec: it is the
    # transport half of the task, so it needs its own switch and phrase rather than
    # the unrestricted tier.  Everything lands under <var_dir>/pulled and can never
    # overwrite an existing file unless the caller asks for it.
    host_pull_enabled: bool = True
    host_pull_phrase: str = "PULL-FROM-SANDBOX"
    #: hard cap on one pulled file (both the AI service and the control plane enforce it)
    fs_pull_max_bytes: int = 8_000_000

    # ---------------------------------------------------------------- persona
    # AGENT_PERSONA picks a markdown persona injected into the system prompt.
    # Personas steer tone/role only; permissions live in code, never in the prompt.
    persona: str = "engineer"
    # extra directory of operator-authored personas (defaults to <repo>/personas)
    persona_dir: str = ""
    # The operator's own prompt text, as a *file* rather than hand-edited Python: an
    # absolute path that may live outside the repo (e.g. /opt/agentbox/custom-prompt.md,
    # the durable slot deploy/windows/push-repo-to-vm.ps1 never touches).  It is read
    # again on every request, so an edit takes effect on the next turn with no restart,
    # and a missing/unreadable/oversized file only costs this one layer.
    # Empty = <repo>/prompts/custom.md if that file exists, else no custom layer.
    custom_prompt_file: str = ""

    net_enabled: bool = False
    # comma separated hosts; "*.example.com" wildcards; a bare "*" allows everything
    net_allow_hosts: str = ""
    # comma separated ports; a URL whose port is not listed is refused
    net_allow_ports: str = "80,443"
    net_max_bytes: int = 8_000_000
    net_timeout_s: float = 30.0
    net_max_redirects: int = 3
    net_allow_private_hosts: bool = False
    net_user_agent: str = "agentbox/0.1 (+https://localhost)"

    # Where per-session workspace overlays live.  Empty = <sandbox_root>/sessions.
    # Two managers sharing one directory can hand the same overlay to two VMs,
    # which corrupts it; tests set this to a scratch directory.
    sessions_dir_override: str = ""

    # ------------------------------------------------------------------- misc
    log_level: str = "INFO"
    rpc_token: str = ""

    # ------------------------------------------------------------- validators
    @field_validator("embedding_dim")
    @classmethod
    def _fixed_dim(cls, v: int) -> int:
        if v != EMBEDDING_DIM:
            raise ValueError(
                f"embedding_dim must stay {EMBEDDING_DIM}: the pgvector column is vector({EMBEDDING_DIM}). "
                "Changing it requires a migration and a re-embedding of every registered tool."
            )
        return v

    # -------------------------------------------------------------- accessors
    @property
    def sandbox_root(self) -> Path:
        return Path(self.sandbox_dir) if self.sandbox_dir else self.var_dir / "sandbox"

    @property
    def sessions_dir(self) -> Path:
        return Path(self.sessions_dir_override) if self.sessions_dir_override else self.sandbox_root / "sessions"

    def net_allowlist(self) -> list[str]:
        from agent.ai.net import parse_allowlist

        return parse_allowlist(self.net_allow_hosts)

    @property
    def console_dir(self) -> Path:
        return self.sandbox_root / "console"

    @property
    def run_dir(self) -> Path:
        return self.var_dir / "run"

    @property
    def base_rootfs(self) -> Path:
        return self.sandbox_root / "rootfs.img"

    @property
    def base_kernel(self) -> Path:
        return self.sandbox_root / "vmlinuz"

    @property
    def base_initrd(self) -> Path:
        return self.sandbox_root / "initrd.img"

    @property
    def blank_workspace(self) -> Path:
        return self.sandbox_root / "workspace-blank.qcow2"

    def image_ready(self) -> bool:
        return all(
            p.is_file()
            for p in (self.base_rootfs, self.base_kernel, self.base_initrd, self.blank_workspace)
        )

    def sandbox_image_problems(self) -> list[str]:
        missing = [
            str(p.name)
            for p in (self.base_rootfs, self.base_kernel, self.base_initrd, self.blank_workspace)
            if not p.is_file()
        ]
        if missing:
            return [f"missing sandbox image artefact(s): {', '.join(missing)} in {self.sandbox_root}"]
        return []

    # --------------------------------------------------------- qemu binaries
    def _qemu_search_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        if self.qemu_dir:
            dirs.append(Path(self.qemu_dir))
        env_dir = os.environ.get("AGENT_QEMU_DIR")
        if env_dir:
            dirs.append(Path(env_dir))
        dirs.append(project_root() / "qemu")
        dirs.append(project_root() / "tools" / "qemu")
        return dirs

    @staticmethod
    def _exe(name: str) -> str:
        return f"{name}.exe" if sys.platform.startswith("win") else name

    def find_qemu(self, name: str) -> Path:
        """Locate a QEMU binary: explicit setting, known dirs, then PATH."""
        exe = self._exe(name)
        for d in self._qemu_search_dirs():
            candidate = d / exe
            if candidate.is_file():
                return candidate
        found = shutil.which(exe) or shutil.which(name)
        if found:
            return Path(found)
        raise FileNotFoundError(
            f"{exe} not found. Set AGENT_QEMU_DIR (e.g. {project_root() / 'qemu'}) or add QEMU to PATH."
        )

    @property
    def qemu_system(self) -> Path:
        return self.find_qemu("qemu-system-x86_64")

    @property
    def qemu_cpu(self) -> str | None:
        """CPU model override for QEMU, or ``None`` to use the per-accelerator default."""
        return self.sandbox_cpu or None

    @property
    def qemu_img(self) -> Path:
        return self.find_qemu("qemu-img")

    @property
    def iso_path(self) -> Path:
        return project_root() / "debian-13.7.0-amd64-netinst.iso"

    def ensure_dirs(self) -> None:
        for d in (self.var_dir, self.sandbox_root, self.sessions_dir, self.console_dir, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
