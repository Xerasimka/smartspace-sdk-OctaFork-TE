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


class InputPinInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    sticky: bool
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]
    generics: list[str]
    type: PinType
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


class FunctionInterface(BaseModel):
    """
    ports is the list of ports that need data for this function to run
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    ports: list[str]


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

    name: str
    version: str
    metadata: dict = {}
    ports: dict[str, PortInterface]
    functions: dict[str, FunctionInterface]
    state: dict[str, StateInterface]
