# JoyMetric Virtual Audio Driver — development source

This directory contains JoyMetric's custom PCM cable patch/build tooling.
It uses Microsoft's open-source **Simple Audio Sample** only as the WDM/WaveRT
framework; `patch_joymetric_driver.py` replaces the sample tone/file behavior
with a real kernel render→capture ring transport and assigns JoyMetric-specific
IDs/names.

Data path after installation:

`Spotify -> JoyMetric Virtual Input -> kernel PCM ring -> JoyMetric Internal Capture -> Clean6x DSP -> selected physical Audio Out`

The app hides both JoyMetric endpoints from its normal Input/Output selectors.
The user selects Spotify and a physical Audio Out only.

Development-driver note: local builds require the Windows Driver Kit and a
trusted test signature. Production distribution requires normal Microsoft
kernel-driver signing/validation; do not ship a test-signed build to end users.
