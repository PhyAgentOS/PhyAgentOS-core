"""Minecraft game agent CLI commands."""

from __future__ import annotations

import asyncio
import json
import os
import time

import typer
from rich.console import Console

console = Console()

minecraft_app = typer.Typer(help="Minecraft game agent demo")

_MC_SYSTEM_PROMPT = (
    "你是 Minecraft 机器人控制器。将用户指令转为层级化任务计划 JSON。\n\n"
    "## 输出格式：TaskPlan\n"
    "{\n"
    '  "goal": "任务描述",\n'
    '  "subgoals": [{\n'
    '    "id": "唯一ID",\n'
    '    "name": "子目标名",\n'
    '    "depends_on": ["依赖的子目标ID"],\n'
    '    "precheck": ["前置条件断言"],\n'
    '    "postcheck": ["后验证断言"],\n'
    '    "tasks": [{\n'
    '      "id": "唯一ID",\n'
    '      "name": "任务名",\n'
    '      "preconditions": ["断言列表"],\n'
    '      "actions": [{"type":"动作类型","params":{}}],\n'
    '      "verify": ["断言列表"],\n'
    '      "on_fail": "retry|skip|abort",\n'
    '      "max_retries": 3\n'
    '    }]\n'
    '  }]\n'
    "}\n\n"
    "## 可用动作\n"
    "move: {forward:N} 沿面朝方向走N步（负值后退）\n"
    "   或 {dx,dy,dz,absolute:true} 走到绝对坐标\n"
    "   或 {target:\"player\"/\"pig\"/...} 追踪实体\n"
    "look: {yaw,pitch} 角度制（0=南 90=西 180=北 -90=东）\n"
    "jump: {} 跳跃\n"
    "chat: {message} 在游戏里说话\n"
    "dig: {x,y,z} 挖绝对坐标的方块\n"
    "place: {x,y,z,face} 面编号0=下1=上2=北3=南4=西5=东\n"
    "collect: {block_type,count} 自动寻找并采集\n"
    "craft: {recipe_id,count} 合成（需附近有工作台）\n"
    "smelt: {input,fuel,count} 使用附近熔炉烧炼\n"
    "select_slot: {slot:0-8} 切换快捷键\n"
    "equip: {item,destination:\"hand\"|\"torso\"|...} 装备指定物品（如 {\"item\":\"wooden_shovel\"}）\n"
    "drop: {slot} 丢弃物品\n"
    "attack: {entity_id} 或 {target_type} 攻击实体\n"
    "interact: {entity_id} 与实体交互\n"
    "use: {} 使用手持物品\n"
    "sneak: {start:true|false}\n"
    "sprint: {start:true|false}\n\n"
    "## 断言格式（precheck/preconditions/verify/postcheck）\n"
    "has_item:物品名        — 背包里至少有1个\n"
    "has_item:物品名×N      — 背包里至少有N个\n"
    "block_at:x,y,z,物品名  — 指定坐标有指定方块\n"
    "block_at:x,y,z        — 指定坐标有非空气方块\n"
    "block_near:物品名,距离  — 附近指定距离内存在该方块\n"
    "bot_near:x,y,z,距离    — bot在坐标附近指定距离内\n\n"
    "## 规则\n"
    "1. 子目标按依赖顺序排列（depends_on 引用前置子目标ID）\n"
    "2. 每个能独立完成的操作拆为独立子目标\n"
    "3. 涉及坐标时：已知用绝对坐标，未知用相对目标（如 collect 不需要坐标）\n"
    "4. 放置方块前必须确保 bot 已走到目标旁（bot 坐标与目标坐标差值 < 3）\n"
    "5. 挖/放方块后用 block_at 做 verify\n"
    "6. 合成物品前必须验证材料足够（用 has_item 做 precheck）\n"
    "7. ⚠️ dig 前必须先 equip 正确工具（铲→泥土/沙子/砂砾，镐→石头，斧→木头/木板，剑→蜘蛛网）\n"
    "8. dig 时 bot 必须站在目标方块 4.5 格内（bot_near precheck）\n"
    "9. on_fail: 可重试用 retry，无解用 skip，致命用 abort\n"
    "10. 只返回 JSON，不要额外文字\n\n"
    '## 示例：建造工作台\n'
    '{"goal":"建造工作台","subgoals":[{"id":"sg1","name":"走到空地","tasks":['
    '{"id":"t1","name":"向前走5步","preconditions":[],'
    '"actions":[{"type":"move","params":{"forward":5}}],"verify":["bot_near:0,65,0,3"],'
    '"on_fail":"retry","max_retries":2}]},{"id":"sg2","name":"放置工作台",'
    '"depends_on":["sg1"],"precheck":["has_item:crafting_table"],'
    '"tasks":[{"id":"t2","name":"放工作台在地面","preconditions":["bot_near:0,64,0,3"],'
    '"actions":[{"type":"place","params":{"x":0,"y":64,"z":0,"face":1}}],'
    '"verify":["block_at:0,65,0,crafting_table"],"on_fail":"retry","max_retries":3}]}]}\n'
    '## 示例：清理5x5区域（含equip）\n'
    '{"goal":"清理5x5地面区域","subgoals":[{'
    '"id":"sg1","name":"装备铲子","precheck":["has_item:wooden_shovel"],'
    '"tasks":[{"id":"t1","name":"持铲子","actions":[{"type":"equip","params":{"item":"wooden_shovel"}}],'
    '"verify":[],"on_fail":"skip","max_retries":1}]},{'
    '"id":"sg2","name":"走到清理起点","depends_on":["sg1"],'
    '"tasks":[{"id":"t2","name":"走到(-75,63,-110)","preconditions":[],'
    '"actions":[{"type":"move","params":{"dx":-75,"dy":63,"dz":-110,"absolute":true}}],'
    '"verify":["bot_near:-75,63,-110,3"],"on_fail":"retry","max_retries":3}]},{'
    '"id":"sg3","name":"挖除杂物","depends_on":["sg2"],'
    '"tasks":[{"id":"t3","name":"挖(-75,63,-110)","preconditions":["bot_near:-75,63,-110,3"],'
    '"actions":[{"type":"dig","params":{"x":-75,"y":63,"z":-110}}],'
    '"verify":["block_at:-75,63,-110"],"on_fail":"retry","max_retries":3}]}]}'
)


@minecraft_app.command("say")
def minecraft_say(
    instruction: str = typer.Argument(help="自然语言指令，如 '挖5个橡木'"),
    bridge_url: str = typer.Option(
        "https://carucated-kattie-cryptogamic.ngrok-free.dev",
        "--url", "-u", help="Bridge HTTP API URL",
    ),
):
    """用自然语言控制 Minecraft bot"""
    from PhyAgentOS.cli.commands import _load_runtime_config, _make_provider  # noqa: E402

    config = _load_runtime_config()
    provider = _make_provider(config)

    console.print("[dim]Paos 思考中...[/dim]")

    async def _ask():
        resp = await provider.chat_with_retry(messages=[
            {"role": "system", "content": _MC_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ])
        return resp.content.strip()

    raw = asyncio.run(_ask())

    # Parse LLM output: could be TaskPlan or flat action list
    try:
        if "```" in raw:
            plan_raw = raw.split("```")[1]
            if plan_raw.startswith("json"):
                plan_raw = plan_raw[4:]
            plan = json.loads(plan_raw)
        else:
            plan = json.loads(raw)
    except Exception:
        console.print(f"[red]LLM 返回格式错误: {raw[:200]}[/red]")
        raise typer.Exit(1)

    is_task_plan = isinstance(plan, dict) and "subgoals" in plan
    if is_task_plan:
        subgoal_count = len(plan.get("subgoals", []))
        total_tasks = sum(len(sg.get("tasks", [])) for sg in plan.get("subgoals", []))
        console.print(f"[dim]-> 生成任务计划: {subgoal_count} 子目标, {total_tasks} 任务[/dim]")
        for sg in plan.get("subgoals", []):
            deps = f" (依赖: {','.join(sg.get('depends_on',[]))})" if sg.get("depends_on") else ""
            console.print(f"  [{sg.get('id','?')}] {sg.get('name','?')}{deps}")
            for t in sg.get("tasks", []):
                acts = ", ".join(f"{a['type']}: {a.get('params',{})}" for a in t.get("actions", []))
                console.print(f"    ├─ {t.get('name','?')}: {acts}")
        # Wrap TaskPlan as single-element queries list for backward compat
        action_plan = [plan]
    else:
        if not isinstance(plan, list):
            raise ValueError("not a list or TaskPlan")
        console.print(f"[dim]-> 生成 {len(plan)} 步动作[/dim]")
        for i, a in enumerate(plan):
            console.print(f"  {i+1}. {a['type']}: {a.get('params', {})}")
        action_plan = plan

    from PhyAgentOS.runtime.adapters.minecraft.minecraft_adapter import MinecraftTargetAdapter
    from PhyAgentOS.runtime.schemas import AdapterPlan, SessionSpec
    from PhyAgentOS.runtime.skillruntime.game.minecraft_skill_runtime import MinecraftSkillRuntime
    from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

    target = MinecraftTarget({"bridge_url": bridge_url, "verify_ssl": False})
    # For TaskPlan, estimate steps from total actions; for flat list, use list length
    if is_task_plan:
        total_actions = sum(
            len(t.get("actions", []))
            for sg in plan.get("subgoals", [])
            for t in sg.get("tasks", [])
        )
        max_steps = total_actions * 3 + 10  # buffer for retries
    else:
        max_steps = len(action_plan) + 5
    session = SessionSpec(
        session_id=f"sess_cli_{os.urandom(3).hex()}",
        target_ref="target://minecraft_java_env",
        skillruntime_ref="skillruntime://minecraft_navigate",
        task_description=instruction,
        execution={"max_steps": max_steps},
        runtime_hints={"perception_queries": action_plan},
    )

    console.print()
    result = MinecraftSkillRuntime().run(
        session, target, MinecraftTargetAdapter(),
        None, [], None,
        AdapterPlan(target_adapter="target_adapter://minecraft_adapter"),
    )
    console.print(f"\n[green]完成: {result.num_steps} 步, status={result.status}[/green]")


@minecraft_app.command("listen")
def minecraft_listen(
    bridge_url: str = typer.Option(
        "https://carucated-kattie-cryptogamic.ngrok-free.dev",
        "--url", "-u", help="Bridge HTTP API URL",
    ),
    poll_interval: float = typer.Option(
        3.0, "--interval", "-i", help="轮询间隔（秒）",
    ),
    prefix: str = typer.Option(
        "paos", "--prefix", "-p", help="游戏内指令前缀",
    ),
):
    """监听 Minecraft 聊天，自动响应带前缀的消息。新指令可打断当前执行的任务。"""
    import threading as _thr  # noqa: E402

    from PhyAgentOS.cli.commands import _load_runtime_config, _make_provider  # noqa: E402

    config = _load_runtime_config()
    provider = _make_provider(config)

    from PhyAgentOS.runtime.adapters.minecraft.minecraft_adapter import MinecraftTargetAdapter
    from PhyAgentOS.runtime.schemas import AdapterPlan, SessionSpec
    from PhyAgentOS.runtime.skillruntime.game.minecraft_skill_runtime import (
        InterruptedError,
        MinecraftSkillRuntime,
    )
    from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget
    from PhyAgentOS.runtime.watchdog.errors import TargetConnectionError

    target = MinecraftTarget({"bridge_url": bridge_url, "verify_ssl": False})
    try:
        target.build()
    except TargetConnectionError as e:
        console.print(f"[red]连接失败: {e}[/red]")
        raise typer.Exit(1)

    # Shared state for the background agent thread
    agent_runtime: MinecraftSkillRuntime | None = None
    agent_thread: _thr.Thread | None = None
    agent_lock = _thr.Lock()

    def _run_agent_async(instruction: str):
        """Run an agent in a background thread. Sets global state so the main
        loop can detect and cancel it when a new instruction arrives."""
        nonlocal agent_runtime, agent_thread
        with agent_lock:
            runtime = MinecraftSkillRuntime()
            agent_runtime = runtime
            agent_thread = _thr.current_thread()

        try:
            # ── LLM plan generation (same thread, blocks briefly) ──
            async def _ask():
                resp = await provider.chat_with_retry(messages=[
                    {"role": "system", "content": _MC_SYSTEM_PROMPT},
                    {"role": "user", "content": instruction},
                ])
                return resp.content.strip()

            raw = asyncio.run(_ask())
            plan = _parse_plan(raw)
            is_task_plan = isinstance(plan, dict) and "subgoals" in plan

            if is_task_plan:
                subgoal_count = len(plan.get("subgoals", []))
                total_tasks = sum(len(sg.get("tasks", [])) for sg in plan.get("subgoals", []))
                console.print(f"  → 任务计划: {subgoal_count} 子目标, {total_tasks} 任务")
                for sg in plan.get("subgoals", []):
                    console.print(f"    [{sg.get('id','?')}] {sg.get('name','?')}")
                action_plan = [plan]
                total_actions = sum(
                    len(t.get("actions", []))
                    for sg in plan.get("subgoals", [])
                    for t in sg.get("tasks", [])
                )
                max_steps = total_actions * 3 + 10
            else:
                console.print(f"  → 生成 {len(plan)} 步动作")
                for i, a in enumerate(plan):
                    console.print(f"    {i+1}. {a['type']}: {a.get('params', {})}")
                action_plan = plan
                max_steps = len(plan) + 5

            # Check if cancelled before starting execution
            if runtime._cancelled.is_set():
                console.print("  [yellow]任务已取消（计划阶段）[/yellow]")
                return

            session = SessionSpec(
                session_id=f"sess_chat_{os.urandom(3).hex()}",
                target_ref="target://minecraft_java_env",
                skillruntime_ref="skillruntime://minecraft_navigate",
                task_description=instruction,
                execution={"max_steps": max_steps},
                runtime_hints={"perception_queries": action_plan},
            )
            result = runtime.run(
                session, target, MinecraftTargetAdapter(),
                None, [], None,
                AdapterPlan(target_adapter="target_adapter://minecraft_adapter"),
            )
            console.print(f"  [green]完成: {result.num_steps} 步, status={result.status}[/green]")
        except InterruptedError:
            console.print("  [yellow]任务被中断[/yellow]")
        except Exception as e:
            console.print(f"  [red]执行异常: {e}[/red]")
        finally:
            with agent_lock:
                agent_runtime = None
                agent_thread = None

    console.print(f"[green]✓[/green] 已连接 bridge，监听游戏聊天中... (前缀: {prefix}, 间隔: {poll_interval}s)")
    console.print("[dim]在游戏里说 'paos 挖5个橡木' 即可触发[/dim]")
    console.print("[dim]说 'paos stop' 取消当前任务[/dim]")
    console.print("[dim]Ctrl+C 停止[/dim]\n")

    seen: set[str] = set()
    first_poll = True

    try:
        while True:
            obs = target.observe()
            chats = obs.get("last_chats", [])
            if not isinstance(chats, list):
                time.sleep(poll_interval)
                continue

            if first_poll:
                first_poll = False
                for c in chats:
                    if isinstance(c, dict):
                        key = f"{c.get('username','')}:{c.get('message','')}:{c.get('time',0)}"
                        seen.add(key)
                console.print("[dim]已跳过历史消息，等待新指令...[/dim]")
                time.sleep(poll_interval)
                continue

            for c in chats:
                if not isinstance(c, dict):
                    continue
                username = c.get("username", "")
                message = c.get("message", "")
                chat_time = c.get("time", 0)
                key = f"{username}:{message}:{chat_time}"
                if key in seen:
                    continue
                seen.add(key)

                if str(username).lower() == "paos":
                    continue

                stripped = message.strip()
                if not stripped.lower().startswith(prefix.lower()):
                    continue

                instruction = stripped[len(prefix):].strip()
                if not instruction:
                    continue

                # "stop" command: cancel current agent
                if instruction.lower() == "stop":
                    with agent_lock:
                        rt = agent_runtime
                    if rt is not None:
                        rt.cancel(reason="stop command from chat")
                        console.print("  [yellow]已发送取消指令，等待当前任务结束...[/yellow]")
                        with agent_lock:
                            t = agent_thread
                        if t is not None and t.is_alive():
                            t.join(timeout=10.0)
                        console.print("  [green]任务已取消[/green]")
                    else:
                        console.print("  [dim]没有正在执行的任务[/dim]")
                    continue

                console.print(f"[游戏] <{username}> {message}")

                # Cancel any running agent before starting new one
                with agent_lock:
                    rt = agent_runtime
                    t = agent_thread
                if rt is not None:
                    rt.cancel(reason=f"new instruction: {instruction}")
                    console.print("  [yellow]中断当前任务...[/yellow]")
                    if t is not None and t.is_alive():
                        t.join(timeout=15.0)

                # Start new agent in background thread
                thr = _thr.Thread(
                    target=_run_agent_async, args=(instruction,), daemon=True,
                )
                thr.start()

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        with agent_lock:
            rt = agent_runtime
            t = agent_thread
        if rt is not None:
            rt.cancel(reason="Ctrl+C")
            if t is not None and t.is_alive():
                t.join(timeout=5.0)
        target.close()
        console.print("\n已停止")


def _parse_plan(raw: str):
    """Parse LLM output into plan dict or list."""
    if "```" in raw:
        plan_raw = raw.split("```")[1]
        if plan_raw.startswith("json"):
            plan_raw = plan_raw[4:]
        plan = json.loads(plan_raw)
    else:
        plan = json.loads(raw)
    return plan


@minecraft_app.command("tp")
def minecraft_tp(
    x: float = typer.Argument(help="X 坐标"),
    y: float = typer.Argument(help="Y 坐标"),
    z: float = typer.Argument(help="Z 坐标"),
    bridge_url: str = typer.Option(
        "https://carucated-kattie-cryptogamic.ngrok-free.dev",
        "--url", "-u", help="Bridge HTTP API URL",
    ),
):
    """传送 bot 到指定坐标"""
    from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget
    from PhyAgentOS.runtime.watchdog.errors import TargetConnectionError

    target = MinecraftTarget({"bridge_url": bridge_url, "verify_ssl": False})
    try:
        target.build()
    except TargetConnectionError as e:
        console.print(f"[red]连接失败: {e}[/red]")
        raise typer.Exit(1)

    target.step({"type": "move", "params": {"dx": x, "dy": y, "dz": z, "absolute": True}})
    console.print(f"[green]✓[/green] bot 已传送到 ({x}, {y}, {z})")
    target.close()


@minecraft_app.command("warmup")
def minecraft_warmup(
    output_dir: str = typer.Option(..., "--output-dir", "-o", help="Skill Graph 输出根目录"),
    bridge_url: str = typer.Option("http://127.0.0.1:3001", "--url", "-u"),
):
    """固定运行 W01-W07，每个任务一个 trial，并保存冻结图谱和 benchmark 可写副本。"""
    from PhyAgentOS.game_agents.minecraft import run_warmup
    from PhyAgentOS.runtime.benchmark.minecraft_glue import MinecraftTargetWorldAdapter
    from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

    target = MinecraftTarget({"bridge_url": bridge_url, "verify_ssl": False})
    try:
        target.build()
        result = run_warmup(MinecraftTargetWorldAdapter(target), output_dir)
    except Exception as exc:
        console.print(f"[red]预热失败: {exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        target.close()
    console.print_json(data=result)


@minecraft_app.command("benchmark")
def minecraft_benchmark(
    output_dir: str = typer.Option(..., "--output-dir", "-o", help="episode 结果目录"),
    graph_dir: str = typer.Option(..., "--graph-dir", help="预热产生的 benchmark_graph 目录"),
    tasks: str = typer.Option("wooden.obtain_oak_log", "--tasks", help="逗号分隔 task id"),
    all_tasks: bool = typer.Option(False, "--all", help="运行 manifest 中全部任务"),
    trials: int = typer.Option(1, "--trials", min=1),
    run_id: str | None = typer.Option(None, "--run-id", help="可复现的批次 ID；默认自动生成"),
    bridge_url: str = typer.Option("http://127.0.0.1:3001", "--url", "-u"),
):
    """串行执行 benchmark，并在每个 episode 后同步沉淀 Skill Graph。"""
    from PhyAgentOS.game_agents.minecraft import (
        build_scripted_agent,
        run_benchmark_tasks,
    )
    from PhyAgentOS.benchmarks.minecraft.techtree import list_tasks
    from PhyAgentOS.runtime.benchmark.minecraft_glue import MinecraftTargetWorldAdapter
    from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

    task_ids = (
        [task.id for task in list_tasks()]
        if all_tasks
        else [task.strip() for task in tasks.split(",") if task.strip()]
    )
    if not task_ids:
        raise typer.BadParameter("--tasks must contain at least one task id")
    target = MinecraftTarget({"bridge_url": bridge_url, "verify_ssl": False})
    try:
        target.build()
        results = run_benchmark_tasks(
            task_ids,
            build_scripted_agent,
            MinecraftTargetWorldAdapter(target),
            graph_dir=graph_dir,
            results_dir=output_dir,
            trials=trials,
            run_id=run_id,
        )
    except Exception as exc:
        console.print(f"[red]benchmark 失败: {exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        target.close()
    passed = sum(result.success for result in results)
    actual_run_id = results[0].metadata.get("run_id") if results else run_id
    console.print(
        f"[green]完成[/green]: {passed}/{len(results)} episodes passed (run_id={actual_run_id})"
    )
