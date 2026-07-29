"""
TCR (Track Circuit Reader) protocol simulator.

Public API — the engine and the protocol codec.
"""

from .engine import TcrEngine
from .protocol import pack, decode_message, escape, unescape, calc_crc
