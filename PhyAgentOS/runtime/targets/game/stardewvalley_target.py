"""Stardew Valley rollout target via PhyAgentOS bridge HTTP API.

Architecture:
  [PhyAgentOS Python] StardewValleyTarget --HTTP→ bridge → StarDojo/SMAPI → Stardew Valley

The bridge server (bridge_server.py / uvicorn) runs on the same Windows machine
alongside Stardew Valley + SMAPI + StardojoMod. It exposes a REST API that
this target consumes. No game protocol code lives in this file — it is a
pure HTTP client, mirroring MinecraftTarget.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PhyAgentOS.runtime.targets.game.base import BaseGameTarget
from PhyAgentOS.runtime.watchdog.errors import (
    TargetConnectionError,
    TargetResetError,
    TargetStepError,
)

logger = logging.getLogger(__name__)

FALLBACK_ACTION_TYPES = frozenset({
    "move", "use", "interact", "choose_item", "craft",
    "choose_option", "attach_item", "unattach_item", "menu",
})

_STARDEW_ACTION_CONVERTERS: dict[str, Callable] = {}


def _register(types: list[str]):
    def _deco(fn):
        for t in types:
            _STARDEW_ACTION_CONVERTERS[t] = fn
        return fn
    return _deco


@_register(["move"])
def _fmt_move(params: dict) -> str:
    return f"move({int(params['dx'])}, {int(params['dy'])})"


@_register(["use"])
def _fmt_use(params: dict) -> str:
    return f"use(\"{params['direction']}\")"


@_register(["interact"])
def _fmt_interact(params: dict) -> str:
    return f"interact(\"{params['direction']}\")"


@_register(["choose_item"])
def _fmt_choose_item(params: dict) -> str:
    return f"choose_item({int(params['slot_index'])})"


@_register(["craft"])
def _fmt_craft(params: dict) -> str:
    return f"craft(\"{params['item_name']}\")"


@_register(["choose_option"])
def _fmt_choose_option(params: dict) -> str:
    idx = int(params["option_index"])
    qty = int(params.get("quantity", 0))
    direction = params.get("direction")
    if direction is not None:
        return f"choose_option({idx}, {qty}, \"{direction}\")"
    return f"choose_option({idx}, {qty})"


@_register(["attach_item"])
def _fmt_attach_item(params: dict) -> str:
    return f"attach_item({int(params['slot_index'])})"


@_register(["unattach_item"])
def _fmt_unattach_item(params: dict) -> str:
    return "unattach_item()"


@_register(["menu"])
def _fmt_menu(params: dict) -> str:
    return f"menu(\"{params['option']}\", \"{params['menu_name']}\")"


def action_dict_to_string(action: dict[str, Any]) -> str:
    action_type = action.get("type", "")
    params = action.get("params", {})
    converter = _STARDEW_ACTION_CONVERTERS.get(action_type)
    if converter:
        return converter(params)
    return action.get("action", "") or action_type


class StardewValleyTarget(BaseGameTarget):
    """Remote target connected to a PhyAgentOS Stardew bridge.

    The bridge runs on the Windows machine alongside Stardew Valley + SMAPI.
    It exposes /health, /observe, /execute endpoints. This target talks
    to the bridge over HTTP.
    """

    @property
    def _current_action_types(self) -> frozenset:
        return getattr(self, "_valid_action_types", FALLBACK_ACTION_TYPES)

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._built = False
        self._http = None
        self._bridge_url: str = str(self.config.get("bridge_url", "")).strip()
        self._step_idx: int = 0
        self._step_delay: float = float(self.config.get("step_delay", 0.1))
        self._last_status: dict[str, Any] = {"status": "idle"}
        self._valid_action_types = FALLBACK_ACTION_TYPES
        self._latest_obs: dict[str, Any] = {}
        self._benchmark_mode: bool = bool(self.config.get("benchmark_mode", False))

    def _game_type(self) -> str:
        return "stardewvalley_smapi"

    def reset_step_counter(self) -> None:
        self._step_idx = 0

    def _get_http(self):
        if self._http is None:
            import httpx
            verify = self.config.get("verify_ssl", True)
            http_timeout = float(self.config.get("http_timeout", 15.0))
            self._http = httpx.Client(timeout=http_timeout, verify=verify, trust_env=False)
        return self._http

    def build(self) -> None:
        bridge_url = self._bridge_url
        if not bridge_url:
            raise TargetConnectionError(
                "bridge_url is not configured. Set it in TARGETS.md config, "
                "e.g. http://127.0.0.1:8765 (the Stardew bridge URL)."
            )
        client = self._get_http()
        try:
            resp = client.get(f"{bridge_url}/health")
            data = resp.json()
        except Exception as exc:
            raise TargetConnectionError(
                f"Cannot reach Stardew bridge at {bridge_url}: {exc}. "
                "Ensure the bridge is running on Windows."
            ) from exc

        if not data.get("ok"):
            raise TargetConnectionError(
                f"Stardew bridge at {bridge_url} returned not ok: {data}"
            )

        self._built = True
        logger.info("StardewValleyTarget connected to bridge at %s", bridge_url)

    def reset(self, session_ctx: dict[str, Any]) -> dict[str, Any]:
        if not self._built:
            raise TargetResetError("target not built")
        time.sleep(self._step_delay)
        return super().reset(session_ctx)

    def observe(self) -> dict[str, Any]:
        client = self._get_http()
        try:
            resp = client.get(f"{self._bridge_url}/observe")
            data = resp.json()
        except Exception as exc:
            logger.warning("observe failed: %s, using cached obs", exc)
            return self._cached_obs()

        if not data.get("ok"):
            logger.warning("observe returned not ok: %s", data.get("error"))
            return self._cached_obs()

        obs = data.get("obs", {})
        self._latest_obs = obs
        return self._format_obs(obs)

    def _cached_obs(self) -> dict[str, Any]:
        if self._latest_obs:
            return self._format_obs(self._latest_obs)
        return self._empty_obs()

    def _empty_obs(self) -> dict[str, Any]:
        return {
            "image": None,
            "state": [0.0, 0.0, 100.0, 270.0, 500.0],
            "inventory": [],
            "info": {
                "location": "unknown",
                "position": [0, 0],
                "facing_direction": "down",
                "health": 100,
                "energy": 270,
                "money": 500,
                "time": "600",
                "day": 1,
                "season": "spring",
            },
        }

    def _format_obs(self, obs: dict[str, Any]) -> dict[str, Any]:
        position = obs.get("position", [0, 0])
        if isinstance(position, list) and len(position) == 2:
            px, py = position[0], position[1]
        else:
            px, py = 0, 0

        try:
            health = float(obs.get("health", 100))
        except (ValueError, TypeError):
            health = 100.0
        try:
            energy = float(obs.get("energy", 270))
        except (ValueError, TypeError):
            energy = 270.0
        try:
            money = float(obs.get("money", 500))
        except (ValueError, TypeError):
            money = 500.0

        try:
            px_f = float(px) if not isinstance(px, dict) else 0.0
            py_f = float(py) if not isinstance(py, dict) else 0.0
        except (ValueError, TypeError):
            px_f, py_f = 0.0, 0.0

        return {
            **obs,
            "image": None,
            "state": [px_f, py_f, health, energy, money],
            "inventory": obs.get("inventory", []),
            "nearby_objects": obs.get("surroundings", []),
            "last_image_url": obs.get("latest_image_url"),
            "chosen_item": obs.get("chosen_item"),
            "current_menu": obs.get("current_menu"),
            "info": {
                "location": obs.get("location", "unknown"),
                "position": [px, py],
                "facing_direction": obs.get("facing_direction", "down"),
                "health": obs.get("health", 100),
                "energy": obs.get("energy", 270),
                "money": obs.get("money", 500),
                "time": obs.get("time", "600"),
                "day": obs.get("day", 1),
                "season": obs.get("season", "spring"),
                "chosen_item": obs.get("chosen_item"),
                "current_menu": obs.get("current_menu"),
            },
        }

    def step(self, action: Any) -> dict[str, Any]:
        if not self._built:
            raise TargetStepError("target not built")

        if isinstance(action, str):
            action_str = action
        elif isinstance(action, dict):
            action_str = action_dict_to_string(action)
        else:
            raise TargetStepError(f"action must be str or dict, got {type(action).__name__}")

        result = self._post_action(action_str)
        if result.get("fatal") is True:
            raise TargetStepError("game bridge stopped after an uncertain execution")
        time.sleep(self._step_delay)
        self._step_idx += 1

        obs = result.get("obs") or self.observe()
        action_ok = result.get("ok", False)
        info = {
            "ok": action_ok,
            "step_idx": self._step_idx,
            "result": result.get("error", ""),
            "action": action_str,
        }
        return {
            "obs": obs,
            "reward": 0.0,
            "done": self._check_done(),
            "info": info,
        }

    def _post_action(self, action_str: str) -> dict[str, Any]:
        client = self._get_http()
        action_timeout = float(self.config.get("action_timeout", 15.0))
        endpoint = "/benchmark/execute" if self._benchmark_mode else "/execute"
        try:
            payload = {"action": action_str}
            resp = client.post(
                f"{self._bridge_url}{endpoint}",
                json=payload,
                timeout=action_timeout,
            )
            return resp.json()
        except Exception as exc:
            logger.warning("action post failed (%s): %s", action_str, exc)
            return {"ok": False, "error": str(exc)}

    def _check_done(self) -> bool:
        return False

    def cancel(self, reason: str) -> None:
        logger.info("StardewValleyTarget cancelled: %s", reason)
        self._last_status = {"status": "cancelled", "reason": reason}

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None
        self._built = False
        logger.info("StardewValleyTarget disconnected")

    def write_environment_snapshot(self, env_path: Path) -> None:
        try:
            obs = self.observe()
        except Exception:
            logger.warning("Failed to observe for environment snapshot")
            return

        info = obs.get("info", {})
        position = info.get("position", [0, 0])
        nearby = obs.get("nearby_objects", [])

        objects_summary = []
        for obj in nearby:
            if isinstance(obj, dict):
                pos = obj.get("position", "")
                name = obj.get("object_at_tile") or obj.get("terrain_at_tile") or obj.get("building_info")
                if name:
                    objects_summary.append(f"{pos}: {name}")

        snapshot = {
            "player": {
                "position": position,
                "location": info.get("location", "unknown"),
                "facing": info.get("facing_direction", "?"),
                "health": info.get("health"),
                "energy": info.get("energy"),
                "money": info.get("money"),
                "time": info.get("time"),
                "day": info.get("day"),
                "season": info.get("season"),
            },
            "nearby_objects_summary": objects_summary[:30],
        }

        prefix = "\n## Stardew Snapshot\n"
        content = prefix + "```json\n" + json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n```\n"
        if env_path.exists():
            try:
                existing = env_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""
        else:
            existing = ""
        idx = existing.rfind("\n## Stardew Snapshot\n")
        if idx >= 0:
            existing = existing[:idx]
        env_path.write_text(existing + content, encoding="utf-8")
