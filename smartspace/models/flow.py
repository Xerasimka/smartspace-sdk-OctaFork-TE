from typing import Any

from pydantic import BaseModel, ConfigDict


class PortRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    block: str
    port: str


class PinRef(PortRef):
    model_config = ConfigDict(populate_by_name=True)

    pin: str


class Connection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: PinRef
    target: PinRef


class FlowConstant(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: Any


class FlowBlock(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: str


class FlowDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connections: list[Connection]
    blocks: dict[str, FlowBlock | FlowConstant]
