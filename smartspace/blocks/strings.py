from smartspace.core import (
    Block,
    metadata,
    step,
)
from smartspace.enums import BlockCategory


@metadata(category=BlockCategory.FUNCTION)
class Concat(Block):
    @step(output_name="result")
    async def concat(self, a: str, b: str) -> str:
        return a + b
