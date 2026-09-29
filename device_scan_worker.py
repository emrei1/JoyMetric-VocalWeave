from __future__ import annotations
import json
import os
import sys
import traceback
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        raise SystemExit(2)
    result = Path(sys.argv[1])
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8767
    # This process is intentionally disposable. Any native PyAudioWPatch/PyCAW
    # fault dies here instead of taking the Flask/UI process with it.
    from realtime_native_engine import NativeRealtimeEngine
    engine = NativeRealtimeEngine(ai_host=host, ai_port=port)
    try:
        data = engine.list_devices()
        result.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except BaseException as exc:
        payload = {"inputs": [], "outputs": [], "error": str(exc), "traceback": traceback.format_exc()}
        try:
            result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        finally:
            raise

if __name__ == "__main__":
    main()
