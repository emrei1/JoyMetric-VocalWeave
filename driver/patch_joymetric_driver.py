from __future__ import annotations
import re
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit('usage: patch_joymetric_driver.py <simpleaudiosample-dir>')
root = Path(sys.argv[1]).resolve()
if not (root / 'SimpleAudioSample.sln').exists():
    raise SystemExit(f'not a SimpleAudioSample tree: {root}')

inx = root / 'Source' / 'Main' / 'SimpleAudioSample.inx'
defs = root / 'Source' / 'Inc' / 'definitions.h'
endpoints = root / 'Source' / 'Inc' / 'endpoints.h'
speaker = root / 'Source' / 'Filters' / 'speakerwavtable.h'
for path in (inx, defs, endpoints, speaker):
    if not path.exists():
        raise SystemExit(f'missing upstream file: {path}')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f'{label}: expected one occurrence, found {n}')
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, repl: str, label: str, flags: int = 0) -> str:
    out, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise RuntimeError(f'{label}: expected one regex match, found {n}')
    return out


def detect_text_encoding(path: Path) -> str:
    head = path.read_bytes()[:4]
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def read_source(path: Path) -> tuple[str, str]:
    enc = detect_text_encoding(path)
    return path.read_text(encoding=enc), enc


def write_source(path: Path, text: str, enc: str | None = None) -> None:
    if enc is None:
        enc = detect_text_encoding(path)
    path.write_text(text, encoding=enc)

# v30.4.42: the Microsoft WaveRT speaker implementation is intentionally NOT
# modified.  JoyMetric only rebrands the root device / miniport names and uses
# standard WASAPI loopback on that render endpoint.  This avoids the old
# PCM32/ring mutations entirely.

# Unique JoyMetric product/name GUIDs.
s, enc = read_source(defs)
s = regex_once(
    s,
    r'// \{836BA6D1-3FF7-4411-8BCD-469553452DCE\}.*?DEFINE_GUIDSTRUCT\("836BA6D1-3FF7-4411-8BCD-469553452DCE", PID_SIMPLEAUDIOSAMPLE\);',
    '// {94179EE5-F2CD-4064-AAD8-32406D93A33B}\n'
    '#define STATIC_PID_SIMPLEAUDIOSAMPLE\\\n'
    '    0x94179ee5, 0xf2cd, 0x4064, 0xaa, 0xd8, 0x32, 0x40, 0x6d, 0x93, 0xa3, 0x3b\n'
    'DEFINE_GUIDSTRUCT("94179EE5-F2CD-4064-AAD8-32406D93A33B", PID_SIMPLEAUDIOSAMPLE);',
    'product GUID', flags=re.S,
)
write_source(defs, s, enc)

s, enc = read_source(endpoints)
s = regex_once(
    s,
    r'// \{0104947F-82AE-4291-A6F3-5E2DE1AD7DC2\}.*?DEFINE_GUIDSTRUCT\("0104947F-82AE-4291-A6F3-5E2DE1AD7DC2", NAME_SIMPLE_AUDIO_SAMPLE\);',
    '// {451E9BB2-9B79-40C0-908E-B997157898DF}\n'
    '#define STATIC_NAME_SIMPLE_AUDIO_SAMPLE\\\n'
    '    0x451e9bb2, 0x9b79, 0x40c0, 0x90, 0x8e, 0xb9, 0x97, 0x15, 0x78, 0x98, 0xdf\n'
    'DEFINE_GUIDSTRUCT("451E9BB2-9B79-40C0-908E-B997157898DF", NAME_SIMPLE_AUDIO_SAMPLE);',
    'endpoint GUID', flags=re.S,
)
write_source(endpoints, s, enc)

# Rebrand INF while preserving Microsoft's stock render/capture AddInterface
# sections verbatim.
s, enc = read_source(inx)
s = replace_once(s, 'ROOT\\SimpleAudioSample', 'ROOT\\JoyMetricVirtualAudio', 'root hardware ID')
s = regex_once(
    s,
    r'(?m)^(DriverVer)\s*=\s*02/22/2016,\s*1\.0\.0\.1\s*$',
    r'\1   = 08/28/2026, 0.3.0.0',
    'DriverVer',
)
s = regex_once(s, r'(?m)^ProviderName\s*=\s*"TODO-Set-Provider"\s*$', 'ProviderName = "JoyMetric"', 'provider')
s = regex_once(s, r'(?m)^MfgName\s*=\s*"TODO-Set-Manufacturer"\s*$', 'MfgName = "JoyMetric"', 'manufacturer')
s = regex_once(s, r'(?m)^MsCopyRight\s*=\s*"TODO-Set-Copyright"\s*$', 'MsCopyRight = "Copyright (c) 2026 JoyMetric"', 'copyright')
replacements = {
    'SIMPLEAUDIOSAMPLE_SA.DeviceDesc="Virtual Audio Device (WDM) - Simple Audio Sample"': 'SIMPLEAUDIOSAMPLE_SA.DeviceDesc="JoyMetric Virtual Audio Driver"',
    'SimpleAudioSample.SvcDesc="Virtual Audio Device (WDM) - Simple Audio Sample Driver"': 'SimpleAudioSample.SvcDesc="JoyMetric Virtual Audio Driver"',
    'SIMPLEAUDIOSAMPLE.WaveSpeaker.szPname="Simple Audio Sample Wave Speaker"': 'SIMPLEAUDIOSAMPLE.WaveSpeaker.szPname="JoyMetric Virtual Input"',
    'SIMPLEAUDIOSAMPLE.TopologySpeaker.szPname="Simple Audio Sample Topology Speaker"': 'SIMPLEAUDIOSAMPLE.TopologySpeaker.szPname="JoyMetric Virtual Input"',
    'SIMPLEAUDIOSAMPLE.WaveMicArray1.szPname="Simple Audio Sample Wave Microphone Array - Front"': 'SIMPLEAUDIOSAMPLE.WaveMicArray1.szPname="JoyMetric Internal Capture"',
    'SIMPLEAUDIOSAMPLE.TopologyMicArray1.szPname="Simple Audio Sample Topology Microphone Array - Front"': 'SIMPLEAUDIOSAMPLE.TopologyMicArray1.szPname="JoyMetric Internal Capture"',
    'MicArray1CustomName= "Internal Microphone Array - Front"': 'MicArray1CustomName= "JoyMetric Internal Capture"',
}
for old, new in replacements.items():
    s = replace_once(s, old, new, f'INF string: {old[:30]}')

required_inf = [
    'AddInterface=%KSCATEGORY_RENDER%, %KSNAME_WaveSpeaker%, SIMPLEAUDIOSAMPLE.I.WaveSpeaker',
    'AddInterface=%KSCATEGORY_AUDIO%, %KSNAME_WaveSpeaker%, SIMPLEAUDIOSAMPLE.I.WaveSpeaker',
    'SIMPLEAUDIOSAMPLE.WaveSpeaker.szPname="JoyMetric Virtual Input"',
    'ROOT\\JoyMetricVirtualAudio',
    '0.3.0.0',
]
for token in required_inf:
    if token not in s:
        raise RuntimeError(f'render-endpoint patch verification failed: {token}')
write_source(inx, s, enc)

# Tolerant stock-render sanity check.  Do not require exact comment spacing or
# macro spelling from a particular upstream commit.  The file is never written.
spk, _ = read_source(speaker)
if 'SpeakerHostPinSupportedDeviceFormats' not in spk:
    raise RuntimeError('upstream speaker render format table is missing')
if 'KSPIN_DATAFLOW_IN' not in spk or 'KSPIN_COMMUNICATION_SINK' not in spk:
    raise RuntimeError('upstream speaker render pin is missing/invalid')
if not re.search(r'WAVE_FORMAT_EXTENSIBLE[\s\S]{0,900}?\b2\s*,\s*48000\s*,\s*192000\s*,\s*4\s*,\s*16\s*,', spk):
    raise RuntimeError('upstream stock 48kHz/stereo/PCM16 speaker format was not recognized')

print('JoyMetric render-endpoint patch: OK')
print('  driver: ROOT\\JoyMetricVirtualAudio / v0.3.0.0')
print('  render miniport: Microsoft stock WaveRT (unmodified)')
print('  expected render format: 48 kHz / stereo / PCM16')
print('  output endpoint: JoyMetric Virtual Input')
print('  capture path: private WASAPI loopback of the render endpoint')
