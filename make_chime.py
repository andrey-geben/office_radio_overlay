# make_chime.py
"""
Генерирует WAV-файл «плим-блим» — сигнал внимания перед SIP-звонком.
Стилизация: рында / корабельный колокол — яркий, металлический, долгий звон.

Запуск:  python make_chime.py [путь] [--volume=0.85]
Или автоматически при старте main.py, если файла нет.
"""
import os
import sys
import wave
import numpy as np


def bell_strike(freq, duration, sample_rate=44100, strike_strength=1.0):
    """
    Один удар колокола.
    Частичные тона (partials) взяты по спектру реального колокола:
    hum, prime, tierce (малая терция — характерный «кланг»),
    quint, nominal (яркость), superquint, upper octave.
    """
    n = int(sample_rate * duration)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / sample_rate

    # (ratio, amplitude, decay_rate)
    partials = [
        (0.5,   0.18, 0.7),   # hum (суб-октава)
        (1.0,   0.50, 1.1),   # prime — основа
        (1.19,  0.50, 1.3),   # tierce — характерный "кланг"
        (1.5,   0.22, 1.6),   # quint
        (2.0,   0.70, 2.0),   # nominal — яркость
        (2.51,  0.32, 2.6),   # superquint
        (2.67,  0.22, 2.8),   # minor seventh
        (3.01,  0.38, 3.2),   # octave nominal
        (4.0,   0.22, 3.8),   # upper quint
        (5.03,  0.14, 4.2),   # upper octave
    ]

    signal = np.zeros(n)
    for ratio, amp, decay in partials:
        f = freq * ratio
        env = np.exp(-decay * t)
        # Два слегка расстроенных компонента → биения → «мерцание»
        signal += amp * 0.65 * env * np.sin(2 * np.pi * f * t)
        signal += amp * 0.35 * env * np.sin(2 * np.pi * f * 1.003 * t)

    # Удар молотка — короткий высокочастотный всплеск
    attack_env = np.exp(-t / 0.006)
    signal += 0.45 * strike_strength * attack_env * np.sin(2 * np.pi * freq * 5.3 * t)

    # Шумовой «металлический» призвук в первые 20 мс
    noise = np.random.randn(n) * 0.09
    noise_env = np.exp(-120 * t)
    signal += noise * noise_env * strike_strength

    # Атака 2 мс — без щелчка
    a = int(sample_rate * 0.002)
    if 0 < a < n:
        signal[:a] *= np.linspace(0, 1, a)

    # Плавное затухание в конце (100 мс)
    f_out = int(sample_rate * 0.1)
    if 0 < f_out < n:
        signal[-f_out:] *= np.linspace(1, 0, f_out)

    return signal


def generate_chime(output_path, sample_rate=44100, volume=0.85):
    """
    Два перекрывающихся удара:
      Note 1: A5 (880 Гц) — яркий, «плим»
      Note 2: E5 (659 Гц) — ниже, «блим», с долгим звоном
    Общая длительность ~2.6 с.
    """
    s1 = bell_strike(880.00, duration=1.3, sample_rate=sample_rate, strike_strength=1.0)
    s2 = bell_strike(659.25, duration=2.2, sample_rate=sample_rate, strike_strength=1.15)

    offset = int(0.45 * sample_rate)
    total_len = max(len(s1), offset + len(s2))
    combined = np.zeros(total_len)
    combined[:len(s1)] += s1
    combined[offset:offset + len(s2)] += s2

    peak = float(np.max(np.abs(combined))) if len(combined) else 0.0
    if peak > 0:
        combined = combined / peak * volume

    samples = (combined * 32767).astype(np.int16)

    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())

    return len(samples)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    volume = 0.85
    for a in sys.argv[1:]:
        if a.startswith("--volume="):
            try:
                volume = float(a.split("=", 1)[1])
            except ValueError:
                pass
    out = args[0] if args else "assets/chime.wav"
    n = generate_chime(out, volume=volume)
    print(f"✅ Сгенерирован: {out}")
    print(f"   Длительность: {n / 44100:.2f} с")
    print(f"   Размер: {os.path.getsize(out) // 1024} КБ")
    print(f"   Громкость: {volume:.0%}")