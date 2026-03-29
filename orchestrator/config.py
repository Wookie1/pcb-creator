"""Configuration for the orchestrator."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load a .env file into os.environ (stdlib only, no dependencies).

    Supports: KEY=value, KEY="value", # comments, blank lines.
    Does NOT override existing env vars.
    """
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Don't override existing env vars
            if key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass  # .env loading is best-effort


@dataclass
class OrchestratorConfig:
    # LLM settings
    generate_model: str = "openrouter/qwen/qwen3.5-27b"
    review_model: str = "openrouter/qwen/qwen3.5-27b"
    gather_model: str = "openrouter/qwen/qwen3.5-27b"
    temperature: float = 0.0
    max_tokens: int = 32768

    # LLM API settings
    api_base: str | None = None   # LLM provider base URL (e.g. http://localhost:11434/v1)
    api_key: str | None = None    # LLM provider API key (OpenRouter, Anthropic, OpenAI, etc.)
    llm_timeout: int = 1800       # LLM request timeout in seconds (default 30min for large boards)
    llm_extra_body: dict = field(default_factory=dict)  # e.g. {"thinking": False}

    # Workflow settings
    max_rework_attempts: int = 5

    # Export settings
    export_kicad: Path | bool | None = None  # True = auto path, Path = specific path

    # Agent mode — use vision-based autonomous review instead of browser approval gate
    agent_mode: bool = False

    # Vision review settings (used when agent_mode=True)
    vision_model: str = "anthropic/claude-sonnet-4-20250514"
    vision_max_review_attempts: int = 3

    # Router settings
    router_engine: str = "freerouting"  # "freerouting" or "builtin"
    freerouting_jar_path: Path | None = None
    freerouting_timeout_s: int = 300

    # Optimizer settings
    enable_optimizer: bool = True
    optimizer_iterations: int | None = None  # None = auto-scale from component count
    optimizer_seed: int | None = None

    # Tiered lookup settings
    kicad_library_path: str | None = None   # Root of KiCad footprint library (.pretty dirs)
    component_cache_path: str = "~/.pcb-creator/component_cache.json"
    llm_enrichment_workers: int = 4         # Max parallel LLM calls for spec/footprint enrichment

    # Paths (relative to base_dir)
    base_dir: Path = field(default_factory=lambda: Path.cwd())
    standards_path: str = "STANDARDS.md"
    schema_path: str = "schemas/circuit_schema.json"
    validator_path: str = "validators/validate_netlist.py"
    projects_dir: str = "projects"

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "OrchestratorConfig":
        """Load config from .env file and environment variables.

        Priority: env vars > .env file > defaults.
        """
        config = cls()
        if base_dir:
            config.base_dir = base_dir

        # Load .env file (stdlib only — no python-dotenv dependency)
        env_path = config.base_dir / ".env"
        if env_path.exists():
            _load_dotenv(env_path)

        config.generate_model = os.environ.get(
            "PCB_GENERATE_MODEL", config.generate_model
        )
        config.review_model = os.environ.get("PCB_REVIEW_MODEL", config.review_model)
        config.gather_model = os.environ.get("PCB_GATHER_MODEL", config.gather_model)
        config.api_base = os.environ.get("PCB_LLM_API_BASE", config.api_base)
        config.api_key = os.environ.get("PCB_LLM_API_KEY", config.api_key)
        max_tokens_env = os.environ.get("PCB_LLM_MAX_TOKENS")
        if max_tokens_env:
            config.max_tokens = int(max_tokens_env)
        timeout_env_llm = os.environ.get("PCB_LLM_TIMEOUT")
        if timeout_env_llm:
            config.llm_timeout = int(timeout_env_llm)
        config.max_rework_attempts = int(
            os.environ.get("PCB_MAX_REWORK", str(config.max_rework_attempts))
        )
        config.enable_optimizer = os.environ.get(
            "PCB_ENABLE_OPTIMIZER", "true"
        ).lower() in ("true", "1", "yes")
        iter_env = os.environ.get("PCB_OPTIMIZER_ITERATIONS")
        config.optimizer_iterations = int(iter_env) if iter_env else None
        seed_env = os.environ.get("PCB_OPTIMIZER_SEED")
        config.optimizer_seed = int(seed_env) if seed_env else None
        config.router_engine = os.environ.get(
            "PCB_ROUTER_ENGINE", config.router_engine
        )
        jar_env = os.environ.get("PCB_FREEROUTING_JAR")
        config.freerouting_jar_path = Path(jar_env) if jar_env else None
        timeout_env = os.environ.get("PCB_FREEROUTING_TIMEOUT")
        config.freerouting_timeout_s = int(timeout_env) if timeout_env else config.freerouting_timeout_s
        kicad_lib = os.environ.get("PCB_KICAD_LIBRARY_PATH")
        if kicad_lib:
            config.kicad_library_path = kicad_lib
        cache_path_env = os.environ.get("PCB_COMPONENT_CACHE_PATH")
        if cache_path_env:
            config.component_cache_path = cache_path_env
        workers_env = os.environ.get("PCB_LLM_ENRICHMENT_WORKERS")
        if workers_env:
            config.llm_enrichment_workers = int(workers_env)
        config.vision_model = os.environ.get("PCB_VISION_MODEL", config.vision_model)
        vision_attempts_env = os.environ.get("PCB_VISION_MAX_ATTEMPTS")
        if vision_attempts_env:
            config.vision_max_review_attempts = int(vision_attempts_env)
        models_dir_env = os.environ.get("PCB_3D_MODELS_DIR")
        if models_dir_env:
            os.environ["PCB_3D_MODELS_DIR"] = models_dir_env  # propagate to model_fetcher
        projects_dir_env = os.environ.get("PCB_PROJECTS_DIR")
        if projects_dir_env:
            config.projects_dir = projects_dir_env
        return config

    def resolve(self, relative_path: str) -> Path:
        """Resolve a path relative to base_dir."""
        return self.base_dir / relative_path
