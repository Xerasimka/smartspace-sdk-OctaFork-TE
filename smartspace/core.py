import abc
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

from smartspace.models.block import (
    BlockInterface,
    BlockPinRef,
    InputPinInterface,
    OutputPinInterface,
    PinType,
    PortInterface,
    PortType,
    StateInterface,
)
from smartspace.utils import _issubclass

B = TypeVar("B", bound="Block")
S = TypeVar("S")
T = TypeVar("T")
R = TypeVar("R")
P = ParamSpec("P")


class CallablePins(NamedTuple):
    inputs: dict[str, InputPinInterface]
    output: OutputPinInterface | None
    generics: dict[str, dict[str, Any]]


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


def _get_function_pins(fn: Callable, port_name: str | None = None) -> CallablePins:
    signature = inspect.signature(fn)
    inputs: dict[str, InputPinInterface] = {}
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

        schema, _generics = _get_json_schema_with_generics(param.annotation)
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
                    port=port_name if port_name else name, pin=name if port_name else ""
                )
                for name in _generics.keys()
            },
        )

    if signature.return_annotation != signature.empty:
        annotations = getattr(signature.return_annotation, "__metadata__", [])
        metadata = first(
            (metadata.data for metadata in annotations if type(metadata) is Metadata),
            {},
        )

        schema, _generics = _get_json_schema_with_generics(signature.return_annotation)
        generics.update(_generics)

        output = OutputPinInterface(
            metadata=metadata,
            json_schema=schema,
            type=PinType.SINGLE,
            generics={
                name: BlockPinRef(
                    port=port_name if port_name else name, pin=name if port_name else ""
                )
                for name in _generics.keys()
            },
        )
    else:
        output = None

    return CallablePins(
        output=output,
        inputs=inputs,
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
) -> tuple[InputPinInterface | None, dict[str, dict[str, Any]]]:
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
        return None, {}

    if state:
        return None, {}

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

    schema, _generics = _get_json_schema_with_generics(field_type)

    return InputPinInterface(
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
    ), _generics


def _get_state_from_metadata(
    field_type: type,
    field_name: str,
    parent: type,
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
    default = getattr(parent, field_name, no_default)

    if default is no_default:
        raise ValueError("State() attributes must have a default value")

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
    schema: dict[str, Any]
    generics: dict[str, dict[str, Any]]


def _get_json_schema_with_generics(t: type) -> JsonSchemaWithGenerics:
    new_t, type_var_map = _map_type_vars(t)

    generics = {
        name.__name__: generic_schema
        for name, (_, generic_schema) in type_var_map.items()
    }

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
    block_type: type,
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

            schema, _generics = _get_json_schema_with_generics(args[0])
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

        _inputs, _output, _generics = _get_function_pins(
            tool_type.run, port_name=port_name
        )

        inputs.update(_inputs)
        if _output:
            outputs["return"] = _output

        for generic_name, generic_schema in _generics.items():
            inputs[generic_name] = InputPinInterface(
                metadata={"generic": True},
                sticky=True,
                json_schema=TypeAdapter(dict[str, Any]).json_schema(),
                generics={},
                type=PinType.SINGLE,
                required=False,
                default=generic_schema,
            )

    input_pin, _generics = _get_input_pin_from_metadata(
        base_type,
        port_name=port_name,
        field_name=port_name,
        parent=block_type,
        pin_type=PinType.SINGLE,
    )
    if isinstance(input_pin, InputPinInterface):
        inputs[""] = input_pin
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

            schema, _generics = _get_json_schema_with_generics(args[0])
            for generic_name, generic_schema in _generics.items():
                inputs[generic_name] = InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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

                    schema, _generics = _get_json_schema_with_generics(args[0])
                    for generic_name, generic_schema in _generics.items():
                        inputs[generic_name] = InputPinInterface(
                            metadata={"generic": True},
                            sticky=True,
                            json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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
                    input_pin, _generics = _get_input_pin_from_metadata(
                        item_type,
                        port_name=port_name,
                        field_name=field_name,
                        parent=cls,
                        pin_type=PinType.DICTIONARY,
                    )
                    if isinstance(input_pin, InputPinInterface):
                        inputs[field_name] = input_pin
                        for generic_name, generic_schema in _generics.items():
                            inputs[generic_name] = InputPinInterface(
                                metadata={"generic": True},
                                sticky=True,
                                json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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

                    schema, _generics = _get_json_schema_with_generics(args[0])
                    for generic_name, generic_schema in _generics.items():
                        inputs[generic_name] = InputPinInterface(
                            metadata={"generic": True},
                            sticky=True,
                            json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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
                    input_pin, _generics = _get_input_pin_from_metadata(
                        item_type,
                        port_name=port_name,
                        field_name=field_name,
                        parent=cls,
                        pin_type=PinType.LIST,
                    )
                    if isinstance(input_pin, InputPinInterface):
                        inputs[field_name] = input_pin
                        for generic_name, generic_schema in _generics.items():
                            inputs[generic_name] = InputPinInterface(
                                metadata={"generic": True},
                                sticky=True,
                                json_schema=TypeAdapter(dict[str, Any]).json_schema(),
                                generics={},
                                type=PinType.SINGLE,
                                required=False,
                                default=generic_schema,
                            )

        input_pin, _generics = _get_input_pin_from_metadata(
            field_annotation,
            port_name=port_name,
            field_name=field_name,
            parent=cls,
            pin_type=PinType.SINGLE,
        )
        if isinstance(input_pin, InputPinInterface):
            inputs[field_name] = input_pin
            for generic_name, generic_schema in _generics.items():
                inputs[generic_name] = InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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
                parent=block_type,
            )
            if s:
                state[port_name] = s

    for generic_name, generic_schema in generics.items():
        ports[generic_name] = PortInterface(
            metadata={},
            inputs={
                "": InputPinInterface(
                    metadata={"generic": True},
                    sticky=True,
                    json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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


class Output(Generic[T]):
    def send(self, value: T): ...


class BlockError(BaseModel):
    message: str
    data: Any = None


class MetaBlock(type):
    _version: str | None = None

    @property
    def version(cls):
        version_str = cls._version or ".".join(cls.__name__.split("_")[1:]) or "1.0.0"
        version = semantic_version.Version.coerce(version_str)
        return str(version)

    @property
    def name(cls):
        return cls.__name__.split("_")[0]


class Block(metaclass=MetaBlock):
    metadata: ClassVar[dict] = {}
    error: Annotated[Output[BlockError], Metadata(hidden=True)]

    @classmethod
    def interface(cls) -> BlockInterface:
        ports, state = _get_ports_and_state(cls)

        for attribute_name in dir(cls):
            attribute = getattr(cls, attribute_name)

            if type(attribute) is Step or type(attribute) is Callback:
                i, generics = attribute.interface()
                ports[attribute_name] = i

                for generic_name, generic_schema in generics.items():
                    ports[generic_name] = PortInterface(
                        metadata={},
                        inputs={
                            "": InputPinInterface(
                                metadata={"generic": True},
                                sticky=True,
                                json_schema=TypeAdapter(dict[str, Any]).json_schema(),
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

                for port in ports.values():
                    for pin in list(port.inputs.values()) + list(port.outputs.values()):
                        for g, pin_ref in pin.generics.items():
                            if (
                                g == generic_name
                                and pin_ref.port == generic_name
                                and pin_ref.pin == ""
                            ):
                                pin.generics[g] = BlockPinRef(port=port_name, pin="")

        return BlockInterface(
            metadata=cls.metadata,
            ports=ports,
            state=state,
        )


class Tool(abc.ABC, Generic[P, T]):
    metadata: ClassVar[dict] = {}

    @abc.abstractmethod
    def run(self, *args: P.args, **kwargs: P.kwargs) -> T: ...


class Step(Generic[B, P, T]):
    """
    A step is a process in a block. It is quite generic and can have one or more inputs and can push values to 0 or more outputs while running
    """

    def __init__(
        self,
        fn: Callable[Concatenate[B, P], Awaitable[T]],
        output_name: str | None = None,
    ):
        self.name = fn.__name__
        self._fn = fn
        self._output_name = output_name or ""
        self.metadata: dict = {}

    def interface(self) -> tuple[PortInterface, dict[str, dict[str, Any]]]:
        (inputs, output, generics) = _get_function_pins(self._fn)

        return PortInterface(
            metadata=self.metadata,
            inputs=inputs,
            outputs={self._output_name: output} if output else {},
            is_function=True,
            type=PortType.SINGLE,
        ), generics

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        s = cast(B, self)
        return await self._fn(s, *args, **kwargs)


class Callback(Generic[B, P]):
    def __init__(
        self,
        fn: Callable[Concatenate[B, P], Awaitable],
    ):
        self.name = fn.__name__
        self._fn = fn
        self.metadata: dict = {}

    def interface(self) -> tuple[PortInterface, dict[str, dict[str, Any]]]:
        (inputs, _, generics) = _get_function_pins(self._fn)

        if "hidden" not in self.metadata:
            self.metadata["hidden"] = True

        return PortInterface(
            metadata=self.metadata,
            inputs=inputs,
            outputs={},
            is_function=True,
            type=PortType.SINGLE,
        ), generics


def metadata(**kwargs):
    def _inner(cls):
        setattr(cls, "metadata", kwargs)
        return cls

    return _inner


def version(version: str):
    def inner(cls: MetaBlock):
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
        fn: Callable[Concatenate[B, P], Awaitable[T]],
    ) -> Callback[B, P]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"Callbacks must be async and step {fn.__name__} is not")

        return Callback[B, P](fn)

    return callback_decorator


class User(Block):
    @step(output_name="response")
    async def ask(self, message: str, schema: str) -> Any: ...
