from typing import Any

from jsonpath_ng import JSONPath
from jsonpath_ng.ext import parse

from smartspace.core import Block, Config, Output, step


class If(Block):
    condition: Config[str]
    Then: Output[Any]
    Else: Output[Any]

    @step()
    async def filter(self, item: Any):
        expr: JSONPath = parse(f"$[?(@.{self.condition})]")

        if expr.find([{"item": item}]):
            self.Then.emit(item)
        else:
            self.Else.emit(item)
