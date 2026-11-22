"""Run the hub's read-only MCP tools from a shell."""

import argparse
import json
from collections.abc import Callable
from inspect import Parameter, getdoc, signature
from typing import get_type_hints

from pydantic import BaseModel, ValidationError, create_model

from strap_hub.mcp.server import QUERY_TOOLS, HubToolError, InvalidToolArguments


def _argument_model(fn: Callable[..., str]) -> type[BaseModel]:
    hints = get_type_hints(fn, include_extras=True)
    fields = {
        name: (hints[name], ... if parameter.default is Parameter.empty else parameter.default)
        for name, parameter in signature(fn).parameters.items()
    }
    return create_model(f"{fn.__name__}_arguments", **fields)


def register(commands: argparse._SubParsersAction) -> None:
    query = commands.add_parser("query", help="run a read-only MCP tool")
    query.add_argument("--list", action="store_true", help="list tools and their descriptions")
    query.set_defaults(query_parser=query)
    tools = query.add_subparsers(dest="query_tool", metavar="tool")
    descriptions = {}
    for name, fn in QUERY_TOOLS.items():
        description = getdoc(fn) or ""
        descriptions[name] = description.partition("\n")[0]
        tool = tools.add_parser(name, help=descriptions[name], description=description)
        model = _argument_model(fn)
        tool.set_defaults(query_parser=tool, query_model=model)
        for field_name, field in model.model_fields.items():
            label = field.validation_alias or field_name
            argument = label if field.is_required() else f"--{label.replace('_', '-')}"
            tool.add_argument(
                argument,
                **({} if field.is_required() else {"dest": label}),
                type=int if field.annotation is int else str,
                default=argparse.SUPPRESS,
                help=field.description,
            )
    query.set_defaults(query_descriptions=descriptions)


def run(args: argparse.Namespace) -> None:
    parser = args.query_parser
    if args.list:
        if args.query_tool:
            parser.error("--list cannot be combined with a tool")
        for name, description in args.query_descriptions.items():
            print(f"{name}\t{description}")
        return
    if args.query_tool is None:
        parser.error("provide a tool name or --list")

    try:
        arguments = args.query_model.model_validate(vars(args)).model_dump()
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        parser.error(f"{error['loc'][0]}: {error['msg']}")

    fn = QUERY_TOOLS[args.query_tool]
    try:
        result = fn(**arguments)
    except InvalidToolArguments as exc:
        parser.error(str(exc))
    except HubToolError as exc:
        error = str(exc)
    except Exception:
        # Unexpected exceptions can include paths, credentials or stored data.
        error = "Could not query the hub store"
    else:
        print(result)
        return
    print(json.dumps({"error": error}, separators=(",", ":")))
    raise SystemExit(1)
