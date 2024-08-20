from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from smartspace.models.flow import FlowDefinition


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
    flow_definition: Annotated[FlowDefinition | None, Field(alias="flowDefinition")] = (
        None
    )

    @property
    def dataspace_ids(self) -> list[str]:
        return [dataspace.id for dataspace in self.data_spaces]
