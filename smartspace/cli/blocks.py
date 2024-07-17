import importlib

import typer

from smartspace.blocks import Block
from smartspace.models import BlockDefinition, DebugBlockResult, FlowContext
from smartspace.utils import my_issubclass

app = typer.Typer()


@app.command()
def debug():
    import asyncio
    from contextlib import suppress

    from pysignalr.client import SignalRClient
    from pysignalr.messages import (
        CompletionMessage,
        HandshakeMessage,
        InvocationMessage,
        Message,
    )
    from pysignalr.protocol.json import JSONProtocol, MessageEncoder

    import smartspace.cli.auth

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

    async def on_message_override(message: Message):
        if isinstance(message, InvocationMessage) and message.target == "run_block":
            block_definition = BlockDefinition.model_validate(message.arguments[0])
            flow_context = FlowContext.model_validate(message.arguments[1])
            inputs = message.arguments[2]

            print(
                f"run_block received for {block_definition.type.name} with values {inputs}"
            )
            result = DebugBlockResult(
                states={},
                outputs=[],
                callbacks=[],
            )
            message = CompletionMessage(
                message.invocation_id,
                result.model_dump(by_alias=True),
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
        blocks = _load_blocks()
        for block in blocks:
            await client.send(
                "registerblock", [block.interface().model_dump(by_alias=True)]
            )

    client.on_open(on_open)
    client.on_close(on_close)
    client.on_error(on_error)

    with suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(client.run())


def _load_blocks(path: str | None = None) -> list[type[Block]]:
    import glob
    from os.path import basename, dirname, isfile, join

    if path is None:
        path = dirname(__file__)

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

    blocks: list[type[Block]] = []

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
