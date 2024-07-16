from typing import Annotated

import typer

from smartspace.cli import blocks

# client_id = "30101e46-ba13-4957-b610-233f56ebb94f"
# local_config.save_config(
#     {
#         "client_id": "e3f39d90-9235-435e-ba49-681727352613",
#         "tenant_id": "fd656490-ea47-45d1-a9a2-d102f4d92017",
#         "config_api_url": "TODO",
#     }
# )

app = typer.Typer()
app.add_typer(blocks.app, name="blocks")


@app.command()
def hello():
    from smartspace.cli import auth

    token = auth.get_token()
    print(token)


@app.command()
def login(deviceCode: Annotated[bool, typer.Option("--device-code")] = False):
    from smartspace.cli import auth

    auth.login(deviceCode=deviceCode)


def cli():
    app()


if __name__ == "__main__":
    cli()
