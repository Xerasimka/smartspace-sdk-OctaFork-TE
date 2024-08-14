import json
from typing import Annotated, Any, List, Union

from jsonpath_ng import JSONPath
from jsonpath_ng.ext import parse
from pydantic import BaseModel

from smartspace.core import (
    Block,
    Config,
    Metadata,
    metadata,
    step,
)
from smartspace.enums import BlockCategory


@metadata(
    description="This block takes a JSON string or a list of JSON strings and parses them",
    category=BlockCategory.FUNCTION,
)
class ParseJson(Block):
    @step(output_name="json")
    async def parse_json(
        self,
        json_string: Annotated[
            Union[str, List[str]],
            Metadata(description="JSON string or list of JSON strings"),
        ],
    ) -> Any:
        def fullParse(jsonString: str):
            try:
                return json.loads(jsonString)
            except json.JSONDecodeError:
                try:
                    lines = jsonString.split("\n")
                    json_objects = [json.loads(line) for line in lines]
                    return json_objects
                except json.JSONDecodeError as e:
                    return {"error": str(e)}

        if isinstance(json_string, list):
            results: List[Any] = [fullParse(item) for item in json_string]
            return results[0]
        else:
            result = fullParse(json_string)
            return result


@metadata(
    category=BlockCategory.FUNCTION,
    description="Uses JSONPath to extract data from a JSON object or list",
)
class GetJsonField(Block):
    json_path: Config[str]

    @step(output_name="field")
    async def get(self, json_object: Any) -> Any:
        if isinstance(json_object, BaseModel):
            json_object = json.loads(json_object.model_dump_json())
        elif isinstance(json_object, list) and all(
            isinstance(item, BaseModel) for item in json_object
        ):
            json_object = [json.loads(item.model_dump_json()) for item in json_object]

        jsonpath_expr: JSONPath = parse(self.json_path)
        results: List[Any] = [match.value for match in jsonpath_expr.find(json_object)]
        return results


@metadata(category=BlockCategory.FUNCTION)
class MergeLists(Block):
    key: Config[str]

    @step(output_name="result")
    async def merge_lists(
        self,
        a: list[dict[str, Any]],
        b: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        dict1 = {item[self.key]: item for item in a}
        dict2 = {item[self.key]: item for item in b}

        merged_dict = {}
        for code in dict1.keys() | dict2.keys():
            if code in dict1 and code in dict2:
                merged_dict[code] = {**dict1[code], **dict2[code]}
            elif code in dict1:
                merged_dict[code] = dict1[code]
            elif code in dict2:
                merged_dict[code] = dict2[code]

        final_result = list(merged_dict.values())

        return final_result
