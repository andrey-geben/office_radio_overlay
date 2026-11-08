# dtmf_detector.py
import numpy as np
import time

# Стандартная матрица ITU-T Q.23
DTMF_LOW = [697.0, 770.0, 852.0, 941.0]
DTMF_HIGH = [1209.0, 1336.0, 1477.0, 1633.0]

DTMF_MATRIX = {
    (697, 1209): '1', (697, 1336): '2', (697, 1477): '3', (697, 1633): 'A',
    (770, 1209): '4', (770, 1336): '5', (770, 1477): '6', (770, 1633): 'B',
    (852, 1209): '7', (852, 1336): '8', (852, 1477): '9', (852, 1633): 'C',
    (941, 1209): '*', (941, 1336): '0', (941, 1477): '#', (941, 1633): 'D',
}


class DTMFDetector:
    """
    Детектор DTMF из PCM-потока int16.
    Возвращает по одной цифре за непрерывный тон.
    """

    def __init__(self, sample_rate=44100, frame_size=2048,
                 min_digit_ms=40, freq_tolerance_hz=40):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.min_digit_ms = min_digit_ms
        self.freq_tolerance = freq_tolerance_hz
        self._buffer = np.array([], dtype=np.float32)
        self._current_digit = None
        self._digit_start = 0.0
        self._digit_reported = False

    def reset(self):
        self._buffer = np.array([], dtype=np.float32)
        self._current_digit = None
        self._digit_start = 0.0
        self._digit_reported = False

    def feed(self, chunk_bytes):
        """Возвращает список новых цифр, распознанных в этом чанке."""
        try:
            samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            return []
        if len(samples) == 0:
            return []

        self._buffer = np.concatenate([self._buffer, samples])
        if len(self._buffer) < self.frame_size:
            return []

        if len(self._buffer) > self.frame_size * 3:
            self._buffer = self._buffer[-self.frame_size * 2:]

        window = self._buffer[-self.frame_size:]
        digit = self._detect_digit(window)

        now = time.time()
        if digit is None:
            self._current_digit = None
            self._digit_reported = False
            return []

        if digit != self._current_digit:
            self._current_digit = digit
            self._digit_start = now
            self._digit_reported = False
            return []

        duration_ms = (now - self._digit_start) * 1000
        if duration_ms < self.min_digit_ms:
            return []
        if self._digit_reported:
            return []

        self._digit_reported = True
        return [digit]

    def _detect_digit(self, samples):
        n = len(samples)
        if n < 256:
            return None
        windowed = samples * np.hanning(n)
        fft = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(n, 1.0 / self.sample_rate)

        def band_power(f):
            mask = (freqs >= f - self.freq_tolerance) & (freqs <= f + self.freq_tolerance)
            return float(np.sum(fft[mask] ** 2)) if mask.any() else 0.0

        low_powers = {f: band_power(f) for f in DTMF_LOW}
        high_powers = {f: band_power(f) for f in DTMF_HIGH}

        best_low = max(low_powers, key=low_powers.get)
        best_high = max(high_powers, key=high_powers.get)
        low_p = low_powers[best_low]
        high_p = high_powers[best_high]

        low_total = sum(low_powers.values())
        high_total = sum(high_powers.values())
        total = low_total + high_total

        if total < 1e-10:
            return None
        if low_p < low_total * 0.5 or high_p < high_total * 0.5:
            return None

        return DTMF_MATRIX.get((int(best_low), int(best_high)))