import enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class PinType(enum.Enum):
    SINGLE = "Single"
    LIST = "List"
    DICTIONARY = "Dictionary"


class PortType(enum.Enum):
    SINGLE = "Single"
    LIST = "List"
    DICTIONARY = "Dictionary"


class PinRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    port: str
    pin: str


class InputPinInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    sticky: bool
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]
    generics: dict[
        str, PinRef
    ]  # Name of the generic, like OutputT, and then a reference to the input on this block that defines the schema
    type: PinType
    # if it is required, then it can't have a default
    # if it is not required, then it will default the default to None when not explicitly set
    required: bool
    default: Any


class OutputPinInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]
    generics: dict[str, dict[str, Any]]
    type: PinType


class PortInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    inputs: dict[str, InputPinInterface]
    outputs: dict[str, OutputPinInterface]
    type: PortType
    is_function: Annotated[bool, Field(alias="isFunction")]


class StateInterface(BaseModel):
    """
    scope_pins is a list of pins that set the scope of the state.
    When any function runs, state is set on the component.
    And for each run that the scope_pins have the same values, that state will be reused
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    scope_pins: Annotated[list[str], Field(alias="scopePins")]
    default: Any


class BlockInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    ports: dict[str, PortInterface]
    state: dict[str, StateInterface]
