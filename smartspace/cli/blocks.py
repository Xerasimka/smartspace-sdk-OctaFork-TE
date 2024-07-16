import typer

from smartspace.blocks import Block
from smartspace.utils import my_issubclass

app = typer.Typer()


@app.command()
def debug():
    import asyncio
    from contextlib import suppress
    from typing import Any, Dict, List

    from pysignalr.client import SignalRClient
    from pysignalr.messages import CompletionMessage

    import smartspace.cli.auth

    async def on_open() -> None:
        print("Connected to the server")

    async def on_close() -> None:
        print("Disconnected from the server")

    async def on_message(message: List[Dict[str, Any]]) -> None:
        print(f"Received message: {message}")

    async def on_error(message: CompletionMessage) -> None:
        print(f"Received error: {message.error}")

    def token_factory() -> str:
        token = smartspace.cli.auth.get_token()
        return token

    async def main() -> None:
        client = SignalRClient(
            url="http://host.docker.internal:5056/debug",
            access_token_factory=token_factory,
            headers={"Authorization": f"Bearer {smartspace.cli.auth.get_token()}"},
        )

        client.on_open(on_open)
        client.on_close(on_close)
        client.on_error(on_error)
        # client.on("operations", on_message)

        async def register_blocks():
            blocks = _load_blocks()
            for block in blocks:
                await client.send("registerblock", [block.interface().model_dump()])

        await asyncio.gather(
            client.run(),
            register_blocks(),
        )

    with suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main())


def _load_blocks() -> list[type[Block]]:
    import glob
    from os.path import basename, dirname, isfile, join

    modules = glob.glob(join(dirname(__file__), "*.py"))
    files = [
        basename(f)[:-3] for f in modules if isfile(f) and not f.endswith("__init__.py")
    ]

    blocks = []

    for file in files:
        module = __import__(file)
        for name in dir(module):
            item = getattr(module, name)
            if my_issubclass(item, Block) and item != Block:
                if not any([b == Block for b in blocks]):
                    blocks.append(item)

    return blocks
