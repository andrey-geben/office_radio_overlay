# mixer.py
import numpy as np
from pydub import AudioSegment
import config


def load_announcement(path=None):
    if path is None:
        path = config.ANNOUNCEMENT_FILE
    audio = AudioSegment.from_file(path)
    audio = audio.set_frame_rate(config.SAMPLE_RATE).set_channels(config.CHANNELS)
    return audio.raw_data


def mix_with_ducking(radio_chunk, announcement_chunk, ducking_level=None, ann_level=None):
    if ducking_level is None:
        ducking_level = config.DUCKING_LEVEL
    if ann_level is None:
        ann_level = config.ANNOUNCEMENT_LEVEL

    radio = np.frombuffer(radio_chunk, dtype=np.int16).astype(np.float32)
    ann = np.frombuffer(announcement_chunk, dtype=np.int16).astype(np.float32)

    min_len = min(len(radio), len(ann))
    radio, ann = radio[:min_len], ann[:min_len]

    mixed = radio * ducking_level + ann * ann_level
    mixed = np.clip(mixed, -32768, 32767)
    return mixed.astype(np.int16).tobytes()