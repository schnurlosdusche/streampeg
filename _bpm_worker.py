#!/usr/bin/env python3
"""
Subprocess worker for BPM/Key analysis.
Called by bpm_analyzer.py to run CPU-intensive analysis in a separate process.
Usage: python3 _bpm_worker.py <filepath> <backend>
Output: JSON {"bpm": int, "key": str} on stdout
"""
import sys
import json


def analyze_aubio(filepath):
    import aubio
    bpm = 0
    try:
        src = aubio.source(filepath, samplerate=0, hop_size=512)
        tempo = aubio.tempo("default", 1024, 512, src.samplerate)
        total_frames = 0
        while True:
            samples, read = src()
            tempo(samples)
            total_frames += read
            if read < 512:
                break
        bpm = tempo.get_bpm()
        bpm = int(round(bpm)) if bpm > 0 else 0
    except Exception:
        pass
    # aubio has no key detection
    return bpm, ""


def analyze_essentia(filepath):
    import essentia.standard as es
    bpm = 0
    key = ""
    try:
        audio = es.MonoLoader(filename=filepath, sampleRate=44100)()
        rhythm = es.RhythmExtractor2013(method="multifeature")
        bpm_val = rhythm(audio)[0]
        bpm = int(round(bpm_val)) if bpm_val > 0 else 0
    except Exception:
        pass
    try:
        audio = es.MonoLoader(filename=filepath, sampleRate=44100)()
        k, scale, _ = es.KeyExtractor()(audio)
        if k and scale:
            key = k + "m" if scale == "minor" else k
    except Exception:
        pass
    return bpm, key


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"bpm": 0, "key": ""}))
        sys.exit(1)

    filepath = sys.argv[1]
    backend = sys.argv[2]

    bpm, key = 0, ""
    if backend == "essentia":
        bpm, key = analyze_essentia(filepath)
    elif backend == "aubio":
        bpm, key = analyze_aubio(filepath)

    print(json.dumps({"bpm": bpm, "key": key}))
