import smartspace.core
import smartspace.utils


def load(
    path: str | None = None,
    block_set: smartspace.core.BlockSet | None = None,
) -> smartspace.core.BlockSet:
    import importlib.util
    import pathlib
    import sys
    from os.path import dirname, isfile

    block_set = block_set or smartspace.core.BlockSet()
    if not path:
        block_set.add(smartspace.core.User)

    smartspace.core.block_scope.set(block_set)

    _path = path or dirname(__file__)
    if isfile(_path):
        file_paths = [_path]
    else:
        file_paths = [str(f) for f in pathlib.Path(_path).glob("**/*.py")]

    existing_modules = {
        m.__file__: m for m in sys.modules.values() if getattr(m, "__file__", None)
    }

    for file_path in file_paths:
        if file_path == __file__ or file_path.endswith("__main__.py"):
            continue

        if file_path in existing_modules:
            module = existing_modules[file_path]
        else:
            module_path = file_path.removeprefix(_path).replace("/", ".")[:-3]
            module_name = module_path.replace("/", ".")

            if path is None:
                module = importlib.import_module(
                    module_path, package="smartspace.blocks"
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    existing_modules[file_path] = module
                    spec.loader.exec_module(module)

    return block_set
