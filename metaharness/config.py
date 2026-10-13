"""Config — a single dataclass loaded from ~/.metaharness/config.toml.

[router]
base_url = "https://api.openai.com/v1"
model    = "gpt-5.4"
api_key  = "env:GPT_API_KEY"   # resolved from ~/.metaharness/.env

[timeouts]
router_connect_s = 3
router_read_s    = 25
router_deadline_s = 35
turn_idle_s = 120
turn_wall_s = 900
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home() / ".metaharness"
CONFIG_PATH = HOME / "config.toml"


@dataclass(frozen=True)
class RouterConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.4"
    api_key: str = "env:GPT_API_KEY"
    candidates: int = 2  # k for chat A/B; clamped to [1,3] on load


@dataclass(frozen=True)
class Timeouts:
    router_connect_s: float = 3.0
    router_read_s: float = 25.0
    router_deadline_s: float = 35.0
    turn_idle_s: float = 120.0
    turn_wall_s: float = 900.0


@dataclass(frozen=True)
class UIConfig:
    repo_roots: tuple[str, ...] = ("~/Documents",)


@dataclass(frozen=True)
class TrainingConfig:
    # empty string -> inherit from [router]
    router_base_url: str = ""
    router_model: str = ""
    router_api_key: str = ""
    k: int = 2
    router_wall_s: float = 300.0

    def resolve(self, router: "RouterConfig") -> tuple[str, str, str]:
        return (self.router_base_url or router.base_url,
                self.router_model or router.model,
                self.router_api_key or router.api_key)


@dataclass(frozen=True)
class VerifyConfig:
    auto: bool = False
    mode: str = "review"      # review | run
    verifier: str = ""        # "" = router picks; else "harness:model"


@dataclass(frozen=True)
class Config:
    router: RouterConfig = field(default_factory=RouterConfig)
    timeouts: Timeouts = field(default_factory=Timeouts)
    ui: UIConfig = field(default_factory=UIConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    home: Path = HOME


def _resolve_secret(value: str) -> str:
    """`env:NAME` -> ~/.metaharness/.env[NAME]. Bare values pass through."""
    if not value.startswith("env:"):
        return value
    from metaharness import secrets

    name = value[4:].strip()
    return secrets.get(name) or ""


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        cfg = Config()
        return Config(
            router=RouterConfig(
                base_url=cfg.router.base_url,
                model=cfg.router.model,
                api_key=_resolve_secret(cfg.router.api_key),
            ),
            timeouts=cfg.timeouts, ui=cfg.ui, training=cfg.training,
            verify=cfg.verify, home=cfg.home,
        )
    data = tomllib.loads(path.read_text())
    r = data.get("router", {})
    t = data.get("timeouts", {})
    u = data.get("ui", {})
    tr = data.get("training", {})
    vf = data.get("verify", {})
    roots = u.get("repo_roots")
    api_key = _resolve_secret(r.get("api_key", RouterConfig.api_key))
    tr = dict(tr)
    if tr.get("router_api_key"):
        tr["router_api_key"] = _resolve_secret(tr["router_api_key"])
    return Config(
        router=RouterConfig(
            base_url=r.get("base_url", RouterConfig.base_url),
            model=r.get("model", RouterConfig.model),
            api_key=api_key,
            candidates=max(1, min(3, int(r.get("candidates", RouterConfig.candidates)))),
        ),
        timeouts=Timeouts(**{k: v for k, v in t.items() if k in Timeouts.__annotations__}),
        ui=UIConfig(repo_roots=tuple(roots) if roots else UIConfig.repo_roots),
        training=TrainingConfig(**{k: v for k, v in tr.items()
                                   if k in TrainingConfig.__annotations__}),
        verify=VerifyConfig(**{k: v for k, v in vf.items()
                               if k in VerifyConfig.__annotations__}),
    )
