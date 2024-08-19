import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class PinRef(BaseModel):
    """
    When referencing block pins, block, port, and pin must be set
    When referencing a constant, only block must be set
    """

    model_config = ConfigDict(populate_by_name=True)

    block: str
    port: str | None = None
    pin: str | None = None


class Connection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: PinRef
    target: PinRef


class FlowBlock(BaseModel):
    name: str
    version: str

    def to_component(self) -> "FlowComponent":
        return FlowComponent(
            type=FlowComponentType.BLOCK,
            name=self.name,
            version=self.version,
        )


class FlowConstant(BaseModel):
    value: Any

    def to_component(self) -> "FlowComponent":
        return FlowComponent(
            type=FlowComponentType.CONSTANT,
            value=self.value,
        )


class FlowComponentType(enum.Enum):
    BLOCK = "Block"
    CONSTANT = "Constant"


class FlowComponent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: FlowComponentType
    name: str | None = None
    version: str | None = None
    value: Any = None

    @model_validator(mode="after")
    def check_values_correct_given_type(self):
        if self.type == FlowComponentType.BLOCK:
            if self.value is not None:
                raise ValueError("'value' must be 'None' when type is Block")

            if not self.name:
                raise ValueError("'name' must not be 'None' when type is Block")

            if not self.version:
                raise ValueError("'version' must not be 'None' when type is Block")

        elif self.type == FlowComponentType.CONSTANT:
            if self.name:
                raise ValueError("'name' must be 'None' when type is Constant")

            if self.version:
                raise ValueError("'version' must be 'None' when type is Constant")

        return self

    def as_block(self) -> FlowBlock:
        if self.type != FlowComponentType.BLOCK:
            raise ValueError(f"Can not cast to FlowBlock when 'type' is '{self.type}'")

        if not self.name:
            raise ValueError("Can not cast to FlowBlock when 'name' is 'None'")

        if not self.version:
            raise ValueError("Can not cast to FlowBlock when 'version' is 'None'")

        return FlowBlock(
            name=self.name,
            version=self.version,
        )

    def as_constant(self) -> FlowConstant:
        if self.type != FlowComponentType.CONSTANT:
            raise ValueError(f"Can not cast to FlowBlock when 'type' is '{self.type}'")

        return FlowConstant(
            value=self.value,
        )


class FlowDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connections: list[Connection]
    blocks: dict[str, FlowComponent]
