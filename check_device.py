"""Quick sanity check for the RTL-SDR dongle.

Run this AFTER installing the WinUSB driver with Zadig:

    python check_device.py

Expected: it opens the device, prints tuner info, grabs a short capture,
and reports basic signal stats. If it fails with LIBUSB_ERROR_NOT_FOUND,
the WinUSB driver is not bound yet -> run Zadig.
"""
from __future__ import annotations

import rtl_sdr_env  # noqa: F401  (registers the bundled DLL dir)
import numpy as np
from rtlsdr import RtlSdr, librtlsdr


def main() -> int:
    n = librtlsdr.rtlsdr_get_device_count()
    print(f"Devices found: {n}")
    if n == 0:
        print("No RTL-SDR enumerated. Is it plugged in?")
        return 1
    print(f"Device 0: {librtlsdr.rtlsdr_get_device_name(0).decode(errors='replace')}")

    try:
        sdr = RtlSdr()
    except Exception as e:  # noqa: BLE001
        print(f"OPEN FAILED: {type(e).__name__}: {e}")
        if "LIBUSB_ERROR_NOT_FOUND" in str(e):
            print("-> No WinUSB driver bound. Run tools/rtl-sdr/zadig.exe on "
                  "'Bulk-In, Interface (Interface 0)'.")
        return 2

    try:
        sdr.sample_rate = 2.048e6      # Hz
        sdr.center_freq = 100.0e6      # Hz (placeholder; set to your reference tx)
        sdr.gain = "auto"
        print(f"Tuner:        {sdr.get_tuner_type()}")
        print(f"Sample rate:  {sdr.sample_rate/1e6:.3f} Msps")
        print(f"Center freq:  {sdr.center_freq/1e6:.3f} MHz")

        samples = sdr.read_samples(256 * 1024)  # complex64 IQ
        power_db = 10 * np.log10(np.mean(np.abs(samples) ** 2) + 1e-12)
        print(f"Captured:     {len(samples)} IQ samples")
        print(f"Mean power:   {power_db:6.1f} dB (relative)")
        print("OK -- device is working.")
    finally:
        sdr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
