"""Minecraft Java Edition rollout target.  Connects to a mineflayer bridge
running on the same machine as Minecraft (typically Windows 11).

Architecture:
  [Linux cloud] MinecraftTarget --HTTP→ ngrok → [Windows 11] mineflayer bridge
                                                              └→ Minecraft

The bridge runs on the Windows machine alongside Minecraft, connecting
to the local game instance.  This target is a remote HTTP client — it
does NOT require Minecraft or pyCraft on the Linux side.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np

from PhyAgentOS.runtime.targets.game.base import BaseGameTarget
from PhyAgentOS.runtime.watchdog.errors import (
    TargetConnectionError,
    TargetResetError,
    TargetStepError,
)

logger = logging.getLogger(__name__)

FALLBACK_ACTION_TYPES = frozenset({
    "move", "look", "jump", "sneak", "sprint", "attack",
    "interact", "place", "dig", "use", "select_slot", "drop",
    "chat", "collect", "equip", "craft", "smelt",
})


class MinecraftTarget(BaseGameTarget):
    """Remote target connected to a mineflayer bridge running on the game machine.

    The bridge (``game/mc-bridge/bridge_server.js``) runs on the Windows
    machine alongside Minecraft.  It spawns a mineflayer bot that joins
    the local Minecraft world and exposes an HTTP API.  This target talks
    to the bridge over HTTP (typically via an ngrok tunnel).
    """

    @property
    def _current_action_types(self) -> frozenset:
        return getattr(self, "_valid_action_types", FALLBACK_ACTION_TYPES)

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._built = False
        self._http = None
        self._bridge_url: str = str(self.config.get("bridge_url", "")).strip()
        self._position = np.zeros(3, dtype=np.float64)
        self._yaw: float = 0.0
        self._pitch: float = 0.0
        self._health: float = 20.0
        self._hunger: int = 20
        self._held_slot: int = 0
        self._on_ground: bool = True
        self._dimension: str = "overworld"
        self._step_idx: int = 0
        self._step_delay: float = float(self.config.get("step_delay", 0.1))
        self._last_status: dict[str, Any] = {"status": "idle"}
        self._valid_action_types = FALLBACK_ACTION_TYPES

    def _game_type(self) -> str:
        return "minecraft_java_bot"

    def reset_step_counter(self) -> None:
        self._step_idx = 0

    def _get_http(self):
        if self._http is None:
            import httpx
            verify = self.config.get("verify_ssl", True)
            # Default timeout covers state/health polling; actions override this
            http_timeout = float(self.config.get("http_timeout", 15.0))
            headers = {"ngrok-skip-browser-warning": "true"}
            host = urlparse(self._bridge_url).hostname
            local_bridge = host in {"127.0.0.1", "localhost", "::1"}
            trust_env = bool(self.config.get("trust_env", not local_bridge))
            self._http = httpx.Client(
                timeout=http_timeout,
                verify=verify,
                headers=headers,
                trust_env=trust_env,
            )
        return self._http

    def build(self) -> None:
        bridge_url = self._bridge_url
        if not bridge_url:
            raise TargetConnectionError(
                "bridge_url is not configured. Set it in TARGETS.md config, "
                "e.g. https://xxxx.ngrok-free.app (the ngrok URL for the Windows bridge)."
            )
        client = self._get_http()
        try:
            resp = client.get(f"{bridge_url}/health")
            data = resp.json()
        except Exception as exc:
            raise TargetConnectionError(
                f"Cannot reach mineflayer bridge at {bridge_url}: {exc}. "
                "Ensure the bridge and Minecraft server are running."
            ) from exc

        if not data.get("bot_spawned"):
            raise TargetConnectionError(
                f"mineflayer bot not spawned at {bridge_url}. "
                "Wait for bridge to finish connecting."
            )

        # Dynamic action types from bridge capabilities
        caps = data.get("capabilities") or data.get("actions")
        if isinstance(caps, list) and caps:
            self._valid_action_types = frozenset(caps)
            logger.info("Loaded %d action types from bridge", len(caps))

        self._built = True
        logger.info("MinecraftTarget connected to bridge at %s", bridge_url)

    def reset(self, session_ctx: dict[str, Any]) -> dict[str, Any]:
        if not self._built:
            raise TargetResetError("target not built")
        time.sleep(self._step_delay)
        return super().reset(session_ctx)

    def observe(self) -> dict[str, Any]:
        client = self._get_http()
        try:
            resp = client.get(f"{self._bridge_url}/state")
            data = resp.json()
        except Exception as exc:
            logger.warning("state fetch failed: %s", exc)
            return self._cached_obs()

        bot_data = data.get("bot") or {}
        pos = bot_data.get("position", {})
        rot = bot_data.get("rotation", {})
        self._position = np.array([
            float(pos.get("x", 0)),
            float(pos.get("y", 0)),
            float(pos.get("z", 0)),
        ], dtype=np.float64)
        self._yaw = float(rot.get("yaw", self._yaw))
        self._pitch = float(rot.get("pitch", self._pitch))
        self._health = float(data.get("health", self._health))
        self._hunger = int(data.get("hunger", self._hunger))
        self._on_ground = bool(bot_data.get("on_ground", self._on_ground))
        self._dimension = str(data.get("dimension", self._dimension))

        state = np.array([
            self._position[0], self._position[1], self._position[2],
            self._yaw, self._pitch,
            self._health, float(self._hunger), float(self._held_slot),
        ], dtype=np.float32)

        return {
            "image": np.zeros((224, 224, 3), dtype=np.uint8),
            "state": state,
            "nearby_blocks": data.get("nearby_blocks", []),
            "nearby_entities": data.get("nearby_entities", []),
            "players": data.get("players", []),
            "inventory": data.get("inventory", {}),
            "inventory_items": data.get("inventory_items", []),
            "last_chats": data.get("last_chats", []),
            "info": {
                "position": {"x": float(self._position[0]), "y": float(self._position[1]), "z": float(self._position[2])},
                "rotation": {"yaw": self._yaw, "pitch": self._pitch},
                "on_ground": self._on_ground,
                "dimension": self._dimension,
                "health": self._health,
                "hunger": self._hunger,
                "world": data.get("world", {}),
                "player_list": data.get("player_list", []),
                "inventory_items": data.get("inventory_items", []),
            },
        }

    def _cached_obs(self) -> dict[str, Any]:
        state = np.array([
            self._position[0], self._position[1], self._position[2],
            self._yaw, self._pitch,
            self._health, float(self._hunger), float(self._held_slot),
        ], dtype=np.float32)
        return {
            "image": np.zeros((224, 224, 3), dtype=np.uint8),
            "state": state,
            "nearby_blocks": [],
            "nearby_entities": [],
            "inventory": {},
            "inventory_items": [],
            "last_chats": [],
            "info": {
                "position": {"x": float(self._position[0]), "y": float(self._position[1]), "z": float(self._position[2])},
                "rotation": {"yaw": self._yaw, "pitch": self._pitch},
                "on_ground": self._on_ground,
                "dimension": self._dimension,
            },
        }

    def step(self, action: Any) -> dict[str, Any]:
        if not self._built:
            raise TargetStepError("target not built")
        if isinstance(action, dict):
            action_type = action.get("type", "")
            params = action.get("params", {})
        else:
            raise TargetStepError(f"action must be a dict, got {type(action).__name__}")

        if action_type not in self._current_action_types:
            raise TargetStepError(f"unknown action type: {action_type}")

        result = self._post_action(action_type, params)
        time.sleep(self._step_delay)
        self._step_idx += 1

        if action_type == "select_slot":
            slot = int(params.get("slot", 0))
            self._held_slot = max(0, min(8, slot))

        obs = self.observe()
        done = self._check_done()
        action_ok = result.get("ok", False)
        info = {
            "ok": action_ok,
            "step_idx": self._step_idx,
            "result": result.get("result", ""),
            "action_type": action_type,
        }
        # Forward enriched feedback from bridge (block, position, dist_to_goal, goal)
        for key in ("block", "position", "dist_to_goal", "goal"):
            val = result.get(key)
            if val is not None:
                info[key] = val
        return {
            "obs": obs,
            "reward": 0.0,
            "done": done,
            "info": info,
        }

    def _post_action(self, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._get_http()
        # Action may take up to ACTION_TIMEOUT_MS (default 20s) + queue delay
        action_timeout = float(self.config.get("action_timeout", 60.0))
        try:
            resp = client.post(
                f"{self._bridge_url}/action",
                json={"type": action_type, "params": params},
                timeout=action_timeout,
            )
            return resp.json()
        except Exception as exc:
            logger.warning("action post failed (type=%s): %s", action_type, exc)
            return {"ok": False, "result": str(exc)}

    def block_query(self, x: int, y: int, z: int) -> dict[str, Any] | None:
        """Query a specific block at world coordinates (x, y, z).

        Returns the block dict or None if not found / air.
        Uses GET /block?x=&y=&z= endpoint on the bridge.
        """
        client = self._get_http()
        try:
            resp = client.get(
                f"{self._bridge_url}/block",
                params={"x": x, "y": y, "z": z},
                timeout=5.0,
            )
            data = resp.json()
            if data.get("ok") and data.get("block"):
                return data["block"]
            return None
        except Exception as exc:
            logger.warning("block query failed (%d,%d,%d): %s", x, y, z, exc)
            return None

    def inventory_query(self) -> dict[str, Any]:
        """Query full bot inventory via GET /inventory on the bridge.

        Returns dict with keys: ok, slots (list), by_name (dict: name→count), total_unique.
        """
        client = self._get_http()
        try:
            resp = client.get(f"{self._bridge_url}/inventory", timeout=5.0)
            return resp.json()
        except Exception as exc:
            logger.warning("inventory query failed: %s", exc)
            return {"ok": False, "error": str(exc), "by_name": {}, "slots": [], "total_unique": 0}

    def best_tool_query(self, block_name: str) -> dict[str, Any]:
        """Query the best harvest tool for a block via the bridge data endpoint.

        Returns dict with: ok, block, best_tool, held_tool, has_tool.
        Falls back to built-in block→tool mapping if bridge unavailable.
        """
        client = self._get_http()
        try:
            resp = client.get(
                f"{self._bridge_url}/best-tool",
                params={"block": block_name},
                timeout=5.0,
            )
            return resp.json()
        except Exception as exc:
            logger.warning("best-tool query failed (%s): %s, using built-in map", block_name, exc)
            return self._fallback_best_tool(block_name)

    @staticmethod
    def _fallback_best_tool(block_name: str) -> dict[str, Any]:
        """Built-in block→tool mapping — decoupled from bridge, runs offline."""
        tool_map = {
            "stone": "wooden_pickaxe", "cobblestone": "wooden_pickaxe",
            "granite": "wooden_pickaxe", "diorite": "wooden_pickaxe", "andesite": "wooden_pickaxe",
            "iron_ore": "stone_pickaxe", "gold_ore": "iron_pickaxe",
            "coal_ore": "wooden_pickaxe", "diamond_ore": "iron_pickaxe",
            "oak_log": "wooden_axe", "spruce_log": "wooden_axe",
            "birch_log": "wooden_axe", "jungle_log": "wooden_axe",
            "dark_oak_log": "wooden_axe", "acacia_log": "wooden_axe",
            "oak_planks": "wooden_axe", "spruce_planks": "wooden_axe",
            "dirt": "wooden_shovel", "grass_block": "wooden_shovel",
            "sand": "wooden_shovel", "gravel": "wooden_shovel", "snow": "wooden_shovel",
            "cobweb": "wooden_sword",
        }
        best = tool_map.get(block_name)
        return {"ok": True, "block": block_name, "best_tool": best, "held_tool": None, "has_tool": None, "source": "builtin"}

    def _check_done(self) -> bool:
        return False

    def cancel(self, reason: str) -> None:
        logger.info("MinecraftTarget cancelled: %s", reason)
        self._last_status = {"status": "cancelled", "reason": reason}

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None
        self._built = False
        logger.info("MinecraftTarget disconnected")

    def write_environment_snapshot(self, env_path: Path) -> None:
        """Write Minecraft terrain (nearby_blocks by y-level) to ENVIRONMENT.md."""
        try:
            obs = self.observe()
        except Exception:
            logger.warning("Failed to observe for terrain snapshot")
            return
        blocks = obs.get("nearby_blocks")
        if not isinstance(blocks, list):
            return
        info = obs.get("info", {})
        bot_pos = info.get("position", {})
        bot_y = float(bot_pos.get("y", 0))
        terrain = {
            "bot": {
                "x": round(float(bot_pos.get("x", 0)), 1),
                "y": round(bot_y, 1),
                "z": round(float(bot_pos.get("z", 0)), 1),
                "dimension": info.get("dimension", "overworld"),
                "on_ground": info.get("on_ground", True),
                "health": info.get("health", 20),
            },
            "nearby_blocks_summary": _summarize_terrain(blocks, bot_y),
        }
        if env_path.exists():
            try:
                existing = env_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""
        else:
            existing = ""
        marker = "\n## Terrain Snapshot\n"
        idx = existing.rfind(marker)
        if idx >= 0:
            existing = existing[:idx]
        terrain_text = marker + "```json\n" + json.dumps(terrain, indent=2, ensure_ascii=False) + "\n```\n"
        env_path.write_text(existing + terrain_text, encoding="utf-8")


def _summarize_terrain(blocks: list, bot_y: float) -> dict:
    """Group nearby_blocks by y-level with compact summaries for LLM consumption."""
    by_y: dict[int, Counter] = {}
    for b in blocks:
        if not isinstance(b, dict):
            continue
        pos = b.get("position", {})
        if not isinstance(pos, dict):
            continue
        y = int(round(float(pos.get("y", 0))))
        name = str(b.get("name", "unknown"))
        by_y.setdefault(y, Counter())[name] += 1

    layers: list[dict] = []
    bot_y_int = int(round(bot_y))
    for y_level in sorted(by_y.keys(), reverse=True):
        counter = by_y[y_level]
        total = sum(counter.values())
        if total == 0:
            continue
        most = counter.most_common(8)
        desc = ", ".join(f"{name}×{count}" if count > 1 else name for name, count in most)
        if len(counter) > 8:
            desc += f", +{len(counter) - 8} more types"
        tag = ""
        if y_level == bot_y_int:
            tag = " ← BOT"
        elif y_level > bot_y_int:
            tag = " (above)"
        else:
            tag = " (below)"
        layers.append({
            "y": y_level,
            "total_blocks": total,
            "blocks": desc,
            "tag": tag,
        })

    above_bot = sum(c.total() for y, c in by_y.items() if y > bot_y_int)
    below_bot = sum(c.total() for y, c in by_y.items() if y < bot_y_int)
    return {
        "radius": _estimate_radius(blocks),
        "total_non_air_blocks": sum(sum(c.values()) for c in by_y.values()),
        "layers": layers,
        "blocks_above_bot": above_bot,
        "blocks_below_bot": below_bot,
    }


def _estimate_radius(blocks: list) -> int:
    if not blocks:
        return 0
    import math
    max_dist = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        pos = b.get("position", {})
        if not isinstance(pos, dict):
            continue
        x = float(pos.get("x", 0))
        z = float(pos.get("z", 0))
        dist = int(math.ceil(math.sqrt(x * x + z * z)))
        if dist > max_dist:
            max_dist = dist
    return max_dist if max_dist > 0 else 5
