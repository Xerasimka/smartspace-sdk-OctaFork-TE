import abc
import asyncio
import asyncio.queues
import contextvars
import copy
import enum
import inspect
import json
import types
import typing
from typing import (
    Annotated,
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Concatenate,
    Generic,
    NamedTuple,
    ParamSpec,
    TypeVar,
    cast,
)

import semantic_version
from more_itertools import first
from pydantic import BaseModel, ConfigDict, TypeAdapter
from typing_extensions import get_origin

from smartspace.models import (
    BlockContext,
    BlockInterface,
    BlockMessage,
    BlockPinRef,
    InputPinInterface,
    InputValue,
    OutputPinInterface,
    OutputValue,
    PinRedirect,
    PinType,
    PortInterface,
    PortType,
    SmartSpaceWorkspace,
    StateInterface,
    StateValue,
    ThreadMessage,
)
from smartspace.utils import _get_type_adapter, _issubclass

B = TypeVar("B", bound="Block")
S = TypeVar("S")
T = TypeVar("T")
R = TypeVar("R")
P = ParamSpec("P")


def _get_pin_type_from_parameter_kind(kind: inspect._ParameterKind) -> PinType:
    if (
        kind == inspect._ParameterKind.KEYWORD_ONLY
        or kind == inspect._ParameterKind.POSITIONAL_OR_KEYWORD
    ):
        return PinType.SINGLE
    elif kind == inspect._ParameterKind.VAR_POSITIONAL:
        return PinType.LIST
    elif kind == inspect._ParameterKind.VAR_KEYWORD:
        return PinType.DICTIONARY
    else:
        raise Exception(f"Invalid parameter kind {kind}")


class FunctionPins(NamedTuple):
    inputs: dict[str, InputPinInterface]
    input_adapters: dict[str, TypeAdapter]
    output: tuple[OutputPinInterface | None, TypeAdapter | None]
    generics: dict[str, dict[str, Any]]


def _get_function_pins(fn: Callable, port_name: str | None = None) -> FunctionPins:
    signature = inspect.signature(fn)
    inputs: dict[str, InputPinInterface] = {}
    input_adapters: dict[str, TypeAdapter] = {}
    generics: dict[str, dict[str, Any]] = {}

    for name, param in signature.parameters.items():
        if name == "self":
            continue

        annotations = getattr(param.annotation, "__metadata__", [])
        metadata = first(
            (metadata.data for metadata in annotations if type(metadata) is Metadata),
            {},
        )

        if param.default == inspect._empty:
            default = None
            required = True
        else:
            default = param.default
            required = False

        type_adapter, schema, _generics = _get_json_schema_with_generics(
            param.annotation
        )
        generics.update(_generics)

        inputs[name] = InputPinInterface(
            metadata=metadata,
            json_schema=schema,
            sticky=any(
                [metadata.sticky for metadata in annotations if type(metadata) is Input]
            ),
            type=_get_pin_type_from_parameter_kind(param.kind),
            default=default,
            required=required,
            generics={
                name: BlockPinRef(
                    port=port_name if port_name else name,
                    pin=name if port_name else "",
                )
                for name in _generics.keys()
            },
        )
        input_adapters[name] = type_adapter

    if signature.return_annotation != signature.empty:
        annotations = getattr(signature.return_annotation, "__metadata__", [])
        metadata = first(
            (metadata.data for metadata in annotations if type(metadata) is Metadata),
            {},
        )

        output_type_adapter, schema, _generics = _get_json_schema_with_generics(
            signature.return_annotation
        )
        generics.update(_generics)

        output = (
            OutputPinInterface(
                metadata=metadata,
                json_schema=schema,
                type=PinType.SINGLE,
                generics={
                    name: BlockPinRef(
                        port=port_name if port_name else name,
                        pin=name if port_name else "",
                    )
                    for name in _generics.keys()
                },
            ),
            output_type_adapter,
        )
    else:
        output = (None, None)

    return FunctionPins(
        output=output,
        inputs=inputs,
        input_adapters=input_adapters,
        generics=generics,
    )


class ToolPins(NamedTuple):
    input: tuple[InputPinInterface | None, TypeAdapter | None]
    outputs: dict[str, OutputPinInterface]
    output_adapters: dict[str, TypeAdapter]
    generics: dict[str, dict[str, Any]]


def _get_tool_pins(fn: Callable, port_name: str | None = None) -> ToolPins:
    signature = inspect.signature(fn)
    outputs: dict[str, OutputPinInterface] = {}
    output_adapters: dict[str, TypeAdapter] = {}
    generics: dict[str, dict[str, Any]] = {}

    for name, param in signature.parameters.items():
        if name == "self":
            continue

        annotations = getattr(param.annotation, "__metadata__", [])
        metadata = first(
            (metadata.data for metadata in annotations if type(metadata) is Metadata),
            {},
        )

        type_adapter, schema, _generics = _get_json_schema_with_generics(
            param.annotation
        )
        generics.update(_generics)

        outputs[name] = OutputPinInterface(
            metadata=metadata,
            json_schema=schema,
            type=_get_pin_type_from_parameter_kind(param.kind),
            generics={
                name: BlockPinRef(
                    port=port_name if port_name else name,
                    pin=name if port_name else "",
                )
                for name in _generics.keys()
            },
        )

        output_adapters[name] = type_adapter

    if signature.return_annotation != signature.empty:
        annotations = getattr(signature.return_annotation, "__metadata__", [])
        metadata = first(
            (metadata.data for metadata in annotations if type(metadata) is Metadata),
            {},
        )

        input_type_adapter, schema, _generics = _get_json_schema_with_generics(
            signature.return_annotation
        )
        generics.update(_generics)

        _input = (
            InputPinInterface(
                metadata=metadata,
                json_schema=schema,
                type=PinType.SINGLE,
                sticky=False,
                required=False,
                default=None,
                generics={
                    name: BlockPinRef(
                        port=port_name if port_name else name,
                        pin=name if port_name else "",
                    )
                    for name in _generics.keys()
                },
            ),
            input_type_adapter,
        )
    else:
        _input = (None, None)

    return ToolPins(
        outputs=outputs,
        output_adapters=output_adapters,
        input=_input,
        generics=generics,
    )


class Metadata:
    def __init__(self, **kwargs):
        self.data = kwargs


GenericSetterT = TypeVar("GenericSetterT")


class GenericSetter(Generic[GenericSetterT]):
    schema: dict[str, Any]


class Config: ...


def _get_all_bases(cls: type):
    bases: list[type] = []

    cls_bases = set(
        getattr(cls, "__orig_bases__", tuple())
        + getattr(cls, "__bases__", tuple())
        + getattr(get_origin(cls), "__bases__", tuple())
    )

    for base in cls_bases:
        if base in bases:
            continue

        bases.append(base)
        bases.extend(_get_all_bases(base))

    return bases


def _get_input_pin_from_metadata(
    field_type: type,
    pin_type: PinType,
    port_name: str,
    field_name: str,
    parent: type | None = None,
) -> tuple[
    tuple[InputPinInterface | None, TypeAdapter | None], dict[str, dict[str, Any]]
]:
    config: Config | None = None
    _input: Input | None = None
    state: State | None = None
    metadata: dict[str, Any] = {}

    for m in getattr(field_type, "__metadata__", []):
        if isinstance(m, Config):
            config = m

        if isinstance(m, Input):
            _input = m

        if isinstance(m, State):
            state = m

        if isinstance(m, Metadata):
            metadata = m.data

    matches = len([True for i in [config, _input, state] if i is not None])

    if matches > 1:
        raise ValueError(
            "Fields can only be annotated with one of Config(), Input(), and State()"
        )

    if matches == 0:
        return (None, None), {}

    if state:
        return (None, None), {}

    if config and "config" not in metadata:
        metadata["config"] = True

    default = None
    required = True
    if pin_type == PinType.SINGLE:
        if parent is None:
            raise ValueError(
                f"'parent' must be given when getting the interface for a {pin_type} pin"
            )

        no_default = "__no_default__"
        default_value = getattr(parent, field_name, no_default)
        if default_value is not no_default:
            required = False
            default = default_value

    type_adapter, schema, _generics = _get_json_schema_with_generics(field_type)

    return (
        (
            InputPinInterface(
                metadata=metadata,
                sticky=config is not None or (_input and _input.sticky) or False,
                json_schema=schema,
                type=pin_type,
                generics={
                    name: BlockPinRef(
                        port=name if port_name == field_name else port_name,
                        pin="" if port_name == field_name else name,
                    )
                    for name in _generics.keys()
                },
                default=default,
                required=required,
            ),
            type_adapter,
        ),
        _generics,
    )


def _get_state_from_metadata(
    field_type: type,
    field_name: str,
    block_type: "type[Block]",
) -> StateInterface | None:
    state: State | None = None
    metadata: dict[str, Any] = {}

    for m in getattr(field_type, "__metadata__", []):
        if isinstance(m, State):
            state = m

        if isinstance(m, Metadata):
            metadata = m.data

    if state is None:
        return None

    no_default = "__no_default__"
    default = getattr(block_type, field_name, no_default)

    if default is no_default:
        raise ValueError("State() attributes must have a default value")

    state_type = field_type.__args__[0]
    block_type._state_type_adapters[field_name] = _get_type_adapter(state_type)

    return StateInterface(
        metadata=metadata,
        scope=[
            BlockPinRef(port=state.step_id, pin=p)
            for p in state.input_ids or []
            if state.step_id
        ],
        default=default,
    )


def _map_type_vars(
    original_type: type,
) -> tuple[type, dict[TypeVar, tuple[type[BaseModel], dict[str, Any]]]]:
    type_var_defs: dict[TypeVar, tuple[type[BaseModel], dict[str, Any]]] = {}

    def _inner(new_type: type | TypeVar, depth: int) -> type:
        origin = get_origin(new_type)
        if origin == Annotated:
            args = getattr(new_type, "__args__", None)
            if not args:
                return cast(type, new_type)

            new_type = cast(type, args[0])
            return _inner(new_type, depth + 1)

        if isinstance(new_type, TypeVar):
            schema = TypeAdapter(new_type).json_schema(by_alias=True)

            class TempTypeVarModel(BaseModel):
                model_config = ConfigDict(
                    title=new_type.__name__,
                    json_schema_extra=schema,
                )

            type_var_defs[new_type] = (
                TempTypeVarModel,
                schema,
            )
            return TempTypeVarModel

        if depth > 10:
            return new_type

        new_args = []
        args = getattr(new_type, "__args__", None)
        if args:
            for arg in args:
                if isinstance(arg, TypeVar):
                    if arg not in type_var_defs:
                        schema = TypeAdapter(arg).json_schema(by_alias=True)

                        class TempTypeVarModelNested(BaseModel):
                            model_config = ConfigDict(
                                title=arg.__name__,
                                json_schema_extra=schema,
                            )

                        type_var_defs[arg] = (
                            TempTypeVarModelNested,
                            schema,
                        )

                    new_args.append(type_var_defs[arg][0])
                else:
                    new_args.append(_inner(arg, depth + 1))

            if origin:
                if origin is types.UnionType:
                    origin = typing.Union

                class_getitem = getattr(origin, "__class_getitem__", None)
                if class_getitem:
                    n = class_getitem(tuple(new_args))
                    return n

                getitem = getattr(origin, "__getitem__", None)
                if getitem:
                    n = getitem(tuple(new_args))
                    return n

                return new_type
            return new_type

        return new_type

    type_with_generics_replaced = _inner(original_type, 0)
    return type_with_generics_replaced, type_var_defs


class JsonSchemaWithGenerics(NamedTuple):
    type_adapter: TypeAdapter
    schema: dict[str, Any]
    generics: dict[str, dict[str, Any]]


def _get_json_schema_with_generics(t: type) -> JsonSchemaWithGenerics:
    new_t, type_var_map = _map_type_vars(t)

    generics = {
        name.__name__: generic_schema
        for name, (_, generic_schema) in type_var_map.items()
    }

    if new_t == inspect._empty:
        new_t = Any

    type_adapter = TypeAdapter(new_t)
    json_schema = type_adapter.json_schema()

    if "$defs" in json_schema:
        definitions: dict[str, dict[str, Any]] = json_schema["$defs"]
        new_definitions: dict[str, dict[str, Any]] = {}

        json_schema_str = json.dumps(json_schema)
        for name, definition in definitions.items():
            if "TempTypeVarModel" in name and "title" in definition:
                title = definition["title"]
                new_definitions[title] = definition
                json_schema_str = json_schema_str.replace(name, title)

        json_schema = json.loads(json_schema_str)
        json_schema["$defs"] = new_definitions

    elif "title" in json_schema:
        title = json_schema["title"]
        if title in generics:
            json_schema = {"$defs": {title: json_schema}, "$ref": "#/$defs/" + title}

    return JsonSchemaWithGenerics(
        type_adapter=type_adapter,
        schema=json_schema,
        generics=generics,
    )


class PinsSet(NamedTuple):
    inputs: dict[str, InputPinInterface]
    outputs: dict[str, OutputPinInterface]
    generics: dict[str, dict[str, Any]]


def _get_pins(
    cls_annotation: type,
    port_name: str,
    block_type: "type[Block]",
):
    # TODO add checking if cls is a tool to this method, similar to checking for Output

    cls_metadata = getattr(cls_annotation, "__metadata__", [])
    if len(cls_metadata):
        cls = cls_annotation.__args__[0]
        metadata = first([a.data for a in cls_metadata if isinstance(a, Metadata)], {})
    else:
        cls = cls_annotation
        metadata = {}

    all_bases = _get_all_bases(cls) + [cls, cls_annotation]

    inputs: dict[str, InputPinInterface] = {}
    outputs: dict[str, OutputPinInterface] = {}
    generics: dict[str, dict[str, Any]] = {}

    for base_type in all_bases:
        o = get_origin(base_type)
        if o is Output:
            args = getattr(base_type, "__args__", None)
            if not args or len(args) != 1:
                raise Exception("Outputs must have exactly one type.")

            type_adapter, schema, _generics = _get_json_schema_with_generics(args[0])
            generics.update(_generics)

            outputs[""] = OutputPinInterface(
                metadata=metadata,
                json_schema=schema,
                type=PinType.SINGLE,
                generics={
                    name: BlockPinRef(port=name, pin="") for name in _generics.keys()
                },
            )

    if _issubclass(cls_annotation, Tool):
        tool_type = cast(Tool, base_type)

        (_input, input_adapter), _outputs, output_adapters, _generics = _get_tool_pins(
            tool_type.run, port_name=port_name
        )
        outputs.update(_outputs)
        for name, adapter in output_adapters.items():
            block_type._set_output_pin_type_adapter(port_name, name, adapter)

        if _input and input_adapter:
            block_type._set_input_pin_type_adapter(port_name, "return", input_adapter)
            inputs["return"] = _input

        for generic_name, generic_schema in _generics.items():
            type_adapter = TypeAdapter(dict[str, Any])
            block_type._set_input_pin_type_adapter(
                port_name, generic_name, type_adapter
            )

            inputs[generic_name] = InputPinInterface(
                metadata={"generic": True},
                sticky=True,
                json_schema=type_adapter.json_schema(),
                generics={},
                type=PinType.SINGLE,
                required=False,
                default=generic_schema,
            )

    (input_pin, input_adapter), _generics = _get_input_pin_from_metadata(
        base_type,
        port_name=port_name,
        field_name=port_name,
        parent=block_type,
        pin_type=PinType.SINGLE,
    )
    if isinstance(input_pin, InputPinInterface) and input_adapter:
        inputs[""] = input_pin
        block_type._set_input_pin_type_adapter(port_name, "", input_adapter)
        generics.update(_generics)

    annotations = {}
    for base in all_bases:
        annotations.update(getattr(base, "__annotations__", {}))
    annotations.update(**getattr(cls, "__annotations__", {}))

    for field_name, field_annotation in annotations.items():
        field_metadata = getattr(field_annotation, "__metadata__", [])
        if len(field_metadata):
            field_type = field_annotation.__args__[0]
            metadata = first(
                [a.data for a in field_metadata if isinstance(a, Metadata)], {}
            )
        else:
            metadata = {}
            field_type = field_annotation

        o = get_origin(field_type)
        if o is Output:
            args = getattr(field_type, "__args__", None)
            if not args or len(args) != 1:
                raise Exception("Outputs must have exactly one type.")

            type_adapter, schema, _generics = _get_json_schema_with_generics(args[0])
            block_type._set_output_pin_type_adapter(port_name, field_name, type_adapter)
            for generic_name, generic_schema in _generics.items():
                type_adapter = TypeAdapter(dict[str, Any])
                block_type._set_input_pin_type_adapter(
                    port_name, generic_name, type_adapter
                )

                inputs[generic_name] = InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=type_adapter.json_schema(),
                    generics={},
                    type=PinType.SINGLE,
                    required=False,
                    default=generic_schema,
                )

            outputs[field_name] = OutputPinInterface(
                metadata=metadata,
                json_schema=schema,
                type=PinType.SINGLE,
                generics={
                    name: BlockPinRef(port=port_name, pin=name)
                    for name in _generics.keys()
                },
            )

        elif o is dict:
            dict_args = getattr(field_type, "__args__", None)
            if dict_args:
                item_type: type = dict_args[1]

                if _issubclass(item_type, Output):
                    if dict_args[0] is not str:
                        raise TypeError("Output dictionaries must have str keys")

                    args = getattr(field_type, "__args__", None)
                    if not args or len(args) != 1:
                        raise Exception("Outputs must have exactly one type.")

                    type_adapter, schema, _generics = _get_json_schema_with_generics(
                        args[0]
                    )
                    block_type._set_output_pin_type_adapter(
                        port_name, field_name, type_adapter
                    )
                    for generic_name, generic_schema in _generics.items():
                        type_adapter = TypeAdapter(dict[str, Any])
                        block_type._set_input_pin_type_adapter(
                            port_name, generic_name, type_adapter
                        )

                        inputs[generic_name] = InputPinInterface(
                            metadata={"generic": True},
                            sticky=True,
                            json_schema=type_adapter.json_schema(),
                            generics={},
                            type=PinType.SINGLE,
                            required=False,
                            default=generic_schema,
                        )

                    outputs[field_name] = OutputPinInterface(
                        metadata=metadata,
                        json_schema=schema,
                        type=PinType.DICTIONARY,
                        generics={
                            name: BlockPinRef(port=port_name, pin=name)
                            for name in _generics.keys()
                        },
                    )
                else:
                    (input_pin, input_adapter), _generics = (
                        _get_input_pin_from_metadata(
                            item_type,
                            port_name=port_name,
                            field_name=field_name,
                            parent=cls,
                            pin_type=PinType.DICTIONARY,
                        )
                    )
                    if isinstance(input_pin, InputPinInterface) and input_adapter:
                        block_type._set_input_pin_type_adapter(
                            port_name, field_name, input_adapter
                        )
                        inputs[field_name] = input_pin
                        for generic_name, generic_schema in _generics.items():
                            type_adapter = TypeAdapter(dict[str, Any])
                            block_type._set_input_pin_type_adapter(
                                port_name, generic_name, type_adapter
                            )

                            inputs[generic_name] = InputPinInterface(
                                metadata={"generic": True},
                                sticky=True,
                                json_schema=type_adapter.json_schema(),
                                generics={},
                                type=PinType.SINGLE,
                                required=False,
                                default=generic_schema,
                            )

        elif o is list:
            list_args = getattr(field_type, "__args__", None)
            if list_args:
                item_type: type = list_args[0]

                if _issubclass(item_type, Output):
                    args = getattr(field_type, "__args__", None)
                    if not args or len(args) != 1:
                        raise Exception("Outputs must have exactly one type.")

                    type_adapter, schema, _generics = _get_json_schema_with_generics(
                        args[0]
                    )
                    block_type._set_output_pin_type_adapter(
                        port_name, field_name, type_adapter
                    )

                    for generic_name, generic_schema in _generics.items():
                        type_adapter = TypeAdapter(dict[str, Any])
                        block_type._set_input_pin_type_adapter(
                            port_name, generic_name, type_adapter
                        )

                        inputs[generic_name] = InputPinInterface(
                            metadata={"generic": True},
                            sticky=True,
                            json_schema=type_adapter.json_schema(),
                            generics={},
                            type=PinType.SINGLE,
                            required=False,
                            default=generic_schema,
                        )

                    outputs[field_name] = OutputPinInterface(
                        metadata=metadata,
                        json_schema=schema,
                        type=PinType.LIST,
                        generics={
                            name: BlockPinRef(port=port_name, pin=name)
                            for name in _generics.keys()
                        },
                    )
                else:
                    (input_pin, input_adapter), _generics = (
                        _get_input_pin_from_metadata(
                            item_type,
                            port_name=port_name,
                            field_name=field_name,
                            parent=cls,
                            pin_type=PinType.LIST,
                        )
                    )
                    if isinstance(input_pin, InputPinInterface) and input_adapter:
                        block_type._set_input_pin_type_adapter(
                            port_name, field_name, input_adapter
                        )
                        inputs[field_name] = input_pin
                        for generic_name, generic_schema in _generics.items():
                            type_adapter = TypeAdapter(dict[str, Any])
                            block_type._set_input_pin_type_adapter(
                                port_name, generic_name, type_adapter
                            )

                            inputs[generic_name] = InputPinInterface(
                                metadata={"generic": True},
                                sticky=True,
                                json_schema=type_adapter.json_schema(),
                                generics={},
                                type=PinType.SINGLE,
                                required=False,
                                default=generic_schema,
                            )

        (input_pin, input_adapter), _generics = _get_input_pin_from_metadata(
            field_annotation,
            port_name=port_name,
            field_name=field_name,
            parent=cls,
            pin_type=PinType.SINGLE,
        )
        if isinstance(input_pin, InputPinInterface) and input_adapter:
            inputs[field_name] = input_pin
            block_type._set_input_pin_type_adapter(port_name, field_name, input_adapter)
            for generic_name, generic_schema in _generics.items():
                type_adapter = TypeAdapter(dict[str, Any])
                block_type._set_input_pin_type_adapter(
                    port_name, generic_name, type_adapter
                )

                inputs[generic_name] = InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=type_adapter.json_schema(),
                    generics={},
                    type=PinType.SINGLE,
                    required=False,
                    default=generic_schema,
                )

    for field_name, field_annotation in annotations.items():
        field_metadata = getattr(field_annotation, "__metadata__", [])
        if len(field_metadata):
            field_type = field_annotation.__args__[0]
            metadata = first(
                [a.data for a in field_metadata if isinstance(a, Metadata)], {}
            )
        else:
            metadata = {}
            field_type = field_annotation

        origin = get_origin(field_type)

        if origin is GenericSetter:
            type_var = field_type.__args__[0]
            generic_name = type_var.__name__
            if generic_name in inputs:
                inputs[field_name] = inputs[generic_name]
                del inputs[generic_name]

            inputs[field_name].metadata["hidden"] = False
            inputs[field_name].metadata.update(metadata)

            block_type._input_pin_type_adapters[port_name][field_name] = (
                block_type._input_pin_type_adapters[port_name][generic_name]
            )
            del block_type._input_pin_type_adapters[port_name][generic_name]

            for pin in list(inputs.values()) + list(outputs.values()):
                for g, pin_ref in pin.generics.items():
                    if (
                        g == generic_name
                        and pin_ref.port == port_name
                        and pin_ref.pin == generic_name
                    ):
                        pin.generics[g] = BlockPinRef(port=port_name, pin=field_name)

    return PinsSet(inputs, outputs, generics)


class PortsAndState(NamedTuple):
    ports: dict[str, PortInterface]
    state: dict[str, StateInterface]


def _get_ports_and_state(block_type: "type[Block]"):
    annotations = {}
    for base in _get_all_bases(block_type):
        base_annotations = getattr(base, "__annotations__", {})
        annotations.update(base_annotations)
    annotations.update(**block_type.__annotations__)

    ports: dict[str, PortInterface] = {}
    state: dict[str, StateInterface] = {}
    generics: dict[str, dict[str, Any]] = {}

    for port_name, port_annotation in annotations.items():
        port_annotations = getattr(port_annotation, "__metadata__", None)
        if port_annotations:
            field_type = port_annotation.__args__[0]
            metadata = first(
                [a.data for a in port_annotations if isinstance(a, Metadata)], {}
            )
        else:
            field_type = port_annotation
            metadata = {}

        o = get_origin(field_type)
        if o is dict:
            dict_args = getattr(field_type, "__args__", None)
            if dict_args:
                item_type: type = dict_args[1]

                input_pins, output_pins, _generics = _get_pins(
                    item_type, port_name=port_name, block_type=block_type
                )
                if len(input_pins) or len(output_pins):
                    generics.update(_generics)

                    if dict_args[0] is not str:
                        raise TypeError("Port dictionaries must have str keys")

                    ports[port_name] = PortInterface(
                        metadata=metadata,
                        inputs=input_pins,
                        outputs=output_pins,
                        type=PortType.DICTIONARY,
                        is_function=False,
                    )

                    continue

        elif o is list:
            list_args = getattr(field_type, "__args__", None)
            if list_args:
                item_type: type = list_args[0]

                input_pins, output_pins, _generics = _get_pins(
                    item_type, port_name=port_name, block_type=block_type
                )
                if len(input_pins) or len(output_pins):
                    generics.update(_generics)
                    ports[port_name] = PortInterface(
                        metadata=metadata,
                        inputs=input_pins,
                        outputs=output_pins,
                        type=PortType.LIST,
                        is_function=False,
                    )

                    continue

        input_pins, output_pins, _generics = _get_pins(
            port_annotation, port_name=port_name, block_type=block_type
        )
        if len(input_pins) or len(output_pins):
            generics.update(_generics)
            ports[port_name] = PortInterface(
                metadata=metadata,
                inputs=input_pins,
                outputs=output_pins,
                type=PortType.SINGLE,
                is_function=False,
            )
        else:
            s = _get_state_from_metadata(
                field_type=port_annotation,
                field_name=port_name,
                block_type=block_type,
            )
            if s:
                state[port_name] = s

    for generic_name, generic_schema in generics.items():
        type_adapter = TypeAdapter(dict[str, Any])
        block_type._set_input_pin_type_adapter(generic_name, "", type_adapter)

        ports[generic_name] = PortInterface(
            metadata={},
            inputs={
                "": InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=type_adapter.json_schema(),
                    generics={},
                    type=PinType.SINGLE,
                    required=False,
                    default=generic_schema,
                )
            },
            outputs={},
            type=PortType.SINGLE,
            is_function=False,
        )

    return ports, state


class Input(BaseModel):
    sticky: bool = False


class State:
    def __init__(
        self,
        step_id: str | None = None,
        input_ids: list[str] | None = None,
    ):
        self.step_id = step_id
        self.input_ids = input_ids


class BlockControlMessage(enum.Enum):
    DONE = "Done"


block_messages: contextvars.ContextVar[
    asyncio.queues.Queue[BlockMessage | BlockControlMessage]
] = contextvars.ContextVar("block_messages")


class Output(Generic[T]):
    def __init__(self, pin: BlockPinRef):
        self.pin = pin

    def send(self, value: T):
        messages = block_messages.get()
        messages.put_nowait(
            BlockMessage(
                outputs=[
                    OutputValue(
                        source=self.pin,
                        value=value,
                    )
                ],
                inputs=[],
                redirects=[],
                states=[],
            )
        )


class BlockError(BaseModel):
    message: str
    data: Any = None


class MetaBlock(type):
    _all_block_types: "ClassVar[dict[str, list[type[Block]]]]" = {}

    def __new__(cls, name, bases, attrs):
        block_type = super().__new__(cls, name, bases, attrs)
        if (
            name != "Block"
            and name != "WorkSpaceBlock"
            and not inspect.isabstract(block_type)
        ):
            block_type.name = block_type.__name__.split("_")[0]
            if block_type.name not in cls._all_block_types:
                cls._all_block_types[block_type.name] = []

            cls._all_block_types[block_type.name].append(block_type)

        return block_type

    def __init__(self, name, bases, attrs):
        super().__init__(name, bases, attrs)

        self.metadata: dict[str, Any] = {}
        self.name: str
        self._version: str | None = None
        self._semantic_version: semantic_version.Version | None = None
        self._all_annotations_cache: dict[str, type] | None = None
        self._class_interface: BlockInterface | None = None
        self._input_pin_type_adapters: dict[str, dict[str, TypeAdapter]] = {}
        self._output_pin_type_adapters: dict[str, dict[str, TypeAdapter]] = {}
        self._state_type_adapters: dict[str, TypeAdapter] = {}

    def find(self, name: str, version: str) -> "type[Block] | None":
        spec = semantic_version.NpmSpec(version)
        if name not in self._all_block_types:
            return None

        versions = {v.semantic_version: v for v in self._all_block_types[name]}
        best_version = spec.select(versions.keys())

        if best_version is None:
            return None

        return versions[best_version]

    def _set_input_pin_type_adapter(
        self, port: str, pin: str, type_adapter: TypeAdapter
    ):
        if port not in self._input_pin_type_adapters:
            self._input_pin_type_adapters[port] = {}

        self._input_pin_type_adapters[port][pin] = type_adapter

    def _set_output_pin_type_adapter(
        self, port: str, pin: str, type_adapter: TypeAdapter
    ):
        if port not in self._output_pin_type_adapters:
            self._output_pin_type_adapters[port] = {}

        self._output_pin_type_adapters[port][pin] = type_adapter

    def _get_interface(cls):
        if cls._class_interface is None:
            ports, state = _get_ports_and_state(cls)

            for attribute_name in dir(cls):
                attribute = getattr(cls, attribute_name)

                if type(attribute) is Step or type(attribute) is Callback:
                    (inputs, input_adapters, (output, output_adapter), generics) = (
                        _get_function_pins(attribute._fn)
                    )

                    if type(attribute) is Step and output_adapter and output:
                        cls._set_output_pin_type_adapter(
                            attribute_name, attribute._output_name, output_adapter
                        )
                        outputs = {attribute._output_name: output}
                    else:
                        outputs = {}

                    for name, adapter in input_adapters.items():
                        cls._set_input_pin_type_adapter(attribute_name, name, adapter)

                    ports[attribute_name] = PortInterface(
                        metadata=attribute.metadata,
                        inputs=inputs,
                        outputs=outputs,
                        is_function=True,
                        type=PortType.SINGLE,
                    )

                    for generic_name, generic_schema in generics.items():
                        type_adapter = TypeAdapter(dict[str, Any])
                        cls._set_input_pin_type_adapter(generic_name, "", type_adapter)

                        ports[generic_name] = PortInterface(
                            metadata={},
                            inputs={
                                "": InputPinInterface(
                                    metadata={"generic": True},
                                    sticky=True,
                                    json_schema=type_adapter.json_schema(),
                                    generics={},
                                    type=PinType.SINGLE,
                                    required=False,
                                    default=generic_schema,
                                )
                            },
                            outputs={},
                            type=PortType.SINGLE,
                            is_function=False,
                        )

            annotations = {}
            for base in _get_all_bases(cls):
                base_annotations = getattr(base, "__annotations__", {})
                annotations.update(base_annotations)
            annotations.update(**cls.__annotations__)

            for port_name, port_annotation in annotations.items():
                port_metadata = getattr(port_annotation, "__metadata__", [])
                if len(port_metadata):
                    field_type = port_annotation.__args__[0]
                    metadata = first(
                        [a.data for a in port_metadata if isinstance(a, Metadata)], {}
                    )
                else:
                    metadata = {}
                    field_type = port_annotation

                origin = get_origin(field_type)

                if origin is GenericSetter:
                    type_var = field_type.__args__[0]
                    generic_name = type_var.__name__
                    if generic_name in ports:
                        ports[port_name] = ports[generic_name]
                        del ports[generic_name]

                    ports[port_name].inputs[""].metadata["hidden"] = False
                    ports[port_name].inputs[""].metadata.update(metadata)

                    cls._input_pin_type_adapters[port_name][""] = (
                        cls._input_pin_type_adapters[port_name][generic_name]
                    )
                    del cls._input_pin_type_adapters[port_name][generic_name]

                    for port in ports.values():
                        for pin in list(port.inputs.values()) + list(
                            port.outputs.values()
                        ):
                            for g, pin_ref in pin.generics.items():
                                if (
                                    g == generic_name
                                    and pin_ref.port == generic_name
                                    and pin_ref.pin == ""
                                ):
                                    pin.generics[g] = BlockPinRef(
                                        port=port_name, pin=""
                                    )

            cls._class_interface = BlockInterface(
                metadata=cls.metadata,
                ports=ports,
                state=state,
            )

        return cls._class_interface

    @property
    def semantic_version(cls):
        if not cls._semantic_version:
            version_str = (
                cls._version or ".".join(cls.__name__.split("_")[1:]) or "1.0.0"
            )
            cls._semantic_version = semantic_version.Version.coerce(version_str)

        return cls._semantic_version

    @property
    def version(cls):
        return str(cls.semantic_version)

    @property
    def _all_annotations(cls):
        if not cls._all_annotations_cache:
            cls._all_annotations_cache = {}
            for base in _get_all_bases(cls):
                base_annotations = getattr(base, "__annotations__", {})
                cls._all_annotations_cache.update(base_annotations)
            cls._all_annotations_cache.update(**cls.__annotations__)

        return cls._all_annotations_cache


def _set_input_pin_value_on_port(
    port: Any,
    pin_name: str,
    pin_index: str | None,
    pin_interface: InputPinInterface,
    value: Any,
):
    if pin_interface.type == PinType.SINGLE:
        setattr(port, pin_name, value)

    elif pin_interface.type == PinType.LIST:
        try:
            pin_index_int = int(pin_index or "")
        except ValueError:
            raise ValueError("Indexes on list Pins must be valid integers")
        pin_list = getattr(port, pin_name, None)
        if not pin_list:
            pin_list = []
            setattr(port, pin_name, pin_list)

        if len(pin_list) < pin_index_int:
            pin_list.extend([None] * (pin_index_int - len(pin_list)))
            pin_list.append(value)
        elif len(pin_list) == pin_index_int:
            pin_list.append(value)
        else:
            pin_list[pin_index_int] = value

    elif pin_interface.type == PinType.DICTIONARY:
        pin_dict = getattr(port, pin_name, None)
        if not pin_dict:
            pin_dict = {}
            setattr(port, pin_name, pin_dict)

        pin_dict[pin_index] = value


class Block(metaclass=MetaBlock):
    error: Annotated[Output[BlockError], Metadata(hidden=True)]

    def __init__(self):
        self._interface = self.interface()

        self._messages: list[BlockMessage] = []
        self._dynamic_ports: dict[str, list[str]] = {}
        self._dynamic_inputs: list[tuple[tuple[str, str], tuple[str, str]]] = []
        self._dynamic_outputs: list[tuple[tuple[str, str], tuple[str, str]]] = []

        for attribute_name in dir(self):
            attribute = getattr(self, attribute_name)

            if _issubclass(type(attribute), BlockFunction):
                attribute._block = self

        self._create_all_ports()

    def _run_function(self, name: str):
        function = getattr(self, name, None)
        if function is None:
            raise ValueError(f"Could not find function '{name}'")

        if not isinstance(function, BlockFunction):
            raise ValueError(f"'{name}' is not a BlockFunction")

        return function._run()

    def _load(
        self,
        context: BlockContext | None = None,
        state: list[StateValue] | None = None,
        inputs: list[InputValue] | None = None,
        dynamic_outputs: list[BlockPinRef] | None = None,
        dynamic_inputs: list[BlockPinRef] | None = None,
    ):
        if (dynamic_inputs and len(dynamic_inputs)) or (
            dynamic_outputs and len(dynamic_outputs)
        ):
            self._create_all_ports(dynamic_inputs, dynamic_outputs)

        if context:
            self._set_context(context)

        if state:
            self._set_state(state)

        if inputs:
            self._set_inputs(inputs)

    def get_messages(self):
        return copy.copy(self._messages)

    @classmethod
    def interface(cls):
        return cls._get_interface().model_copy(deep=True)

    def _create_all_ports(
        self,
        dynamic_inputs: list[BlockPinRef] | None = None,
        dynamic_outputs: list[BlockPinRef] | None = None,
    ):
        for port_name, port_interface in self._interface.ports.items():
            if (
                port_interface.type == PortType.LIST
                or port_interface.type == PortType.DICTIONARY
            ):
                self._dynamic_ports[port_name] = []

        if dynamic_inputs:
            for i in dynamic_inputs:
                port_path = i.port.split(".")
                pin_path = i.port.split(".")

                port_name = port_path[0]
                port_index = port_path[1] if len(port_path) == 2 else ""
                self._dynamic_ports[port_name].append(port_index)

                self._dynamic_inputs.append(
                    (
                        (port_name, port_index),
                        (pin_path[0], pin_path[1] if len(pin_path) == 2 else ""),
                    )
                )

        if dynamic_outputs:
            for i in dynamic_outputs:
                port_path = i.port.split(".")
                pin_path = i.port.split(".")

                port_name = port_path[0]
                port_index = port_path[1] if len(port_path) == 2 else ""
                self._dynamic_ports[port_name].append(port_index)

                self._dynamic_outputs.append(
                    (
                        (port_name, port_index),
                        (pin_path[0], pin_path[1] if len(pin_path) == 2 else ""),
                    )
                )

        for port_name, port_interface in self._interface.ports.items():
            if port_interface.type == PortType.SINGLE:
                setattr(
                    self,
                    port_name,
                    self._create_port(port_name, ""),
                )

            elif port_interface.type == PortType.LIST:
                port_indexes = [
                    int(port_index) for port_index in self._dynamic_ports[port_name]
                ]

                port_list: list[Any] = [None] * (max(port_indexes, default=-1) + 1)
                for port_index in port_indexes:
                    port_list[port_index] = self._create_port(
                        port_name, str(port_index)
                    )
                setattr(self, port_name, port_list)

            elif port_interface.type == PortType.DICTIONARY:
                port_dict = {
                    port_index: self._create_port(port_name, port_index)
                    for port_index in self._dynamic_ports[port_name]
                }
                setattr(self, port_name, port_dict)

    def _set_context(self, context: BlockContext): ...

    def _set_state(self, state: list[StateValue]):
        for s in state:
            adapter = self.__class__._state_type_adapters[s.state]
            value = adapter.validate_python(s.value)
            setattr(self, s.state, value)

    def _set_inputs(self, inputs: list[InputValue]):
        for input_value in inputs:
            port_path = input_value.target.port.split(".")
            port_name = port_path[0]
            if len(port_path) > 1:
                port_index = port_path[1]
            else:
                port_index = ""

            pin_path = input_value.target.pin.split(".")
            pin_name = pin_path[0]
            if len(pin_path) > 1:
                pin_index = pin_path[1]
            else:
                pin_index = ""

            adapter = self.__class__._input_pin_type_adapters[port_name][pin_name]
            value = adapter.validate_python(input_value.value)

            if (
                port_name in self._interface.ports
                and pin_name in self._interface.ports[port_name].inputs
            ):
                port_interface = self._interface.ports[port_name]
                pin_interface = port_interface.inputs[pin_name]

                if port_interface.is_function:
                    port: BlockFunction = getattr(self, port_name)
                    if pin_name not in port._pending_inputs:
                        port._pending_inputs[pin_name] = {}
                    port._pending_inputs[pin_name][pin_index] = value

                else:
                    if pin_name == "":
                        assert pin_interface.type == PinType.SINGLE

                        if port_interface.type == PortType.SINGLE:
                            setattr(self, port_name, value)

                        elif port_interface.type == PortType.LIST:
                            try:
                                port_index_int = int(port_index)
                            except ValueError:
                                raise ValueError(
                                    "Indexes on list Ports must be valid integers"
                                )

                            port_list: list[Any] = getattr(self, port_name)

                            if len(port_list) < port_index_int:
                                port_list.extend(
                                    [None] * (port_index_int - len(port_list))
                                )
                                port_list.append(value)
                            elif len(port_list) == port_index_int:
                                port_list.append(value)
                            else:
                                port_list[port_index_int] = value

                        elif port_interface.type == PortType.DICTIONARY:
                            port_dict: dict[str, Any] = getattr(self, port_name)
                            port_dict[port_index] = value
                    else:
                        if port_interface.type == PortType.SINGLE:
                            port = getattr(self, port_name)

                        elif port_interface.type == PortType.LIST:
                            try:
                                port_index_int = int(port_index)
                            except ValueError:
                                raise ValueError(
                                    "Indexes on list Ports must be valid integers"
                                )

                            port_list = getattr(self, port_name)
                            port = port_list[port_index_int]

                        elif port_interface.type == PortType.DICTIONARY:
                            port_dict = getattr(self, port_name)
                            port = port_dict[port_index]

                        _set_input_pin_value_on_port(
                            port,
                            pin_name=pin_name,
                            pin_index=pin_index,
                            pin_interface=pin_interface,
                            value=value,
                        )

    def _create_port(
        self,
        port_name: str,
        port_index: str,
    ) -> Any:
        port_interface = self._interface.ports[port_name]
        dynamic_inputs: list[tuple[str, str]] = []
        for (_port_name, _port_index), (
            _input_name,
            _input_index,
        ) in self._dynamic_inputs:
            if _port_name == port_name and _port_index == port_index:
                dynamic_inputs.append((_input_name, _input_index))

        dynamic_outputs: list[tuple[str, str]] = []
        for (_port_name, _port_index), (
            _output_name,
            _output_index,
        ) in self._dynamic_outputs:
            if _port_name == port_name and _port_index == port_index:
                dynamic_outputs.append((_output_name, _output_index))

        if len(port_interface.inputs) + len(port_interface.outputs) == 1 and (
            "" in port_interface.inputs or "" in port_interface.outputs
        ):
            if "" in port_interface.inputs:
                input_interface = port_interface.inputs[""]
                type_adapter = self._input_pin_type_adapters[port_name][""]
                return (
                    None
                    if input_interface.default is None
                    else type_adapter.validate_python(input_interface.default)
                )
            elif "" in port_interface.outputs:
                return Output(BlockPinRef(port=port_name, pin=""))

        if port_interface.is_function:
            port = getattr(self, port_name)
        else:
            annotation = self.__class__._all_annotations[port_name]
            if port_interface.type == PortType.SINGLE:
                port_type = annotation
            else:
                if annotation == Annotated:
                    annotation = annotation.__args__[0]

                if port_interface.type == PortType.LIST:
                    port_type = annotation.__args__[0]
                elif port_interface.type == PortType.DICTIONARY:
                    port_type = annotation.__args__[1]

            if _issubclass(port_type, Tool):
                port = port_type(port_name=port_name)
            else:
                port = port_type()

        for input_name, input_interface in port_interface.inputs.items():
            type_adapter = self._input_pin_type_adapters[port_name][input_name]

            if input_interface.type == PinType.SINGLE:
                setattr(
                    port,
                    input_name,
                    None
                    if input_interface.default is None
                    else type_adapter.validate_python(input_interface.default),
                )

            elif input_interface.type == PinType.LIST:
                _dynamic_inputs = [
                    int(index)
                    for _input_name, index in dynamic_inputs
                    if _input_name == input_name
                ]
                inputs = [None] * (max(_dynamic_inputs, default=-1) + 1)

                for index in _dynamic_inputs:
                    inputs[index] = (
                        None
                        if input_interface.default is None
                        else type_adapter.validate_python(input_interface.default)
                    )

                setattr(port, input_name, inputs)

            elif input_interface.type == PinType.DICTIONARY:
                input_dict = {
                    index: None
                    if input_interface.default is None
                    else type_adapter.validate_python(input_interface.default)
                    for _input_name, index in dynamic_inputs
                    if _input_name == input_name
                }
                setattr(port, input_name, input_dict)

        for output_name, output_interface in port_interface.outputs.items():
            if output_interface.type == PinType.SINGLE:
                setattr(
                    port,
                    input_name,
                    Output(BlockPinRef(port=port_name, pin=output_name)),
                )

            elif output_interface.type == PinType.LIST:
                _dynamic_outputs = [
                    int(index)
                    for _output_name, index in dynamic_outputs
                    if _output_name == output_name
                ]
                outputs: list[None | Output] = [None] * (
                    max(_dynamic_outputs, default=-1) + 1
                )

                for index in _dynamic_outputs:
                    outputs[index] = Output(
                        BlockPinRef(port=port_name, pin=output_name)
                    )

                setattr(port, output_name, outputs)

            elif output_interface.type == PinType.DICTIONARY:
                output_dict: dict[str, Output] = {
                    index: Output(BlockPinRef(port=port_name, pin=output_name))
                    for _output_name, index in dynamic_outputs
                    if _output_name == output_name
                }
                setattr(port, output_name, output_dict)

        return port


class WorkSpaceBlock(Block):
    workspace: SmartSpaceWorkspace
    message_history: list[ThreadMessage]

    def _set_context(self, context: BlockContext):
        assert context.workspace is not None, "Workspace is None in a WorkSpaceBlock"
        assert (
            context.message_history is not None
        ), "Workspace is None in a WorkSpaceBlock"

        self.workspace = context.workspace
        self.message_history = context.message_history


class DummyToolValue: ...


class CallbackCall(NamedTuple):
    name: str
    other_params: dict[str, Any]
    dummy_value_param: str


class ToolCall(Generic[R]):
    def __init__(self, parent: "Tool[P, T]", outputs: list[OutputValue]):
        self.parent = parent
        self.outputs = outputs
        self.inputs = []
        self.redirects: list[PinRedirect] = []

    def then(
        self,
        callback: Callable[[R], CallbackCall],
    ) -> "ToolCall[R]":
        callback_name, other_params, dummy_value_param = callback(
            cast(R, DummyToolValue())
        )

        for name, value in other_params.items():
            self.inputs.append(
                InputValue(
                    target=BlockPinRef(
                        port=callback_name,
                        pin=name,
                    ),
                    value=value,
                )
            )

        self.redirects.append(
            PinRedirect(
                source=BlockPinRef(
                    port=self.parent.port_name,
                    pin="return",
                ),
                target=BlockPinRef(
                    port=callback_name,
                    pin=dummy_value_param,
                ),
            )
        )

        return self

    def __await__(self):
        messages = block_messages.get()
        messages.put_nowait(
            BlockMessage(
                outputs=self.outputs,
                redirects=self.redirects,
                inputs=self.inputs,
                states=[],
            )
        )
        yield


class Tool(Generic[P, T], abc.ABC):
    metadata: ClassVar[dict] = {}

    def __init__(self, port_name: str):
        self.port_name = port_name

    @abc.abstractmethod
    def run(self, *args: P.args, **kwargs: P.kwargs) -> T: ...

    def call(self, *args: P.args, **kwargs: P.kwargs) -> ToolCall[T]:
        s = inspect.signature(self.__class__.run)
        binding = s.bind(self, *args, **kwargs)
        binding.apply_defaults()

        single_outputs: list[OutputValue] = []
        list_outputs: list[OutputValue] = []
        dictionary_outputs: list[OutputValue] = []

        for name, p in s.parameters.items():
            if name == "self":
                continue

            value = binding.arguments[name]

            if p.kind == p.POSITIONAL_OR_KEYWORD or p.kind == p.KEYWORD_ONLY:
                single_outputs.append(
                    OutputValue(
                        source=BlockPinRef(
                            port=self.port_name,
                            pin=name,
                        ),
                        value=value,
                    )
                )
            elif p.kind == p.VAR_POSITIONAL:
                for i, v in enumerate(value):
                    list_outputs.append(
                        OutputValue(
                            source=BlockPinRef(
                                port=self.port_name,
                                pin=f"{name}.{i}",
                            ),
                            value=v,
                        )
                    )
            elif p.kind == p.VAR_KEYWORD:
                value = cast(dict[str, Any], value)
                for i, v in value.items():
                    dictionary_outputs.append(
                        OutputValue(
                            source=BlockPinRef(
                                port=self.port_name,
                                pin=f"{name}.{i}",
                            ),
                            value=v,
                        )
                    )

        outputs = single_outputs + list_outputs + dictionary_outputs
        return ToolCall(
            parent=self,
            outputs=outputs,
        )


class BlockFunctionCall:
    def __init__(
        self,
        values: asyncio.queues.Queue[BlockMessage | BlockControlMessage],
        step: Awaitable,
    ):
        self.values = values
        self.step = step

    def __aiter__(self):
        self.step_future = asyncio.tasks.ensure_future(self.step)
        self.step_future.add_done_callback(
            lambda _: self.values.put_nowait(BlockControlMessage.DONE)
        )
        return self

    async def __anext__(self):
        value = await self.values.get()

        if isinstance(value, BlockControlMessage):
            if value == BlockControlMessage.DONE:
                exc = self.step_future.exception()
                if exc:
                    raise exc

                raise StopAsyncIteration
            else:
                raise ValueError(f"Unexpected BlockControlMessage {value}")
        elif isinstance(value, BlockMessage):
            return value
        else:
            raise ValueError(f"Unexpected BlockMessage {value}")


class BlockFunction(Generic[B, P, T]):
    def __init__(
        self,
        fn: Callable[Concatenate[B, P], Awaitable[T]],
    ):
        self.name = fn.__name__
        self._fn = fn
        self.metadata: dict = {}
        self._block: B
        self._pending_inputs: dict[str, dict[str, Any]] = {}

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        messages: asyncio.queues.Queue[BlockMessage | BlockControlMessage] = (
            asyncio.queues.Queue()
        )
        block_messages.set(messages)

        result = await self._fn(self._block, *args, **kwargs)

        while not messages.empty():
            m = await messages.get()
            if isinstance(m, BlockMessage):
                self._block._messages.append(m)

        return result

    def _run(self):
        messages: asyncio.queues.Queue[BlockMessage | BlockControlMessage] = (
            asyncio.queues.Queue()
        )
        block_messages.set(messages)

        s = inspect.signature(self._fn)

        positional_inputs: list[Any] = []
        var_positional_inputs: list[Any] = []
        keyword_inputs: dict[str, Any] = {}

        for name, p in s.parameters.items():
            if name == "self" or name not in self._pending_inputs:
                continue

            values = self._pending_inputs[name]

            if p.kind == p.POSITIONAL_OR_KEYWORD or p.kind == p.KEYWORD_ONLY:
                if "" in values:
                    positional_inputs.append(
                        values[""]
                    )  # s.parameters is an ordered dict
            elif p.kind == p.VAR_POSITIONAL:
                indexed_values = {int(index): value for index, value in values.items()}
                var_positional_inputs = [None] * (
                    max(indexed_values.keys(), default=-1) + 1
                )
                for i, v in indexed_values.items():
                    var_positional_inputs[i] = v

            elif p.kind == p.VAR_KEYWORD:
                keyword_inputs.update(values)

        return BlockFunctionCall(
            messages,
            self._fn(
                self._block,
                *tuple(positional_inputs + var_positional_inputs),
                **keyword_inputs,
            ),
        )


class Step(BlockFunction[B, P, T]):
    def __init__(
        self,
        fn: Callable[Concatenate[B, P], Awaitable[T]],
        output_name: str | None = None,
    ):
        super().__init__(fn)
        self._output_name = output_name or ""


class Callback(BlockFunction[B, P, None]):
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> CallbackCall:
        values = inspect.getcallargs(self._fn, cast(Block, None), *args, **kwargs)

        tool_result_param = ""
        direct_params: dict[str, Any] = {}

        for arg_name, value in values.items():
            if isinstance(value, DummyToolValue):
                tool_result_param = arg_name
            elif arg_name != "self":
                direct_params[arg_name] = value

        return CallbackCall(
            name=self.name,
            other_params=direct_params,
            dummy_value_param=tool_result_param,
        )


def metadata(**kwargs):
    def _inner(cls):
        setattr(cls, "metadata", kwargs)
        return cls

    return _inner


def version(version: str):
    def inner(cls: type[B]) -> type[B]:
        cls._version = version
        return cls

    return inner


def step(
    output_name: str | None = None,
) -> Callable[[Callable[Concatenate[B, P], Awaitable[T]]], Step[B, P, T]]:
    def step_decorator(fn: Callable[Concatenate[B, P], Awaitable[T]]) -> Step[B, P, T]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"Steps must be async and step {fn.__name__} is not")

        return Step[B, P, T](fn, output_name=output_name)

    return step_decorator


def callback() -> Callable[[Callable[Concatenate[B, P], Awaitable]], Callback[B, P]]:
    def callback_decorator(
        fn: Callable[Concatenate[B, P], Awaitable[None]],
    ) -> Callback[B, P]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"Callbacks must be async and step {fn.__name__} is not")

        return Callback[B, P](fn)

    return callback_decorator


class User(Block):
    @step(output_name="response")
    async def ask(self, message: str, schema: str) -> Any: ...
