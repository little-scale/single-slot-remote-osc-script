"""Small strict OSC 1.0 codec; no third-party modules or Live dependencies."""
import math
import struct


def _string(value):
    raw = value.encode("utf-8") + b"\0"
    return raw + b"\0" * (-len(raw) % 4)


def encode(address, args=()):
    tags, data = ",", b""
    for arg in args:
        if isinstance(arg, bool):
            tags += "T" if arg else "F"
        elif isinstance(arg, int):
            tags += "i"
            data += struct.pack(">i", arg)
        elif isinstance(arg, float):
            tags += "f"
            data += struct.pack(">f", arg)
        elif isinstance(arg, str):
            tags += "s"
            data += _string(arg)
        else:
            raise ValueError("Unsupported OSC argument")
    return _string(address) + _string(tags) + data


def decode(packet, limit=64):
    messages = []

    def string(data, pos):
        end = data.find(b"\0", pos)
        if end < 0:
            raise ValueError("Unterminated OSC string")
        next_pos = (end + 4) & ~3
        if next_pos > len(data) or any(data[end:next_pos]):
            raise ValueError("Bad OSC string padding")
        return data[pos:end].decode("utf-8"), next_pos

    def parse(data, depth=0):
        if depth > 8 or not data or len(data) % 4:
            raise ValueError("Invalid OSC packet")
        if data.startswith(b"#bundle\0"):
            if len(data) < 16 or data[8:16] != struct.pack(">Q", 1):
                raise ValueError("Only immediate OSC bundles are supported")
            pos = 16
            while pos < len(data):
                size = struct.unpack_from(">i", data, pos)[0]
                pos += 4
                if size <= 0 or pos + size > len(data):
                    raise ValueError("Invalid OSC bundle element")
                parse(data[pos:pos + size], depth + 1)
                pos += size
            return
        address, pos = string(data, 0)
        tags, pos = string(data, pos)
        if not address.startswith("/") or not tags.startswith(","):
            raise ValueError("Invalid OSC address or type tags")
        args = []
        for tag in tags[1:]:
            if tag == "s":
                value, pos = string(data, pos)
            elif tag in "if":
                if pos + 4 > len(data):
                    raise ValueError("Truncated OSC number")
                value = struct.unpack_from(">" + tag, data, pos)[0]
                pos += 4
                if tag == "f" and not math.isfinite(value):
                    raise ValueError("Non-finite OSC value")
            elif tag in "TF":
                value = tag == "T"
            else:
                raise ValueError("Unsupported OSC type: " + tag)
            args.append(value)
        if pos != len(data):
            raise ValueError("Trailing OSC data")
        messages.append((address, args))
        if len(messages) > limit:
            raise ValueError("Too many OSC messages")

    parse(packet)
    return messages
