"""A restricted Python-subset program runner for composing tool calls.

This module is not a Python sandbox and does not claim to run arbitrary
Python. It parses source with :mod:`ast`, rejects every syntax node outside a
small documented subset, and evaluates only that subset against a private
value model. There is no import, attribute access (except the literal
``tools.call``), file, process, network, or reflection surface.

Supported statements::

    name = expression
    name[index] = expression
    name += expression          # + - * / // %
    if condition: ... [else: ...]
    for name in finite_iterable: ...   # break / continue allowed
    return expression
    <expression>
    pass

Supported expressions: constants, names, list/tuple/dict/set literals,
subscripts and slices, arithmetic/comparison/boolean operators, conditional
expressions, ``await tools.call(name, arguments)``,
``await parallel([await tools.call(...), ...])``, ``print``, and a small set
of pure builtins (``len``, ``range``, ``str``, ``int``, ``float``, ``bool``,
``abs``, ``min``, ``max``, ``sum``).

Every tool call is dispatched through the injected executor callback, so the
core still writes ``tool.intent`` / ``tool.dispatched`` / ``tool.settled``
with the generated subcall id. The runner only decides syntax and limits; it
never calls a tool handler directly.
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
import textwrap
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CodeExecutionLimits",
    "ProgramCancelledError",
    "ProgramError",
    "ProgramResult",
    "ProgramSubcall",
    "ProgramToolResult",
    "run_program",
]


@dataclass(frozen=True, slots=True)
class CodeExecutionLimits:
    """Hard ceilings for one program execution."""

    max_source_chars: int = 8_000
    max_steps: int = 4_000
    max_tool_calls: int = 8
    max_loop_iterations: int = 100
    max_parallel_calls: int = 4
    max_output_chars: int = 4_000
    max_result_chars: int = 4_000
    max_container_items: int = 100
    max_tool_result_chars: int = 20_000
    max_value_nodes: int = 2_000
    max_int_bits: int = 4_096
    max_variables: int = 200

    def __post_init__(self) -> None:
        for name in (
            "max_source_chars",
            "max_steps",
            "max_tool_calls",
            "max_loop_iterations",
            "max_parallel_calls",
            "max_output_chars",
            "max_result_chars",
            "max_container_items",
            "max_tool_result_chars",
            "max_value_nodes",
            "max_int_bits",
            "max_variables",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int")


class ProgramError(Exception):
    """A program was rejected or stopped by a documented limit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ProgramCancelledError(Exception):
    """The turn cancellation token fired while the program was running."""


@dataclass(frozen=True, slots=True)
class ProgramToolResult:
    """Lightweight result of one dispatched subcall.

    The agent capability adapts its ``ToolOutcome`` to this local shape so the
    shared tooling layer never imports the agent runtime.
    """

    status: str
    text: str = ""
    content: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramSubcall:
    """Trace entry for one tool call made by a program."""

    tool_call_id: str
    tool_name: str
    status: str
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramResult:
    """Bounded output plus the complete subcall trace."""

    printed: str
    returned: Any
    has_return: bool
    subcalls: tuple[ProgramSubcall, ...]
    steps: int
    had_unknown: bool


ToolDispatch = Callable[[str, Mapping[str, Any], str], Awaitable[ProgramToolResult]]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


class _ValueBudget:
    """Incremental ceiling for one value conversion.

    The budget is spent while walking and copying, not after a full JSON dump,
    so a nested or shared structure cannot allocate far beyond the limit before
    being rejected.
    """

    __slots__ = ("chars_left", "nodes_left")

    def __init__(self, *, max_chars: int, max_nodes: int) -> None:
        self.chars_left = max_chars
        self.nodes_left = max_nodes

    def visit(self) -> None:
        self.nodes_left -= 1
        if self.nodes_left < 0:
            raise ProgramError(
                "program_result_limit", "program value has too many nodes"
            )

    def spend(self, chars: int) -> None:
        self.chars_left -= chars
        if self.chars_left < 0:
            raise ProgramError(
                "program_result_limit", "program value exceeds the character limit"
            )


def _plain_value(
    value: Any,
    *,
    limits: CodeExecutionLimits,
    depth: int = 0,
    budget: _ValueBudget | None = None,
) -> Any:
    """Convert a runtime value to a finite JSON-compatible value."""
    if depth > 8:
        raise ProgramError("program_value_too_deep", "program value nesting is too deep")
    if budget is None:
        budget = _ValueBudget(
            max_chars=limits.max_result_chars, max_nodes=limits.max_value_nodes
        )
    budget.visit()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > limits.max_int_bits:
            raise ProgramError(
                "program_integer_limit", "integer exceeds the bit limit"
            )
        return value
    if isinstance(value, str):
        budget.spend(len(value))
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ProgramError(
                "program_invalid_value", "non-finite float values are not allowed"
            )
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > limits.max_container_items:
            raise ProgramError(
                "program_container_limit", "program container exceeds the item limit"
            )
        budget.spend(len(value) * 2)
        return [
            _plain_value(item, limits=limits, depth=depth + 1, budget=budget)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        if len(value) > limits.max_container_items:
            raise ProgramError(
                "program_container_limit", "program container exceeds the item limit"
            )
        plain = [
            _plain_value(item, limits=limits, depth=depth + 1, budget=budget)
            for item in value
        ]
        try:
            return sorted(plain, key=repr)
        except TypeError:
            raise ProgramError(
                "program_invalid_value", "set values are not comparable"
            ) from None
    if isinstance(value, Mapping):
        if len(value) > limits.max_container_items:
            raise ProgramError(
                "program_container_limit", "program mapping exceeds the item limit"
            )
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, (str, int, bool)):
                raise ProgramError(
                    "program_invalid_value", "mapping keys must be strings or numbers"
                )
            text_key = str(key)
            budget.spend(len(text_key) + 4)
            result[text_key] = _plain_value(
                item, limits=limits, depth=depth + 1, budget=budget
            )
        return result
    raise ProgramError(
        "program_invalid_value",
        f"value of type {type(value).__name__} cannot cross the program boundary",
    )


def _render(value: Any, *, limits: CodeExecutionLimits) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    plain = _plain_value(value, limits=limits)
    rendered = json.dumps(plain, ensure_ascii=False, sort_keys=True)
    if len(rendered) > limits.max_result_chars:
        raise ProgramError(
            "program_result_limit", "program value exceeds the result character limit"
        )
    return rendered


def _bounded_json(value: Any, *, limits: CodeExecutionLimits) -> str:
    """Render one bounded JSON value, rejecting oversized output."""
    plain = _plain_value(value, limits=limits)
    rendered = json.dumps(plain, ensure_ascii=False, sort_keys=True)
    if len(rendered) > limits.max_result_chars:
        raise ProgramError(
            "program_result_limit", "program result exceeds the character limit"
        )
    return rendered


_SOURCE_WRAPPER = "async def __program__():\n"
_BUILTIN_CALLS = frozenset(
    {"print", "len", "range", "str", "int", "float", "bool", "abs", "min", "max", "sum"}
)
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
_ALLOWED_UNARY = (ast.UAdd, ast.USub, ast.Not)
_ALLOWED_COMPARE = (
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)


class _Return(Exception):  # noqa: N818
    def __init__(self, value: Any) -> None:
        self.value = value


class _Break(Exception):  # noqa: N818
    pass


class _Continue(Exception):  # noqa: N818
    pass


def _forbidden(node: ast.AST) -> ProgramError:
    return ProgramError(
        "program_forbidden_syntax",
        f"{type(node).__name__} is not available in the restricted program",
    )


def _validate_statements(statements: list[ast.stmt]) -> None:
    for statement in statements:
        if isinstance(statement, ast.Expr):
            _validate_expr(statement.value)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                _validate_target(target)
            _validate_expr(statement.value)
        elif isinstance(statement, ast.AugAssign):
            if not isinstance(statement.target, ast.Name):
                raise _forbidden(statement.target)
            if statement.target.id.startswith("_"):
                raise ProgramError(
                    "program_forbidden_name", "names starting with '_' are reserved"
                )
            if not isinstance(statement.op, _ALLOWED_BINOPS):
                raise _forbidden(statement.op)
            _validate_expr(statement.value)
        elif isinstance(statement, ast.If):
            _validate_expr(statement.test)
            _validate_statements(statement.body)
            _validate_statements(statement.orelse)
        elif isinstance(statement, ast.For):
            _validate_target(statement.target)
            _validate_expr(statement.iter)
            if statement.orelse:
                raise _forbidden(statement)
            _validate_statements(statement.body)
        elif isinstance(statement, (ast.Break, ast.Continue, ast.Pass)):
            continue
        elif isinstance(statement, ast.Return):
            if statement.value is not None:
                _validate_expr(statement.value)
        else:
            raise _forbidden(statement)


def _validate_target(target: ast.AST) -> None:
    if isinstance(target, ast.Name):
        if target.id.startswith("_"):
            raise ProgramError(
                "program_forbidden_name", "names starting with '_' are reserved"
            )
        return
    if isinstance(target, ast.Subscript):
        _validate_expr(target.value)
        _validate_expr(target.slice)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _validate_target(item)
        return
    raise _forbidden(target)


def _validate_expr(node: ast.AST) -> None:
    if isinstance(node, ast.Constant):
        if not (
            node.value is None
            or isinstance(node.value, (bool, int, float, str))
        ):
            raise ProgramError(
                "program_forbidden_value",
                f"constant of type {type(node.value).__name__} is not allowed",
            )
        return
    if isinstance(node, ast.Name):
        if node.id.startswith("_"):
            raise ProgramError(
                "program_forbidden_name", "names starting with '_' are reserved"
            )
        return
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for item in node.elts:
            _validate_expr(item)
        return
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if key is not None:
                _validate_expr(key)
        for value in node.values:
            _validate_expr(value)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise _forbidden(node.op)
        _validate_expr(node.left)
        _validate_expr(node.right)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            raise _forbidden(node.op)
        _validate_expr(node.operand)
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise _forbidden(node.op)
        for value in node.values:
            _validate_expr(value)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARE):
                raise _forbidden(op)
        _validate_expr(node.left)
        for comparator in node.comparators:
            _validate_expr(comparator)
        return
    if isinstance(node, ast.Subscript):
        _validate_expr(node.value)
        _validate_expr(node.slice)
        return
    if isinstance(node, ast.Slice):
        for part in (node.lower, node.upper, node.step):
            if part is not None:
                _validate_expr(part)
        return
    if isinstance(node, ast.IfExp):
        _validate_expr(node.test)
        _validate_expr(node.body)
        _validate_expr(node.orelse)
        return
    if isinstance(node, ast.Await):
        _validate_expr(node.value)
        return
    if isinstance(node, ast.Call):
        if node.keywords:
            raise ProgramError(
                "program_forbidden_syntax", "keyword arguments are not available"
            )
        func = node.func
        if isinstance(func, ast.Attribute):
            if not (
                isinstance(func.value, ast.Name)
                and func.value.id == "tools"
                and func.attr == "call"
            ):
                raise _forbidden(func)
        elif isinstance(func, ast.Name):
            if func.id not in _BUILTIN_CALLS and func.id != "parallel":
                raise ProgramError(
                    "program_forbidden_call",
                    f"function {func.id!r} is not available",
                )
        else:
            raise _forbidden(func)
        for argument in node.args:
            _validate_expr(argument)
        return
    raise _forbidden(node)


def _parse(source: str, *, limits: CodeExecutionLimits) -> list[ast.stmt]:
    if not isinstance(source, str) or not source.strip():
        raise ProgramError("program_empty", "program source must be non-empty")
    if len(source) > limits.max_source_chars:
        raise ProgramError(
            "program_source_limit", "program source exceeds the character limit"
        )
    try:
        wrapped = _SOURCE_WRAPPER + textwrap.indent(source, "    ") + "\n"
        module = ast.parse(wrapped)
    except SyntaxError as exc:
        raise ProgramError(
            "program_syntax_error", f"program is not valid Python syntax: {exc.msg}"
        ) from None
    except (RecursionError, MemoryError):
        raise ProgramError(
            "program_too_complex", "program syntax is nested too deeply"
        ) from None
    if len(module.body) != 1 or not isinstance(module.body[0], ast.AsyncFunctionDef):
        raise ProgramError("program_internal_error", "program wrapper was not built")
    body = module.body[0].body
    try:
        _validate_statements(body)
    except RecursionError:
        raise ProgramError(
            "program_too_complex", "program syntax is nested too deeply"
        ) from None
    return body


class _Interpreter:
    def __init__(
        self,
        *,
        body: list[ast.stmt],
        is_cancelled: Callable[[], bool],
        limits: CodeExecutionLimits,
        available_tools: frozenset[str],
        dispatch: ToolDispatch,
        outer_call_id: str,
    ) -> None:
        self._body = body
        self._is_cancelled = is_cancelled
        self._limits = limits
        self._available = available_tools
        self._dispatch = dispatch
        self._outer_call_id = outer_call_id
        self._env: dict[str, Any] = {}
        self._output: list[str] = []
        self._out_chars = 0
        self._subcalls: list[ProgramSubcall] = []
        self._tool_attempts = 0
        self._steps = 0
        self._had_unknown = False
        self._terminal_code: str | None = None
        self._returned: Any = None
        self._has_return = False

    def _tick(self) -> None:
        self._steps += 1
        if self._steps > self._limits.max_steps:
            raise ProgramError(
                "program_step_limit", "program exceeded the step limit"
            )
        if self._is_cancelled():
            raise ProgramCancelledError()

    async def run(self) -> None:
        try:
            await self._exec_block(self._body)
        except _Return as stop:
            self._returned = _plain_value(stop.value, limits=self._limits)
            self._has_return = True
        except (_Break, _Continue):
            raise ProgramError(
                "program_loop_control", "break/continue is only valid inside a for loop"
            ) from None

    async def _exec_block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self._tick()
            await self._exec_stmt(statement)

    async def _exec_stmt(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.Expr):
            await self._eval(statement.value)
            return
        if isinstance(statement, ast.Assign):
            value = await self._eval(statement.value)
            for target in statement.targets:
                await self._assign(target, value)
            return
        if isinstance(statement, ast.AugAssign):
            current = await self._eval(statement.target)
            value = await self._eval(statement.value)
            result = self._binop(statement.op, current, value)
            await self._assign(statement.target, result)
            return
        if isinstance(statement, ast.If):
            if self._truth(await self._eval(statement.test)):
                await self._exec_block(statement.body)
            else:
                await self._exec_block(statement.orelse)
            return
        if isinstance(statement, ast.For):
            iterable = await self._eval(statement.iter)
            await self._exec_for(statement, iterable)
            return
        if isinstance(statement, ast.Break):
            raise _Break()
        if isinstance(statement, ast.Continue):
            raise _Continue()
        if isinstance(statement, ast.Pass):
            return
        if isinstance(statement, ast.Return):
            value = None if statement.value is None else await self._eval(statement.value)
            raise _Return(value)
        raise _forbidden(statement)

    async def _exec_for(self, statement: ast.For, iterable: Any) -> None:
        if isinstance(iterable, (list, tuple, str)):
            items = list(iterable)
        else:
            raise ProgramError(
                "program_forbidden_iterable",
                "for loops may only iterate a list, tuple, or string",
            )
        if len(items) > self._limits.max_loop_iterations:
            raise ProgramError(
                "program_loop_limit", "for loop exceeds the iteration limit"
            )
        for item in items:
            self._tick()
            await self._assign(statement.target, item)
            try:
                await self._exec_block(statement.body)
            except _Continue:
                continue
            except _Break:
                break

    async def _assign(self, target: ast.AST, value: Any) -> None:
        if isinstance(target, ast.Name):
            if target.id.startswith("_"):
                raise ProgramError(
                    "program_forbidden_name", "names starting with '_' are reserved"
                )
            if (
                target.id not in self._env
                and len(self._env) >= self._limits.max_variables
            ):
                raise ProgramError(
                    "program_variable_limit", "program declared too many variables"
                )
            self._env[target.id] = value
            return
        if isinstance(target, ast.Subscript):
            container = await self._eval(target.value)
            index = await self._eval(target.slice)
            self._store_index(container, index, value)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(value, (list, tuple)) or len(value) != len(target.elts):
                raise ProgramError(
                    "program_unpack_error", "unpacking needs a matching sequence"
                )
            for item, item_value in zip(target.elts, value, strict=True):
                await self._assign(item, item_value)
            return
        raise _forbidden(target)

    async def _eval(self, node: ast.AST) -> Any:
        self._tick()
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, int) and not isinstance(value, bool):
                self._check_int(value)
            if isinstance(value, float) and (
                value != value or value in (float("inf"), float("-inf"))
            ):
                raise ProgramError(
                    "program_invalid_value", "non-finite float literals are not allowed"
                )
            return value
        if isinstance(node, ast.Name):
            if node.id in self._env:
                return self._env[node.id]
            raise ProgramError(
                "program_name_error", f"name {node.id!r} is not defined"
            )
        if isinstance(node, ast.List):
            self._check_literal_size(len(node.elts))
            return [await self._eval(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            self._check_literal_size(len(node.elts))
            return tuple([await self._eval(item) for item in node.elts])
        if isinstance(node, ast.Set):
            self._check_literal_size(len(node.elts))
            values = [await self._eval(item) for item in node.elts]
            if len(values) > self._limits.max_container_items:
                raise ProgramError(
                    "program_container_limit", "set literal exceeds the item limit"
                )
            return set(values)
        if isinstance(node, ast.Dict):
            self._check_literal_size(len(node.values))
            result: dict[Any, Any] = {}
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                key = None if key_node is None else await self._eval(key_node)
                result[key] = await self._eval(value_node)
            if len(result) > self._limits.max_container_items:
                raise ProgramError(
                    "program_container_limit", "dict literal exceeds the item limit"
                )
            return result
        if isinstance(node, ast.BinOp):
            left = await self._eval(node.left)
            right = await self._eval(node.right)
            return self._binop(node.op, left, right)
        if isinstance(node, ast.UnaryOp):
            value = await self._eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.Not):
                return not self._truth(value)
            raise _forbidden(node.op)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value_node in node.values:
                    result = await self._eval(value_node)
                    if not self._truth(result):
                        return result
                return result
            result = False
            for value_node in node.values:
                result = await self._eval(value_node)
                if self._truth(result):
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = await self._eval(node.left)
            for op, comparator_node in zip(node.ops, node.comparators, strict=True):
                right = await self._eval(comparator_node)
                if not self._compare(op, left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Subscript):
            container = await self._eval(node.value)
            index = await self._eval(node.slice)
            return self._load_index(container, index)
        if isinstance(node, ast.Slice):
            return slice(
                None if node.lower is None else await self._eval(node.lower),
                None if node.upper is None else await self._eval(node.upper),
                None if node.step is None else await self._eval(node.step),
            )
        if isinstance(node, ast.IfExp):
            branch = node.body if self._truth(await self._eval(node.test)) else node.orelse
            return await self._eval(branch)
        if isinstance(node, ast.Await):
            value = await self._eval(node.value)
            if asyncio.iscoroutine(value):
                return await value
            return value
        if isinstance(node, ast.Call):
            return await self._eval_call(node)
        raise _forbidden(node)

    async def _eval_call(self, node: ast.Call) -> Any:
        func = node.func
        if isinstance(func, ast.Attribute):
            if not (
                isinstance(func.value, ast.Name)
                and func.value.id == "tools"
                and func.attr == "call"
            ):
                raise _forbidden(func)
            if len(node.args) != 2:
                raise ProgramError(
                    "program_call_shape",
                    "tools.call expects (name, arguments)",
                )
            name_value = await self._eval(node.args[0])
            arguments = await self._eval(node.args[1])
            if not isinstance(name_value, str) or not name_value:
                raise ProgramError(
                    "program_call_shape", "tool name must be a non-empty string"
                )
            if not isinstance(arguments, Mapping):
                raise ProgramError(
                    "program_call_shape", "tool arguments must be a dict"
                )
            return await self._call_tool(name_value, arguments)
        if not isinstance(func, ast.Name):
            raise _forbidden(func)
        if func.id == "parallel":
            return await self._eval_parallel(node)
        if func.id not in _BUILTIN_CALLS:
            raise ProgramError(
                "program_forbidden_call", f"function {func.id!r} is not available"
            )
        values = [await self._eval(argument) for argument in node.args]
        return self._builtin(func.id, values)

    async def _eval_parallel(self, node: ast.Call) -> Any:
        if len(node.args) != 1:
            raise ProgramError(
                "program_parallel_shape", "parallel expects one list of await calls"
            )
        sequence = node.args[0]
        if not isinstance(sequence, (ast.List, ast.Tuple)):
            raise ProgramError(
                "program_parallel_shape", "parallel expects a list or tuple literal"
            )
        if len(sequence.elts) > self._limits.max_parallel_calls:
            raise ProgramError(
                "program_parallel_limit", "parallel list exceeds the call limit"
            )
        coroutines: list[Any] = []
        for element in sequence.elts:
            if not isinstance(element, ast.Await):
                raise ProgramError(
                    "program_parallel_shape",
                    "each parallel element must be 'await tools.call(...)'",
                )
            coroutines.append(self._eval(element.value))
        tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
        # Do not cancel already-started subcalls: each one must reach its own
        # settlement. Tasks that have not reached dispatch yet observe the
        # terminal flag in _call_tool and do not dispatch at all.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if (
                isinstance(result, ProgramError)
                and self._terminal_code is not None
                and result.code == self._terminal_code
            ):
                raise result
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def _call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self._tool_attempts += 1
        if self._tool_attempts > self._limits.max_tool_calls:
            raise ProgramError(
                "program_tool_call_limit", "program exceeded the tool call limit"
            )
        if self._terminal_code is not None:
            raise ProgramError(
                self._terminal_code,
                "the program is stopping after a durable tool-record failure",
            )
        plain_arguments = _plain_value(arguments, limits=self._limits)
        if not isinstance(plain_arguments, dict):
            raise ProgramError(
                "program_call_shape", "tool arguments must become a JSON object"
            )
        subcall_id = f"{self._outer_call_id}:sub:{self._tool_attempts:02d}"
        if name not in self._available:
            self._record_subcall(
                subcall_id,
                name,
                "rejected",
                "code_tool_not_exposed",
                "this tool is not exposed to the restricted program",
            )
            return self._subcall_value(
                subcall_id,
                name,
                "rejected",
                "this tool is not exposed to the restricted program",
                None,
            )
        try:
            outcome = await self._dispatch(name, plain_arguments, subcall_id)
        except ProgramError as exc:
            # The capability raises ProgramError only for durable-record
            # failures. Latch it so sequential calls and parallel tasks that
            # have not dispatched yet stop immediately.
            self._terminal_code = exc.code
            raise
        if not isinstance(outcome, ProgramToolResult):
            raise ProgramError(
                "program_dispatch_error",
                "tool dispatch returned an unsupported result type",
            )
        if self._is_cancelled():
            raise ProgramCancelledError()
        self._record_subcall(
            subcall_id,
            name,
            outcome.status,
            outcome.error_code,
            outcome.error_message,
        )
        if outcome.status == "unknown":
            self._had_unknown = True
        return self._subcall_value(
            subcall_id, name, outcome.status, outcome.text, outcome.content
        )

    def _subcall_value(
        self,
        subcall_id: str,
        name: str,
        status: str,
        text: str,
        content: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "tool_call_id": subcall_id,
            "tool": name,
            "status": status,
            "text": _truncate(text, self._limits.max_tool_result_chars),
            "content": (
                _plain_value(content, limits=self._limits) if content is not None else None
            ),
        }
        value["ok"] = status == "succeeded"
        return value

    def _record_subcall(
        self,
        subcall_id: str,
        name: str,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        self._subcalls.append(
            ProgramSubcall(
                tool_call_id=subcall_id,
                tool_name=name,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        )

    def _store_index(self, container: Any, index: Any, value: Any) -> None:
        try:
            container[index] = value
        except (IndexError, KeyError, TypeError) as exc:
            raise ProgramError(
                "program_index_error", f"index assignment failed: {exc}"
            ) from None

    def _load_index(self, container: Any, index: Any) -> Any:
        if isinstance(container, (list, tuple, str, dict)):
            try:
                return container[index]
            except (IndexError, KeyError, TypeError) as exc:
                raise ProgramError(
                    "program_index_error", f"index lookup failed: {exc}"
                ) from None
        raise ProgramError(
            "program_forbidden_value", "only built-in indexable values are supported"
        )

    def _builtin(self, name: str, values: list[Any]) -> Any:
        if name == "print":
            parts: list[str] = []
            total = 1
            for value in values:
                rendered = _render(value, limits=self._limits)
                total += len(rendered) + (1 if parts else 0)
                if self._out_chars + total > self._limits.max_output_chars:
                    raise ProgramError(
                        "program_output_limit",
                        "program output exceeds the character limit",
                    )
                parts.append(rendered)
            text = " ".join(parts)
            self._out_chars += len(text) + 1
            self._output.append(text)
            return None
        if name == "len":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "len expects one argument")
            try:
                return len(values[0])
            except TypeError as exc:
                raise ProgramError("program_call_shape", str(exc)) from None
        if name == "range":
            return self._safe_range(values)
        if name == "str":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "str expects one argument")
            return _render(values[0], limits=self._limits)
        if name == "int":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "int expects one argument")
            try:
                return self._check_int(int(values[0]))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProgramError(
                    "program_value_error", f"int conversion failed: {exc}"
                ) from None
        if name == "float":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "float expects one argument")
            try:
                result = float(values[0])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProgramError(
                    "program_value_error", f"float conversion failed: {exc}"
                ) from None
            return self._check_number(result)
        if name == "bool":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "bool expects one argument")
            return self._truth(values[0])
        if name == "abs":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "abs expects one argument")
            return self._check_number(abs(values[0]))
        if name in ("min", "max"):
            if len(values) == 1:
                values = list(values[0])
            if not values:
                raise ProgramError("program_call_shape", f"{name} needs values")
            return (min if name == "min" else max)(values)
        if name == "sum":
            if len(values) != 1:
                raise ProgramError("program_call_shape", "sum expects one argument")
            return self._check_number(sum(values[0]))
        raise ProgramError(
            "program_forbidden_call", f"function {name!r} is not available"
        )

    def _safe_range(self, values: list[Any]) -> list[int]:
        if not 1 <= len(values) <= 3:
            raise ProgramError(
                "program_call_shape", "range expects one to three arguments"
            )
        for value in values:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ProgramError("program_call_shape", "range arguments must be ints")
        try:
            span = range(*values)
            length = len(span)
        except (ValueError, OverflowError):
            raise ProgramError(
                "program_call_shape", "range arguments are invalid"
            ) from None
        if length > self._limits.max_loop_iterations:
            raise ProgramError(
                "program_loop_limit", "range exceeds the iteration limit"
            )
        return list(span)

    def _truth(self, value: Any) -> bool:
        return bool(value)

    def _check_literal_size(self, count: int) -> None:
        if count > self._limits.max_container_items:
            raise ProgramError(
                "program_container_limit", "literal exceeds the item limit"
            )

    def _check_int(self, value: int) -> int:
        if value.bit_length() > self._limits.max_int_bits:
            raise ProgramError(
                "program_integer_limit", "integer exceeds the bit limit"
            )
        return value

    def _check_number(self, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            return self._check_int(value)
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            raise ProgramError(
                "program_invalid_value", "non-finite float values are not allowed"
            )
        return value

    def _repeat_length(self, length: int, count: int, limit: int) -> None:
        if count > 0 and length and count > limit // length:
            raise ProgramError(
                "program_result_limit", "repeated value exceeds the size limit"
            )

    def _binop(self, op: ast.operator, left: Any, right: Any) -> Any:
        if isinstance(op, ast.Add):
            if isinstance(left, str) and isinstance(right, str):
                if len(left) + len(right) > self._limits.max_result_chars:
                    raise ProgramError(
                        "program_result_limit", "concatenated string exceeds the limit"
                    )
            elif isinstance(left, (list, tuple)) and isinstance(right, type(left)):
                if len(left) + len(right) > self._limits.max_container_items:
                    raise ProgramError(
                        "program_container_limit",
                        "concatenated container exceeds the item limit",
                    )
            return self._guard_sequence(self._check_number(left + right))
        if isinstance(op, ast.Sub):
            return self._check_number(left - right)
        if isinstance(op, ast.Mult):
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and isinstance(right, int)
                and not isinstance(right, bool)
            ):
                if left and right and left.bit_length() + right.bit_length() > self._limits.max_int_bits:
                    raise ProgramError(
                        "program_integer_limit",
                        "integer multiplication would exceed the bit limit",
                    )
                return self._check_int(left * right)
            if isinstance(left, str) and isinstance(right, int):
                self._repeat_length(len(left), right, self._limits.max_result_chars)
                return self._guard_sequence(left * right)
            if isinstance(right, str) and isinstance(left, int):
                self._repeat_length(len(right), left, self._limits.max_result_chars)
                return self._guard_sequence(right * left)
            if isinstance(left, (list, tuple)) and isinstance(right, int):
                self._repeat_length(
                    len(left), right, self._limits.max_container_items
                )
                return self._guard_sequence(left * right)
            if isinstance(right, (list, tuple)) and isinstance(left, int):
                self._repeat_length(
                    len(right), left, self._limits.max_container_items
                )
                return self._guard_sequence(right * left)
            return self._guard_sequence(self._check_number(left * right))
        if isinstance(op, ast.Div):
            try:
                return self._check_number(operator.truediv(left, right))
            except ZeroDivisionError:
                raise ProgramError(
                    "program_zero_division", "division by zero"
                ) from None
        if isinstance(op, ast.FloorDiv):
            try:
                return self._check_number(operator.floordiv(left, right))
            except ZeroDivisionError:
                raise ProgramError(
                    "program_zero_division", "division by zero"
                ) from None
        if isinstance(op, ast.Mod):
            if isinstance(left, str):
                raise ProgramError(
                    "program_forbidden_operation",
                    "string % formatting is not available",
                )
            try:
                return self._check_number(operator.mod(left, right))
            except ZeroDivisionError:
                raise ProgramError(
                    "program_zero_division", "division by zero"
                ) from None
        raise _forbidden(op)

    def _guard_sequence(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self._limits.max_result_chars:
            raise ProgramError(
                "program_result_limit", "value exceeds the size limit"
            )
        if (
            isinstance(value, (list, tuple))
            and len(value) > self._limits.max_container_items
        ):
            raise ProgramError(
                "program_container_limit", "container exceeds the item limit"
            )
        return value

    def _compare(self, op: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
        if isinstance(op, ast.Is):
            return left is right
        if isinstance(op, ast.IsNot):
            return left is not right
        raise _forbidden(op)

    def result(self) -> ProgramResult:
        return ProgramResult(
            printed="\n".join(self._output),
            returned=self._returned,
            has_return=self._has_return,
            subcalls=tuple(self._subcalls),
            steps=self._steps,
            had_unknown=self._had_unknown,
        )


async def run_program(
    source: str,
    *,
    is_cancelled: Callable[[], bool],
    limits: CodeExecutionLimits,
    available_tools: frozenset[str],
    dispatch: ToolDispatch,
    outer_call_id: str,
) -> ProgramResult:
    """Run one restricted program and return its bounded output and trace."""
    body = _parse(source, limits=limits)
    interpreter = _Interpreter(
        body=body,
        is_cancelled=is_cancelled,
        limits=limits,
        available_tools=available_tools,
        dispatch=dispatch,
        outer_call_id=outer_call_id,
    )
    try:
        await interpreter.run()
    except ProgramError:
        raise
    except ProgramCancelledError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - translate to a bounded program error
        raise ProgramError(
            "program_runtime_error", f"{type(exc).__name__}: {exc}"
        ) from None
    return interpreter.result()
