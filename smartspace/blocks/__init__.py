from typing import cast

from semantic_version import NpmSpec

from smartspace.core import Block, MetaBlock


def find_block(name: str, version: str) -> type[Block]:
    spec = NpmSpec(version)
    if name not in MetaBlock._all_block_types:
        raise KeyError(f"Could not find and versions for block '{name}'")

    versions = {v.semantic_version: v for v in MetaBlock._all_block_types[name]}
    best_version = spec.select(versions.keys())

    if best_version is None:
        raise KeyError(f"No version matching '{version}' found for block '{name}'")

    return versions[best_version]


def load(path: str | None = None) -> dict[str, dict[str, type[Block]]]:
    import glob
    import importlib.util
    import sys
    from os.path import dirname, isfile

    from smartspace.core import Block
    from smartspace.utils import _issubclass

    blocks: dict[str, dict[str, type[Block]]] = {}

    _path = path or dirname(__file__)
    if isfile(_path):
        file_paths = [_path]
    else:
        file_paths = glob.glob(_path + "/**/*.py", recursive=True)

    for file_path in file_paths:
        if file_path == __file__ or file_path.endswith("__main__.py"):
            continue

        module_path = file_path.removeprefix(_path).replace("/", ".")[:-3]
        module_name = module_path.replace("/", ".")

        if path is None:
            module = importlib.import_module(module_path, package="smartspace.blocks")
        else:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            else:
                module = None

        if not module:
            continue

        for name in dir(module):
            item = getattr(module, name)
            if _issubclass(item, Block) and item != Block:
                block_type = cast(type[Block], item)
                if block_type.name not in blocks:
                    blocks[block_type.name] = {}

                blocks[block_type.name][block_type.version] = block_type

    return blocks


del Block
