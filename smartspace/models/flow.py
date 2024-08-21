from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


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

    def get_node(self, node: str) -> FlowBlock | FlowInput | FlowOutput | FlowConstant:
        result = (
            self.inputs.get(node, None)
            or self.outputs.get(node, None)
            or self.constants.get(node, None)
            or self.blocks.get(node, None)
        )

        if result is None:
            raise KeyError(f"Flow has no nodes with id {node}")

        return result
