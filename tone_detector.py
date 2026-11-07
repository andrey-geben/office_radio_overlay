# tone_detector.py
import numpy as np
import config

CTCSS_FREQUENCIES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]


def goertzel(samples, target_freq, sample_rate):
    N = len(samples)
    k = int(0.5 + (N * target_freq) / sample_rate)
    omega = (2.0 * np.pi * k) / N
    coeff = 2.0 * np.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2**2 + s_prev**2 - coeff * s_prev * s_prev2


def _lowpass(samples, cutoff=300.0, sample_rate=None, order=4):
    if sample_rate is None:
        sample_rate = config.SAMPLE_RATE
    try:
        from scipy.signal import butter, lfilter
        b, a = butter(order, cutoff, btype='low', fs=sample_rate)
        return lfilter(b, a, samples)
    except Exception:
        return samples


def detect_tone(audio_chunk, target_freq=None, threshold=None):
    if target_freq is None:
        target_freq = config.CTCSS_TONE_FREQ
    if threshold is None:
        threshold = config.DETECTION_THRESHOLD
    samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32)
    if np.max(np.abs(samples)) > 0:
        samples = samples / np.max(np.abs(samples))
    power = goertzel(samples, target_freq, config.SAMPLE_RATE)
    normalized = float(power) / max(len(samples), 1)
    return normalized > threshold, normalized


def detect_all(audio_chunk, configs):
    samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return {c["name"]: {"detected": False, "power": 0.0} for c in configs}
    peak = np.max(np.abs(samples))
    if peak > 0:
        samples = samples / peak

    out = {}
    for c in configs:
        power = goertzel(samples, c["freq"], config.SAMPLE_RATE)
        normalized = float(power) / len(samples)
        out[c["name"]] = {
            "detected": bool(normalized > c["threshold"]),
            "power": normalized,
        }
    return out


def scan_ctcss_frequencies(audio_chunk, sample_rate=None, top_n=10):
    if sample_rate is None:
        sample_rate = config.SAMPLE_RATE

    samples = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return []

    peak = np.max(np.abs(samples))
    if peak > 0:
        samples = samples / peak

    filtered = _lowpass(samples, cutoff=300.0, sample_rate=sample_rate)

    results = []
    for freq in CTCSS_FREQUENCIES:
        if freq >= sample_rate / 2:
            continue
        power = goertzel(filtered, freq, sample_rate)
        normalized = float(power) / len(filtered)
        results.append({"freq": freq, "power": round(normalized, 6)})

    results.sort(key=lambda x: x["power"], reverse=True)
    return results[:top_n]