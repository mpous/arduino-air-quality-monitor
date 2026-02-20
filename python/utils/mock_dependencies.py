"""
Mock pyaudio and six so that edge_impulse_linux can be imported on the UNO Q
without those optional (audio-only) dependencies being installed.
Call apply_mocks() at the very top of main.py, before any edge_impulse_linux import.
"""

import sys
import builtins


def apply_mocks() -> None:
    class _Queue:
        Queue = None

    class _Moves:
        queue = _Queue()

    class _Six:
        moves = _Moves()

    sys.modules.setdefault("six", _Six())
    sys.modules.setdefault("six.moves", _Moves())
    sys.modules.setdefault("six.moves.queue", _Queue())

    class _PyAudio:
        pass

    sys.modules.setdefault("pyaudio", _PyAudio())
    builtins.pyaudio = _PyAudio()  # type: ignore[attr-defined]
