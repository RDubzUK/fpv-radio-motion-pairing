"""Bound MP4 presentation ends without changing compressed source packets.

FFmpeg stream-copy -t can retain reordered packets past the requested end.
Its edit list already hides decoding preroll; limit that presentation window
and the movie/track durations. Editors still need to honor MP4 edit lists.
"""
from __future__ import annotations

import struct


def atoms(data, start=0, stop=None):
    stop = len(data) if stop is None else stop
    at = start
    while at + 8 <= stop:
        size, kind = struct.unpack_from(">I4s", data, at)
        header = 8
        if size == 1:
            if at + 16 > stop:
                raise ValueError("Invalid MP4 atom")
            size = struct.unpack_from(">Q", data, at + 8)[0]
            header = 16
        elif size == 0:
            size = stop - at
        if size < header or at + size > stop:
            raise ValueError("Invalid MP4 atom size")
        yield kind, at + header, at + size
        at += size


def shorten(path, seconds):
    with path.open("r+b") as stream:
        stream.seek(0, 2)
        length = stream.tell()
        position = 0
        while position + 8 <= length:
            stream.seek(position)
            size, kind = struct.unpack(">I4s", stream.read(8))
            header = 8
            if size == 1:
                size = struct.unpack(">Q", stream.read(8))[0]
                header = 16
            if not size:
                size = length - position
            if size < header or position + size > length:
                raise ValueError("Invalid MP4 structure")
            if kind == b"moov":
                break
            position += size
        else:
            raise ValueError("No MP4 movie atom")
        if size > 64 * 1024 ** 2:
            raise ValueError("MP4 metadata is too large for presentation trimming")
        stream.seek(position + header)
        data = bytearray(stream.read(size - header))
        movie = list(atoms(data))
        mvhd = next(begin for kind, begin, end in movie if kind == b"mvhd")
        version = data[mvhd]
        timescale = struct.unpack_from(">I", data, mvhd + (20 if version == 1 else 12))[0]
        ticks = round(seconds * timescale)
        if ticks <= 0:
            raise ValueError("Presentation interval is too short")

        def cap(begin, offset, wide=False):
            fmt = ">Q" if wide else ">I"
            old = struct.unpack_from(fmt, data, begin + offset)[0]
            struct.pack_into(fmt, data, begin + offset, min(old, ticks))

        cap(mvhd, 24 if version == 1 else 16, version == 1)
        for kind, begin, end in movie:
            if kind != b"trak":
                continue
            for child, content, tail in atoms(data, begin, end):
                if child == b"tkhd":
                    wide = data[content] == 1
                    cap(content, 28 if wide else 20, wide)
                if child == b"edts":
                    for box, entry, finish in atoms(data, content, tail):
                        if box != b"elst":
                            continue
                        wide = data[entry] == 1
                        count = struct.unpack_from(">I", data, entry + 4)[0]
                        step, fmt = (20, ">Q") if wide else (12, ">I")
                        if count not in (1, 2) or entry + 8 + count * step > finish:
                            raise ValueError("Unsupported MP4 edit list; choose Accurate trim")
                        remaining = ticks
                        for i in range(count):
                            offset = entry + 8 + i * step
                            old = struct.unpack_from(fmt, data, offset)[0]
                            length = min(old, remaining)
                            struct.pack_into(fmt, data, offset, length)
                            remaining -= length
        # Atom sizes and all sample/chunk offsets are unchanged.
        stream.seek(position + header)
        stream.write(data)
