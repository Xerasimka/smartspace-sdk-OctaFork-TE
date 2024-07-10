import abc
from typing import Any, cast

from more_itertools import first

from smartspace.blocks import (
    BlockDefinition,
    ConfigDefinition,
    InputDefinition,
    OutputDefinition,
    StepDefinition,
    ToolDefinition,
    ToolInputDefinition,
    ToolOutputDefinition,
)
from smartspace.errors import BlockValidationException
from smartspace.models import (
    BlockInterface,
    BlockOutputReference,
    BlockType,
    ChildFlowInputReference,
    FlowDefinition,
    FlowOutputReference,
    StepInputReference,
    ToolInputReference,
    ToolOutputReference,
    ValueSourceType,
    ValueTargetType,
)


class IBlockProvider(abc.ABC):
    @abc.abstractmethod
    def get_block_interface(self, block_type: BlockType) -> BlockInterface: ...


def compile_blocks(
    flow_definition: FlowDefinition,
    block_provider: IBlockProvider,
    validate: bool = True,
):
    blocks: dict[str, BlockDefinition] = {}

    for block in flow_definition.blocks:
        block_interface = block_provider.get_block_interface(block.type)

        blocks[block.id] = BlockDefinition(
            id=block.id,
            type=block.type,
            configs=_get_block_configs(
                block_interface=block_interface,
                config_values=block.config,
                validate=validate,
            ),
            outputs=_get_block_outputs(
                block_interface,
                validate=validate,
            ),
            steps=_get_block_steps(
                block_interface,
                validate=validate,
            ),
            tools=_get_block_tools(
                block_interface,
                block.tools,
                validate=validate,
            ),
        )
        for step in blocks[block.id].steps.values():
            if step.output:
                blocks[block.id].outputs[step.output.id] = step.output

        for tool in blocks[block.id].tools.values():
            for tool_input_name, tool_input_definition in tool.inputs.items():
                blocks[block.id].outputs[f"{tool.id}.{tool_input_name}"] = (
                    tool_input_definition
                )

    for connection in flow_definition.connections:
        if connection.source.type == ValueSourceType.BLOCK_OUTPUT:
            block_output = cast(BlockOutputReference, connection.source.block_output)
            output = blocks[block_output.block_id].outputs[block_output.output_id]
        elif connection.source.type == ValueSourceType.TOOL_INPUT:
            tool_input = cast(ToolInputReference, connection.source.tool_input)
            output = (
                blocks[tool_input.block_id]
                .tools[tool_input.tool_id]
                .inputs[tool_input.input_id]
            )
        else:
            continue

        if connection.target.type == ValueTargetType.STEP_INPUT:
            step_input = cast(StepInputReference, connection.target.step_input)
            target_schema = (
                blocks[step_input.block_id]
                .steps[step_input.step_id]
                .inputs[step_input.input_id]
                .json_schema
            )

        elif connection.target.type == ValueTargetType.FLOW_OUTPUT:
            flow_output = cast(FlowOutputReference, connection.target.flow_output)
            flow_output_definition = None
            for _flow_output_definition in flow_definition.outputs:
                if _flow_output_definition.name == flow_output.flow_output_id:
                    flow_output_definition = _flow_output_definition
            if not flow_output_definition:
                raise Exception(
                    f"Could not find flow output '{flow_output.flow_output_id}'"
                )

            target_schema = flow_output_definition.json_schema

        elif connection.target.type == ValueTargetType.TOOL_OUTPUT:
            tool_output = cast(ToolOutputReference, connection.target.tool_output)
            tool = blocks[tool_output.block_id].tools[tool_output.tool_id]
            if tool.output is None:
                raise Exception(f"Tool {tool.id} has no output")

            target_schema = tool.output.json_schema

        elif connection.target.type == ValueTargetType.CHILD_FLOW_INPUT:
            child_flow_input = cast(
                ChildFlowInputReference, connection.target.child_flow_input
            )
            child_flow_input_defintion = None
            for input in flow_definition.children[child_flow_input.flow_id].inputs:
                if input.name == child_flow_input.flow_input_id:
                    child_flow_input_defintion = input
            if not child_flow_input_defintion:
                raise Exception(
                    f"Could not find flow input {child_flow_input.flow_input_id}"
                )

            target_schema = child_flow_input_defintion.json_schema

        else:
            raise Exception(f"Unexpected target type {connection.target}")

        if "allOf" in output.json_schema:
            output.json_schema["allOf"].append(target_schema)
        elif len(output.json_schema) == 0:
            output.json_schema = target_schema
        else:
            output.json_schema = {"allOf": [output.json_schema, target_schema]}

    return blocks


def _get_block_configs(
    block_interface: BlockInterface,
    config_values: dict[str, Any],
    validate: bool,
) -> dict[str, ConfigDefinition]:
    configs: dict[str, ConfigDefinition] = {}

    for config_interface in block_interface.configs:
        configs[config_interface.name] = ConfigDefinition(
            id=config_interface.name,
            value=config_values[config_interface.name],
        )

    return configs


def _get_block_outputs(
    block_interface: BlockInterface,
    validate: bool,
) -> dict[str, OutputDefinition]:
    outputs: dict[str, OutputDefinition] = {}

    for output in block_interface.outputs:
        outputs[output.name] = OutputDefinition(
            id=output.name,
            json_schema=output.json_schema,
        )

    return outputs


def _get_block_steps(
    block_interface: BlockInterface,
    validate: bool,
) -> dict[str, StepDefinition]:
    steps: dict[str, StepDefinition] = {}

    for step in block_interface.steps:
        inputs = {
            step_input.name: InputDefinition(
                id=step_input.name,
                sticky=step_input.sticky,
                json_schema=step_input.json_schema,
            )
            for step_input in step.inputs
        }

        output = (
            None
            if not step.output_ref
            else first(
                [
                    OutputDefinition(
                        id=output.name,
                        json_schema=output.json_schema,
                    )
                    for output in block_interface.outputs
                    if output.name == step.output_ref
                ],
                None,
            )
        )

        if validate and step.output_ref and not output:
            raise BlockValidationException(
                f"Block {block_interface.name} step {step.name} has output ref {step.output_ref} but could not find a matching output in the block"
            )

        steps[step.name] = StepDefinition(
            id=step.name,
            inputs=inputs,
            output=output,
        )

    return steps


def _get_block_tools(
    block_interface: BlockInterface,
    tool_configs: dict[str, Any],
    validate: bool,
) -> dict[str, ToolDefinition]:
    tools: dict[str, ToolDefinition] = {}

    for tool in block_interface.tools:
        if tool.multiple:
            configs = cast(dict[str, Any], tool_configs.get(tool.name, {}))

            for name, tool_config in configs.items():
                tool_id = f"{tool.name}.{name}"
                tools[tool_id] = ToolDefinition(
                    id=tool_id,
                    inputs={
                        tool_input.name: ToolInputDefinition(
                            id=tool_input.name,
                            json_schema=tool_input.json_schema,
                            tool_id=tool_id,
                        )
                        for tool_input in tool.inputs
                    },
                    output=ToolOutputDefinition(
                        id=tool.output.name,
                        json_schema=tool.output.json_schema,
                        tool_id=tool_id,
                    )
                    if tool.output
                    else None,
                    configs=tool_config,
                )
        else:
            tools[tool.name] = ToolDefinition(
                id=tool.name,
                inputs={
                    tool_input.name: ToolInputDefinition(
                        id=tool_input.name,
                        json_schema=tool_input.json_schema,
                        tool_id=tool.name,
                    )
                    for tool_input in tool.inputs
                },
                output=ToolOutputDefinition(
                    id=tool.output.name,
                    json_schema=tool.output.json_schema,
                    tool_id=tool.name,
                )
                if tool.output
                else None,
                configs=tool_configs[tool.name] if tool.name in tool_configs else {},
            )

    return tools
