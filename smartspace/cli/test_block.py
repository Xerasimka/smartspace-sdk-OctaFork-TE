from typing import Annotated, Any

from smartspace.blocks import (
    Block,
    Config,
    Output,
    State,
    Tool,
    callback,
    step,
)


class MapTest(Block):
    class Operation(Tool):
        def run(self, item: Any) -> Any: ...

    c: Config[str]

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
