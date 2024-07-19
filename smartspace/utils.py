import inspect
from typing import Callable

from typing_extensions import get_origin

from smartspace.enums import BlockCategory


def _issubclass(cls, base):
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


def description(text: str):
    def __inner(cls):
        cls.description = text
        return cls

    return __inner


def category(category: BlockCategory):
    def __inner(cls):
        cls.category = category.value
        return cls

    return __inner
