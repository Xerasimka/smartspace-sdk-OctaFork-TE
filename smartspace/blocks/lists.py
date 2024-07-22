from typing import Annotated, Any

from smartspace.core import (
    Block,
    InputConfig,
    Output,
    State,
    Tool,
    callback,
    metadata,
    step,
)
from smartspace.enums import BlockCategory


@metadata(category=BlockCategory.FUNCTION)
class Map(Block):
    class Operation(Tool):
        def run(self, item: Any) -> Any: ...

    run: Operation

    results: Output[list[Any]]

    count: Annotated[
        int,
        State(
            step_id="map",
            input_ids=["items"],
            default_value=0,
        ),
    ]

    results_state: Annotated[
        list[Any],
        State(
            step_id="map",
            input_ids=["items"],
            default_value=[],
        ),
    ]

    @step()
    async def map(self, items: list[Any]):
        if len(items) == 0:
            self.results.emit([])
            return

        self.results_state = [None] * len(items)
        self.count = len(items)
        for i, item in enumerate(items):
            self.run.call(item).then(lambda result: self.collect(result, i))

    @callback()
    async def collect(
        self,
        result: Any,
        index: int,
    ):
        self.results_state[index] = result
        self.count -= 1

        if self.count == 0:
            self.results.emit(self.results_state)


@metadata(category=BlockCategory.FUNCTION)
class Collect(Block):
    items: Output[list[Any]]

    items_state: Annotated[
        list[Any],
        State(
            step_id="collect",
            input_ids=["count"],
            default_value=[],
        ),
    ]

    @step()
    async def collect(
        self,
        item: str,
        count: Annotated[int, InputConfig(sticky=True)],
    ):
        self.items_state.append(item)

        if len(self.items_state) == count:
            self.items.emit(self.items_state)
            self.items_state = []


@metadata(category=BlockCategory.FUNCTION)
class Count(Block):
    @step(output_name="output")
    async def count(self, items: list[Any]) -> int:
        return len(items)


@metadata(category=BlockCategory.FUNCTION)
class ForEach(Block):
    item: Output[Any]

    @step()
    async def foreach(self, items: list[Any]):
        for item in items:
            self.item.emit(item)
