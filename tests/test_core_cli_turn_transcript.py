"""Permanent turn transcript, boundaries, and DSML protocol-text protection.

The transcript is display-only. It shows the full request, one OS start rule,
the reply, notices and usage, then exactly one end rule. Unparsed DSML tool
call text is removed from display; nothing here executes or rewrites events.
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.types import TurnResult, TurnStatus
from PhyAgentOS.cli.interaction.agent_output import (
    AgentOutput,
    _ProtocolTextFilter,
)
from PhyAgentOS.cli.interaction.terminal_display import TerminalDisplay

_NOTICE = "模型返回了无法解析的工具调用文本；这段文本未作为工具调用执行"


def _output(*, markdown: bool = False, width: int = 100):
    buffer = StringIO()
    console = Console(file=buffer, width=width, color_system=None)
    return AgentOutput(console, markdown=markdown), buffer


def _event(kind, *, session="s", turn="t1", **payload):
    return RuntimeEvent(
        type=kind,
        session_id=session,
        turn_id=turn,
        step_id=None if kind in {
            EventType.TURN_STARTED,
            EventType.TURN_COMPLETED,
            EventType.TURN_FAILED,
            EventType.TURN_INTERRUPTED,
        } else "step",
        payload=payload,
    )


def _usage(output: AgentOutput, *, session="s", turn="t1", call="m1", content=""):
    output.event(
        _event(
            EventType.MODEL_ATTEMPT_COMPLETED,
            session=session,
            turn=turn,
            model_call_id=call,
            content=content,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
    )


def _delta(output: AgentOutput, content: str, *, session="s", turn="t1", call="m1"):
    output.event(
        _event(
            EventType.MODEL_RESPONSE_DELTA,
            session=session,
            turn=turn,
            model_call_id=call,
            content=content,
        )
    )


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


def test_two_turns_keep_each_full_request_once_and_order_both_boundaries():
    output, buffer = _output()
    first = "第一轮完整请求 [red] 不被当成 markup"
    second = "第二轮完整请求\n第二行也保留"

    output.turn_started("s", request_text=first)
    _usage(output, turn="t1", content="第一轮回答")
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply="第一轮回答"))
    output.turn_finished()
    output.turn_finished()  # repeated close must not print twice

    output.turn_started("s", request_text=second)
    _usage(output, turn="t2", content="第二轮回答")
    output.result(TurnResult("s", "t2", TurnStatus.COMPLETED, reply="第二轮回答"))
    output.turn_finished()

    text = buffer.getvalue()
    assert text.count(first) == 1
    assert text.count("第二行也保留") == 1
    assert text.count("PhyAgentOS · Turn 1") == 1
    assert text.count("PhyAgentOS · Turn 2") == 1
    assert text.count("End Turn 1") == 1
    assert text.count("End Turn 2") == 1
    assert "🍞 PhyAgentOS:" not in text

    assert text.index(first) < text.index("PhyAgentOS · Turn 1")
    assert text.index("PhyAgentOS · Turn 1") < text.index("第一轮回答")
    assert text.index("第一轮回答") < text.index("End Turn 1")
    assert text.index("End Turn 1") < text.index("第二轮完整请求")
    assert text.index("第二轮完整请求") < text.index("第二行也保留")
    assert text.index("第二行也保留") < text.index("PhyAgentOS · Turn 2")
    assert text.index("第二轮回答") < text.index("End Turn 2")

    lines = _lines(text)
    assert f"│ {first}" in lines
    assert "│ 第二轮完整请求" in lines
    assert "第二行也保留" in lines
    assert "You:" not in text


def test_dividers_fill_the_console_width_with_a_centered_title():
    output, buffer = _output(width=60)

    output.turn_started("s", request_text="request")
    output.turn_finished()

    lines = buffer.getvalue().splitlines()
    start = next(line for line in lines if "PhyAgentOS · Turn 1" in line)
    end = next(line for line in lines if "End Turn 1" in line)
    for line, title in (
        (start, "PhyAgentOS · Turn 1"),
        (end, "End Turn 1"),
    ):
        # A rule, not centered text: it starts at the left edge and reaches the
        # console width, with the title centered between two segments.
        assert len(line) == 60
        assert line.startswith("─") and line.endswith("─")
        left = line.index(title)
        right = len(line) - left - len(title)
        assert left > 0 and right > 0
        assert abs(left - right) <= 1
    assert "\x1b" not in buffer.getvalue()


def test_dividers_stay_readable_on_a_narrow_console():
    output, buffer = _output(width=20)

    output.turn_started("s", request_text="request")
    output.turn_finished()

    rules = [
        line
        for line in buffer.getvalue().splitlines()
        if line.startswith("─")
    ]
    assert len(rules) == 2
    for line in rules:
        assert len(line) <= 20
        assert line.endswith("─")


def test_request_is_one_marker_and_keeps_every_line():
    output, buffer = _output(width=50)
    request = "第一行\n第二行 [red]\n第三行"

    output.turn_started("s", request_text=request)

    text = buffer.getvalue()
    before_divider = text.split("PhyAgentOS · Turn 1")[0]
    assert before_divider.count("│") == 1
    for line in request.splitlines():
        assert text.count(line) == 1
    assert "You:" not in text


def test_long_request_is_wrapped_not_truncated():
    output, buffer = _output(width=30)
    request = "完整的很长请求" * 12

    output.turn_started("s", request_text=request)

    # Strip the block padding and line breaks; every character of the request
    # must survive the wrap.
    compact = "".join(buffer.getvalue().split())
    assert compact.count(request) == 1
    assert "..." not in buffer.getvalue() and "…" not in buffer.getvalue()


def test_request_block_has_a_bar_and_background_on_a_terminal():
    buffer = StringIO()
    console = Console(
        file=buffer,
        width=40,
        force_terminal=True,
        color_system="standard",
    )
    output = AgentOutput(console, markdown=False)

    output.turn_started("s", request_text="hello [red]\nsecond line")

    text = buffer.getvalue()
    assert "│" in text and "You:" not in text
    assert "\x1b[1;36;100m│" in text
    assert "\x1b[100m" in text
    assert "hello [red]" in text and "second line" in text


def test_usage_and_error_print_before_the_single_end_rule():
    output, buffer = _output()

    output.turn_started("s", request_text="失败请求")
    _usage(output, turn="t1", content="")
    output.result(
        TurnResult("s", "t1", TurnStatus.FAILED, reply=None, error="provider exploded")
    )
    output.turn_finished()
    output.turn_finished()

    text = buffer.getvalue()
    assert text.count("End Turn 1") == 1
    assert text.index("Turn failed") < text.index("End Turn 1")
    assert text.index("本轮") < text.index("End Turn 1")
    assert text.index("provider exploded") < text.index("End Turn 1")


def test_streamed_reply_is_not_printed_twice_and_ends_after_usage():
    output, buffer = _output()
    reply = "streamed answer once"

    output.turn_started("s", request_text="hello")
    _delta(output, "streamed ")
    _delta(output, "answer once")
    output.event(
        _event(
            EventType.MODEL_ATTEMPT_COMPLETED,
            turn="t1",
            model_call_id="m1",
            content=reply,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        )
    )
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=reply))
    output.turn_finished()

    text = buffer.getvalue()
    assert text.count(reply) == 1
    assert text.index("本轮") < text.index("End Turn 1")


def test_local_compaction_does_not_add_turn_boundaries():
    output, buffer = _output()
    output.compact_started()
    output.compact_finished()
    output.turn_finished()  # no model turn exists yet
    text = buffer.getvalue()
    assert "PhyAgentOS · Turn" not in text
    assert "End Turn" not in text

    output.turn_started("s", request_text="第一次模型回合")
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply="ok"))
    output.turn_finished()
    assert "PhyAgentOS · Turn 1" in buffer.getvalue()
    assert "End Turn 1" in buffer.getvalue()


def test_turn_numbers_count_per_session():
    output, buffer = _output()
    output.turn_started("a", request_text="a1")
    output.turn_finished()
    output.turn_started("b", request_text="b1")
    output.turn_finished()
    text = buffer.getvalue()
    assert text.count("PhyAgentOS · Turn 1") == 2
    assert "Turn 2" not in text


def test_requests_and_replies_strip_controls_and_never_parse_markup():
    output, buffer = _output(markdown=False)
    request = "原始 [red] 请求\x1b[31m红色\x07"
    reply = "回答 [bold]不是标记[/bold]\x1b[0m"

    output.turn_started("s", request_text=request)
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=reply))
    output.turn_finished()

    text = buffer.getvalue()
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "原始 [red] 请求" in text and "红色" in text
    assert "回答 [bold]不是标记[/bold]" in text


def test_protocol_filter_keeps_text_that_only_mentions_dsml():
    protocol = _ProtocolTextFilter()
    plain = "DSML 只是一段文本，<|DSML| 不是完整协议标记"
    assert protocol.feed(plain) + protocol.flush() == plain
    assert protocol.blocked is False


def test_protocol_filter_handles_markers_split_across_chunks():
    protocol = _ProtocolTextFilter()
    visible = (
        protocol.feed("开头<|DSML| cal")
        + protocol.feed("ls>SECRET_CALL_BODY</|DSML| cal")
        + protocol.feed("ls>结尾")
    )
    assert visible == "开头结尾"
    assert protocol.blocked is True
    assert protocol.unterminated is False


def test_protocol_filter_discards_a_large_unterminated_body():
    protocol = _ProtocolTextFilter()
    assert protocol.feed("前<|DSML| calls>") == "前"
    for _ in range(200):
        protocol.feed("SECRET" * 500)
    assert protocol._buffer == "" or len(protocol._buffer) < len("</|DSML| calls>")
    assert "SECRET" not in protocol._buffer
    assert protocol.flush() == ""
    assert protocol.blocked is True
    assert protocol.unterminated is True


def test_unclosed_stream_block_is_dropped_with_one_warning():
    output, buffer = _output()
    leak = "可见内容<|DSML| calls>SECRET_UNCLOSED_BODY"

    output.turn_started("s", request_text="协议测试")
    _delta(output, leak)
    output.event(
        _event(
            EventType.MODEL_ATTEMPT_COMPLETED,
            turn="t1",
            model_call_id="m1",
            content=leak,
            usage={"prompt_tokens": 5, "completion_tokens": 1},
        )
    )
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=leak))
    output.turn_finished()

    text = buffer.getvalue()
    assert "可见内容" in text
    assert "SECRET_UNCLOSED_BODY" not in text
    assert "<|DSML|" not in text
    assert text.count(_NOTICE) == 1


def test_complete_protocol_block_streamed_across_chunks_is_filtered():
    output, buffer = _output()
    chunks = [
        "可见前",
        "<|DSML| cal",
        'ls>{"name":"SECRET_STREAM_TOOL"}</|DSML| cal',
        "ls>可见后",
    ]
    reply = "".join(chunks)

    output.turn_started("s", request_text="流式协议")
    for chunk in chunks:
        _delta(output, chunk)
    output.event(
        _event(
            EventType.MODEL_ATTEMPT_COMPLETED,
            turn="t1",
            model_call_id="m1",
            content=reply,
            usage={"prompt_tokens": 5, "completion_tokens": 1},
        )
    )
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=reply))
    output.turn_finished()

    text = buffer.getvalue()
    assert "可见前可见后" in text
    assert "SECRET_STREAM_TOOL" not in text
    assert "<|DSML|" not in text
    assert text.count(_NOTICE) == 1


def test_complete_protocol_block_in_final_reply_is_not_shown():
    output, buffer = _output()
    reply = "外部<|DSML| calls>{\"name\":\"SECRET_TOOL\"}</|DSML| calls>文本"

    output.turn_started("s", request_text="无流式")
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=reply))
    output.turn_finished()

    text = buffer.getvalue()
    assert "外部" in text and "文本" in text
    assert "SECRET_TOOL" not in text
    assert "<|DSML|" not in text
    assert text.count(_NOTICE) == 1


def test_several_protocol_blocks_in_one_turn_warn_once():
    output, buffer = _output()
    reply = (
        "一<|DSML| calls>BLOCK_ONE</|DSML| calls>"
        "二<|DSML| calls>BLOCK_TWO</|DSML| calls>三"
    )

    output.turn_started("s", request_text="多块")
    output.result(TurnResult("s", "t1", TurnStatus.COMPLETED, reply=reply))
    output.turn_finished()

    text = buffer.getvalue()
    assert "一" in text and "二" in text and "三" in text
    assert "BLOCK_ONE" not in text and "BLOCK_TWO" not in text
    assert text.count(_NOTICE) == 1


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _FakePrompt:
    def __init__(self) -> None:
        self.bottom_toolbar = None
        self.app = SimpleNamespace(invalidate=lambda: None)


def _tool(display, turn, call_id, name, status):
    display.note_event(
        _event(EventType.TOOL_INTENT, turn=turn, tool_call_id=call_id, tool_name=name)
    )
    display.note_event(
        _event(EventType.TOOL_DISPATCHED, turn=turn, tool_call_id=call_id, tool_name=name)
    )
    display.note_event(
        _event(
            EventType.TOOL_SETTLED,
            turn=turn,
            tool_call_id=call_id,
            tool_name=name,
            status=status,
        )
    )


def test_default_tool_rows_stay_limited_and_expanded_prints_full_record():
    buffer = StringIO()
    display = TerminalDisplay(
        Console(file=buffer, width=120, color_system=None), clock=_Clock()
    )
    display.attach_prompt(_FakePrompt())
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    for index in range(6):
        _tool(display, "t1", f"c{index}", f"tool{index}", "succeeded")

    lines = display.render_lines(width=80, rows=24)
    rows = [line for line in lines if line.startswith("| ")]
    assert len(rows) == 1 + 4  # header plus the four most recent rows
    assert "6 calls" in rows[0] and "2 hidden" in rows[0]
    assert "tool5" in rows[-1] and "tool0" not in "".join(rows)

    display.tool_view("on")
    text = buffer.getvalue()
    for index in range(6):
        assert f"{index + 1}. tool{index} · succeeded" in text

    _tool(display, "t1", "c6", "tool6", "failed")
    assert "7. tool6 · failed" in buffer.getvalue()
    assert display.expanded is True
