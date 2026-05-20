"""State for tp<idx> to inferior mapping."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TPUThreadBinding:
    tp_name: str
    inferior_num: int


_BINDINGS: dict[str, TPUThreadBinding] = {}


def reset_tpu_thread_bindings() -> None:
    _BINDINGS.clear()


def set_tpu_thread_binding(tp_name: str, inferior_num: int) -> None:
    _BINDINGS[tp_name] = TPUThreadBinding(tp_name=tp_name, inferior_num=inferior_num)


def get_tpu_thread_binding(tp_name: str) -> TPUThreadBinding | None:
    return _BINDINGS.get(tp_name)


def list_tpu_thread_bindings() -> list[TPUThreadBinding]:
    def _sort_key(item: TPUThreadBinding) -> int:
        suffix = item.tp_name[2:] if item.tp_name.startswith("tp") else item.tp_name
        return int(suffix) if suffix.isdigit() else 10**9

    return sorted(_BINDINGS.values(), key=_sort_key)
