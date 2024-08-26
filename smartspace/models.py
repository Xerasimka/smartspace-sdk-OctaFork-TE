import enum
from datetime import datetime
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


class BlockPinRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    port: str
    pin: str


class InputPinInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    sticky: bool
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]
    generics: dict[
        str, BlockPinRef
    ]  # Name of the generic, like OutputT, and then a reference to the input on this block that defines the schema
    type: PinType
    required: bool
    default: Any
    channel: bool


class OutputPinInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]
    generics: dict[
        str, BlockPinRef
    ]  # Name of the generic, like OutputT, and then a reference to the input on this block that defines the schema
    type: PinType
    channel: bool


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
    scope: list[BlockPinRef]
    default: Any


class BlockInterface(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    metadata: dict = {}
    ports: dict[str, PortInterface]
    state: dict[str, StateInterface]


class BlockContext(BaseModel):
    workspace: "SmartSpaceWorkspace | None"
    message_history: "list[ThreadMessage] | None"


class InputValue(BaseModel):
    target: BlockPinRef
    value: Any


class OutputValue(BaseModel):
    source: BlockPinRef
    value: Any
    index: int


class StateValue(BaseModel):
    state: str
    value: Any


class PinRedirect(BaseModel):
    source: BlockPinRef
    target: BlockPinRef


class ThreadMessageResponseSource(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    index: int
    uri: str


class ThreadMessageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    content: str
    sources: list[ThreadMessageResponseSource] | None = None


class File(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    uri: str


class ContentItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    image: File | None = None
    text: str | None = None


class ThreadMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    content: str | None = None
    content_list: Annotated[list[ContentItem] | None, Field(alias="contentList")] = None
    response: ThreadMessageResponse
    created_at: Annotated[datetime, Field(..., alias="createdAt")]
    created_by: Annotated[str, Field(..., alias="createdBy")]


class SmartSpaceDataSpace(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str


class SmartSpaceWorkspace(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    data_spaces: Annotated[list[SmartSpaceDataSpace], Field(alias="dataSpaces")] = []
    flow_definition: Annotated[
        "FlowDefinition | None", Field(alias="flowDefinition")
    ] = None

    @property
    def dataspace_ids(self) -> list[str]:
        return [dataspace.id for dataspace in self.data_spaces]


class FlowPinRef(BaseModel):
    """
    When referencing block pins, block, port, and pin must be set
    When referencing a constant, only block must be set
    """

    model_config = ConfigDict(populate_by_name=True)

    node: str
    port: str | None = None
    pin: str | None = None


class Connection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: FlowPinRef
    target: FlowPinRef


class FlowBlock(BaseModel):
    name: str
    version: str
    dynamic_outputs: Annotated[list[BlockPinRef], Field(alias="dynamicOutputs")]
    dynamic_inputs: Annotated[list[BlockPinRef], Field(alias="dynamicInputs")]


class FlowConstant(BaseModel):
    value: Any


class FlowInput(BaseModel):
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]


class FlowOutput(BaseModel):
    json_schema: Annotated[dict[str, Any], Field(alias="schema")]


class FlowDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    inputs: dict[str, FlowInput]
    outputs: dict[str, FlowOutput]
    constants: dict[str, FlowConstant]
    blocks: dict[str, FlowBlock]

    connections: list[Connection]

    def get_node(
        self, node: str
    ) -> FlowBlock | FlowInput | FlowOutput | FlowConstant | None:
        return (
            self.inputs.get(node, None)
            or self.outputs.get(node, None)
            or self.constants.get(node, None)
            or self.blocks.get(node, None)
        )


class BlockRunData(BaseModel):
    name: str
    version: str
    function: str
    context: BlockContext | None
    state: list[StateValue] | None
    inputs: list[InputValue] | None
    dynamic_outputs: list[BlockPinRef] | None
    dynamic_inputs: list[BlockPinRef] | None


class BlockRunMessage(BaseModel):
    outputs: list[OutputValue] = []
    inputs: list[InputValue] = []
    redirects: list[PinRedirect] = []
    states: list[StateValue] = []
