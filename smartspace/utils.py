import inspect
from typing import Callable

from typing_extensions import get_origin


def my_issubclass(cls, base):
    return inspect.isclass(cls) and issubclass(get_origin(cls) or cls, base)


def get_return_type(callable: Callable):
    signature = inspect.signature(callable)
    return (
        signature.return_annotation
        if signature.return_annotation != signature.empty
        else None
    )


def get_parameter_names_and_types(callable: Callable):
    signature = inspect.signature(callable)
    return [
        (name, param.annotation)
        for name, param in signature.parameters.items()
        if name != "self"
    ]
