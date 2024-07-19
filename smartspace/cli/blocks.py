import importlib
import uuid
from typing import Any

import typer
from more_itertools import first

from smartspace.blocks import Block
from smartspace.models import (
    BlockInterface,
    CallbackCall,
    DebugBlockRequest,
    DebugBlockResult,
    FlowValue,
    ValueSourceRef,
)
from smartspace.utils import my_issubclass

app = typer.Typer()


@app.command()
def debug(path: str = ""):
    import asyncio
    from contextlib import suppress
    from os.path import dirname

    from pysignalr.client import SignalRClient
    from pysignalr.messages import (
        CompletionMessage,
        HandshakeMessage,
        InvocationMessage,
        Message,
    )
    from pysignalr.protocol.json import JSONProtocol, MessageEncoder
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    import smartspace.cli.auth

    root_path = path if path != "" else dirname(__file__)

    message_encoder = MessageEncoder()

    class MyJSONProtocol(JSONProtocol):
        def encode(self, message: Message | HandshakeMessage) -> str:
            if isinstance(message, CompletionMessage):
                data = message.dump()
                if "error" in data and data["error"] is None:
                    del data["error"]
                return message_encoder.encode(data)
            else:
                return JSONProtocol.encode(self, message)

    client = SignalRClient(
        url="http://host.docker.internal:5056/debug",
        access_token_factory=smartspace.cli.auth.get_token,
        headers={"Authorization": f"Bearer {smartspace.cli.auth.get_token()}"},
        protocol=MyJSONProtocol(),
    )

    blocks: list[tuple[BlockInterface, type[Block]]] = []

    async def on_message_override(message: Message):
        if isinstance(message, InvocationMessage) and message.target == "run_block":
            request = DebugBlockRequest.model_validate(message.arguments[0])

            print(
                f"run_block received for {request.block_definition.type.name} with values {request.inputs}"
            )

            block = first(
                [
                    block_type
                    for block_interface, block_type in blocks
                    if block_interface.name == request.block_definition.type.name
                    and block_interface.version == request.block_definition.type.version
                ]
            )

            outputs: list[FlowValue] = []
            callbacks: list[DebugBlockResult.Callback] = []

            def register_tool_callback(
                tool_id: str,
                tool_call_values: list[FlowValue],
                callback: CallbackCall,
            ):
                callbacks.append(
                    DebugBlockResult.Callback(
                        tool_id=tool_id,
                        tool_call_values=tool_call_values,
                        callback=callback,
                    )
                )

            def emit_output_value(value: Any, source: ValueSourceRef) -> FlowValue:
                flow_value = FlowValue(
                    id=uuid.uuid4(),
                    value_source=source,
                    parent_ids=[parent.id for parent in request.inputs.values()],
                    value=value,
                )
                outputs.append(flow_value)

                return flow_value

            block_instance = block(
                register_tool_callback=register_tool_callback,
                emit_output_value=emit_output_value,
                definition=request.block_definition,
                flow_context=request.flow_context,
            )

            step_instance = block_instance._steps_instances[request.step_id]

            step_kwargs: dict[str, Any] = {
                input_name: value.value for input_name, value in request.inputs.items()
            }

            await step_instance.run(**step_kwargs)

            updated_state = {
                state_name: getattr(block_instance, state_name, None)
                for state_name in request.block_definition.states.keys()
            }

            result = DebugBlockResult(
                states=updated_state,
                outputs=outputs,
                callbacks=callbacks,
            )

            message = CompletionMessage(
                message.invocation_id,
                result.model_dump(by_alias=True, mode="json"),
                headers=client._headers,
            )
            await client._transport.send(message)
        else:
            await SignalRClient._on_message(client, message)

    client._on_message = on_message_override
    client._transport._callback = on_message_override

    async def on_close() -> None:
        print("Disconnected from the server")

    async def on_error(message: CompletionMessage) -> None:
        print(f"Received error: {message.error}")

    async def on_open() -> None:
        print("Connected to the server")
        await register_blocks(root_path)

    async def register_blocks(path: str):
        print(f"Looking for blocks in {path}")
        updated_blocks = [(b.interface(), b) for b in _load_blocks(path)]
        old_blocks = blocks.copy()

        blocks.clear()
        blocks.extend(
            [
                b
                for b in old_blocks
                if not any(
                    [
                        updated_block[0].name == b[0].name
                        and updated_block[0].version == b[0].version
                        for updated_block in updated_blocks
                    ]
                )
            ]
            + updated_blocks
        )

        for block_interface, _ in updated_blocks:
            print(f"Registering block {block_interface.name}")
            data = block_interface.model_dump(by_alias=True)
            await client.send("registerblock", [data])

    client.on_open(on_open)
    client.on_close(on_close)
    client.on_error(on_error)

    class _EventHandler(FileSystemEventHandler):
        def __init__(self, loop: asyncio.AbstractEventLoop, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.loop = loop

        def _on_any_event(self, event: FileSystemEvent):
            if not event.is_directory:
                print(event)  # Your code here
                asyncio.run_coroutine_threadsafe(
                    register_blocks(event.src_path), self.loop
                )

        def on_created(self, event: FileSystemEvent):
            self._on_any_event(event)

        def on_deleted(self, event: FileSystemEvent):
            self._on_any_event(event)

        def on_modified(self, event: FileSystemEvent):
            self._on_any_event(event)

        def on_moved(self, event: FileSystemEvent):
            self._on_any_event(event)

    async def main():
        loop = asyncio.get_event_loop()
        handler = _EventHandler(loop)
        observer = Observer()
        observer.schedule(handler, root_path, recursive=True)
        observer.start()
        print("Observer started")

        await client.run()

    with suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main())


def _load_blocks(path: str) -> list[type[Block]]:
    import glob
    import os
    import sys
    from os.path import basename, isfile, join

    blocks: list[type[Block]] = []

    if isfile(path):
        if path.endswith(".py"):
            module_name = (
                path.removeprefix(os.getcwd()).replace("/", ".")[:-3].strip(".")
            )
            if module_name in sys.modules:
                del sys.modules[module_name]

            module = importlib.import_module(module_name)
            for name in dir(module):
                if not name.startswith("_"):
                    item = getattr(module, name)
                    if (
                        my_issubclass(item, Block)
                        and item != Block
                        and not any([b == item for b in blocks])
                    ):
                        blocks.append(item)
        return blocks

    def get_python_files(nested_path: str):
        files_and_folders = glob.glob(join(path, nested_path, "*"))
        files = [
            join(nested_path, basename(f))
            for f in files_and_folders
            if isfile(f) and f.endswith(".py") and not f.endswith("__init__.py")
        ]
        folders = [
            join(nested_path, basename(f))
            for f in files_and_folders
            if not isfile(f)
            and not f.startswith(".")
            and not basename(f).startswith("_")
        ]

        for folder in folders:
            files.extend(get_python_files(folder))

        return files

    python_files = get_python_files("")

    for file in python_files:
        module_name = file.replace("/", ".")[:-3]
        module = importlib.import_module(module_name)
        for name in dir(module):
            if not name.startswith("_"):
                item = getattr(module, name)
                if (
                    my_issubclass(item, Block)
                    and item != Block
                    and not any([b == item for b in blocks])
                ):
                    blocks.append(item)

    return blocks


if __name__ == "__main__":
    debug()
