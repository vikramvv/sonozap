Sonaer Sonozap Preflight GUI
============================

Standalone Tkinter preflight tool for verifying RS-232 communication with a Sonaer ultrasonic generator before running deposition scripts.

Usage:

    hatch run app

Dependencies:

    pyserial

## Notes

**Connect-Request ordering (spec requirement):** The Sonaer protocol spec (Rev F, section 3.1) states the first command to the device must be `Connect-Request`. An earlier version of `sonaer_control_gui.py` sent a `Ping` before `Connect-Request`, which caused the connection sequence to fail. Fixed so `Connect-Request` is always sent first.
