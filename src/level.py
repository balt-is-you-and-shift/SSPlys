from enum import IntEnum
from functools import lru_cache
from io import BytesIO
from itertools import chain
import time
import traceback

import numpy as np
from PIL import Image
from pydub import AudioSegment
import struct
from abc import ABC, abstractmethod


def read_line(file):
    out = bytearray(b"")
    while (f := file.read(1)) != b"\n":
        out.append(int.from_bytes(f, "little"))
    return out.decode("utf-8")


class Level(ABC):
    def __init__(self,
                 name: str = "Unnamed",
                 author: str = "Unknown Author",
                 notes: dict[int, list[tuple[int, int]]] = None,
                 cover: Image.Image = None,
                 audio: AudioSegment = None,
                 difficulty: int | str = -1):
        self.id = (author.lower() + " " + name.lower()).replace(" ", "_")
        self.name = name
        self.author = author
        self.notes = notes if notes is not None else {}
        self.cover = cover
        self.audio = audio
        self.difficulty = difficulty

    def __str__(self):
        return f"{self.__class__.__name__}(author: {self.author}, cover: {self.cover}, difficulty: {self.difficulty}, id: {self.id}, name: {self.name}, notes: ({len(self.notes)} notes))"

    def get_end(self):
        times_to_display = self.get_notes()
        return (np.max(times_to_display) if times_to_display.shape[0] > 0 else 1000)

    def get_notes(self):
        return np.sort(np.array(tuple(self.notes.keys()), dtype=np.int32))

    @classmethod
    @abstractmethod
    def load(cls, file):
        raise NotImplementedError

    @abstractmethod
    def save(self, *_):
        raise NotImplementedError


class SSPMLevel(Level):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def load(cls, file):
        assert file.read(4) == b"SS+m", "Invalid file signature! Your level might be corrupted, or in the wrong format."
        assert file.read(2) == b"\x01\x00", \
            "Sorry, this doesn't support SSPM v2. Use the in-game editor."
        assert file.read(2) == b"\x00\x00", \
            "Reserved bits aren't 0. Is this a modchart?"
        read_line(file)
        song_name = read_line(file)
        song_author = read_line(file)
        file.read(4)  # MS length, not needed
        note_count = int.from_bytes(file.read(4), "little")
        difficulty = int.from_bytes(file.read(1), "little") - 1
        cover = None
        if file.read(1) == b"\x02":
            data_length = int.from_bytes(file.read(8), "little")
            image_data = file.read(data_length)
            with BytesIO(image_data) as io:
                with Image.open(io) as im:
                    cover = im.copy()
        audio = None
        if file.read(1) == b"\x01":
            data_length = int.from_bytes(file.read(8), "little")
            audio_data = file.read(data_length)
            with BytesIO(audio_data) as io:
                audio = AudioSegment.from_file(io).set_sample_width(2)  # HACK: if i don't do this, it plays horribly clipped and way too loud. it's a simpleaudio bug :/
        notes = {}
        for _ in range(note_count):
            timing = int.from_bytes(file.read(4), "little")
            if file.read(1) == b"\x00":
                x = int.from_bytes(file.read(1), "little")
                y = int.from_bytes(file.read(1), "little")
            else:
                x, y = struct.unpack("ff", file.read(8))  # nice
            if timing in notes:
                notes[timing].append((x, y))
            else:
                notes[timing] = [(x, y)]
        metadata = True
        try:
            assert file.read(4) == b"SSPy"
            bpm = struct.unpack("d", file.read(8))[0]
            offset = int.from_bytes(file.read(4), "little")
            time_signature = struct.unpack("HH", file.read(4))
            swing = struct.unpack("d", file.read(8))[0]
        except (EOFError, AssertionError):
            metadata = False
        return cls(song_name, song_author, notes, cover, audio, difficulty), (bpm, offset, time_signature, swing) if metadata else None

    def save(self, bpm, offset, time_signature, swing):
        with BytesIO() as output:
            print("Writing metadata...")
            output.write(b"SS+m\x01\x00\x00\x00")
            output.write(bytes(self.id + "\n", "utf-8"))
            output.write(bytes(self.name + "\n", "utf-8"))
            output.write(bytes(self.author + "\n", "utf-8"))
            output.write(
                (max(self.notes.keys()) if len(self.notes) else 0).to_bytes(4, "little")
            )
            output.write(
                len(tuple(chain(*self.notes.values()))).to_bytes(4, "little")
            )
            output.write(
                (self.difficulty + 1).to_bytes(1, "little")
            )
            if self.cover is None:
                output.write(b"\x00")
            else:
                print(f"Writing cover...")
                output.write(b"\x02")
                with BytesIO() as im_data:
                    self.cover.save(im_data, format="PNG")
                    output.write(
                        im_data.seek(0, 2).to_bytes(8, "little")
                    )
                    output.write(im_data.getvalue())
            if self.audio is None:
                output.write(b"\x00")
            else:
                print(f"Writing song...")
                output.write(b"\x01")
                with BytesIO() as audio_data:
                    self.audio.export(audio_data)
                    output.write(
                        audio_data.seek(0, 2).to_bytes(8, "little")
                    )
                    output.write(audio_data.getvalue())
            for i, (timing, notes) in enumerate(dict(sorted(self.notes.items())).items()):
                print(f"\rWriting notes... ({i+1: >{(len(str(len(self.notes))))}}/{len(self.notes)})", end="")
                for position in notes:
                    output.write(
                        timing.to_bytes(4, "little")
                    )
                    if all([n == int(n) for n in position]):
                        output.write(b"\x00")
                        output.write(int(position[0]).to_bytes(1, "little"))
                        output.write(int(position[1]).to_bytes(1, "little"))
                    else:
                        output.write(b"\x01")
                        output.write(struct.pack("<ff", *position))
            # Write metadata past the notes, SS+ allows this
            output.write(b"SSPy")
            output.write(struct.pack("d", bpm))
            output.write(offset.to_bytes(4, "little"))
            output.write(struct.pack("<HH", *time_signature))
            output.write(struct.pack("d", swing))
            print()
            return output.getvalue()


class RawDataLevel(Level):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def load(cls, file):
        notes = {}
        data_string = file.read().decode("utf-8")
        for note in data_string.split(",")[1:]:
            try:
                x, y, timing = note.split("|")
                x, y = float(x), float(y)
                try:
                    notes[int(timing)].append((x, y))
                except KeyError:
                    notes[int(timing)] = [(x, y)]
            except ValueError:
                print(f"/!\ Invalid note! {note}")
        return cls(notes=notes), None

    def save(self, *_):
        output = []
        for timing, notes in dict(sorted(self.notes.items())).items():
            for note in notes:
                x = 2 - note[0]
                y = 2 - note[1]
                x = int(x) if int(x) == x else x
                y = int(y) if int(y) == y else y
                output.append(f"{2-x}|{2-y}|{timing}")
        return (self.id + "," + ",".join(output)).encode("utf-8")
