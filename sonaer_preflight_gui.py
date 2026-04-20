"""
Sonaer Sonozap Ultrasonic Generator — Preflight GUI
====================================================

Standalone Tkinter preflight tool for verifying RS-232 communication with a
Sonaer ultrasonic generator (NS60K / 120 kHz / etc.) before running any
deposition scripts.

Checks performed (in order):
  1. Serial port opens at 38400 8N1
  2. Ping round-trip (opcode 0x01)
  3. Connect-Request (required before any other command)
  4. Get Software Version
  5. Get Frequency (should be ~60 kHz for NS60K, 120 kHz for 120K heads)
  6. Get Power-Level (%)
  7. Request-Fault (should be 0 with probe idle, or 2 if probe unplugged)
  8. Disconnect (always runs in finally, even on failure)

Dependencies: pyserial  (pip install pyserial)

Usage: python sonaer_preflight_gui.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import time
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise SystemExit(
        "pyserial is required. Install with:  pip install pyserial"
    )


# ---------------------------------------------------------------------------
# Protocol layer
# ---------------------------------------------------------------------------

BAUD = 38400
READ_TIMEOUT = 0.5   # seconds
WRITE_TIMEOUT = 0.5
POST_WRITE_DELAY = 0.04  # spec says <20 ms; give margin for USB-serial latency


def build_packet(opcode: int, *data: int) -> bytes:
    """Build a Sonaer command packet: [Length][Opcode][Data...][Checksum]."""
    body = bytes([opcode, *data])
    length = len(body) + 1  # +1 for checksum byte
    checksum = (-sum(body)) & 0xFF
    return bytes([length, *body, checksum])


def parse_response(raw: bytes) -> tuple[int, int, bytes]:
    """Parse a response packet.

    Returns (status, opcode, payload_bytes) where payload_bytes is everything
    between the opcode byte and the checksum byte (exclusive of both).

    NOTE: The Sonaer spec §3.3 shows inconsistent formats for Get responses —
    some examples include an echoed parameter byte before the value (e.g.
    Get Power-Level: 05 00 02 04 41 B9 where 0x04 is the echoed param), while
    others do not (Request-Fault: 04 00 02 00 FE with no echo). Because of
    this, we return the raw payload and let each caller use the known expected
    value length to slice the actual value. See `extract_value()` helper.

    Raises ValueError on bad checksum or malformed packet.
    """
    if len(raw) < 4:
        raise ValueError(f"Response too short: {raw.hex(' ')}")
    length = raw[0]
    if len(raw) != length + 1:
        raise ValueError(
            f"Length mismatch: declared {length}, got {len(raw) - 1} bytes "
            f"after length byte ({raw.hex(' ')})"
        )
    rest = raw[1:]
    if (sum(rest) & 0xFF) != 0:
        raise ValueError(f"Bad checksum: {raw.hex(' ')}")
    status = rest[0]
    opcode = rest[1]
    payload = rest[2:-1]  # everything between opcode and checksum
    return status, opcode, payload


def extract_value(payload: bytes, expected_value_len: int) -> bytes:
    """Extract the actual value bytes from a response payload.

    Handles both response formats seen in the Sonaer spec:
      - payload == expected_value_len bytes: no param echo (e.g. Request-Fault per spec)
      - payload == expected_value_len + 1 bytes: first byte is echoed param (e.g. Get Power-Level per spec)

    Raises ValueError if payload length doesn't match either form.
    """
    if len(payload) == expected_value_len:
        return payload
    if len(payload) == expected_value_len + 1:
        return payload[1:]  # strip echoed parameter byte
    raise ValueError(
        f"Unexpected payload length: got {len(payload)} bytes, expected "
        f"{expected_value_len} (no echo) or {expected_value_len + 1} (with echo)"
    )


def send_recv(port: serial.Serial, packet: bytes) -> bytes:
    """Write a packet and read back the complete response.

    Reads Length byte first, then reads Length more bytes. Returns raw bytes.
    """
    port.reset_input_buffer()
    port.write(packet)
    time.sleep(POST_WRITE_DELAY)

    length_byte = port.read(1)
    if not length_byte:
        raise TimeoutError("No response from device (timeout)")
    length = length_byte[0]

    rest = port.read(length)
    if len(rest) != length:
        raise TimeoutError(
            f"Short response: expected {length} bytes after length byte, "
            f"got {len(rest)}"
        )
    return length_byte + rest


STATUS_CODES = {
    0x00: "OK",
    0x11: "Unknown opcode",
    0x12: "Unknown parameter",
    0x13: "Invalid value",
    0x40: "General comms error",
    0x41: "Device timeout",
    0x42: "Bad length",
    0x43: "Bad checksum",
}

FAULT_CODES = {
    0: "No fault / Normal",
    1: "Current overload",
    2: "Probe not connected",
    3: "Incorrect frequency or excessive load",
    4: "Internal error (cycle power)",
    5: "Under-voltage",
    6: "Line voltage",
    100: "Max error",
    101: "Warning: more power required",
}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PreflightGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Sonaer Sonozap — Preflight")
        self.root.geometry("760x680")

        self.port = None  # type: serial.Serial | None
        self._build_ui()
        self.refresh_ports()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Top frame: port selection + actions
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="COM Port:").grid(row=0, column=0, sticky="w")

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            top, textvariable=self.port_var, width=30, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, padx=5, sticky="w")

        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(
            row=0, column=2, padx=5
        )

        self.run_btn = ttk.Button(
            top, text="Run Preflight", command=self.run_preflight_threaded
        )
        self.run_btn.grid(row=0, column=3, padx=5)

        self.ping_btn = ttk.Button(
            top, text="Ping Only", command=self.run_ping_threaded
        )
        self.ping_btn.grid(row=0, column=4, padx=5)

        ttk.Button(top, text="Clear Log", command=self.clear_log).grid(
            row=0, column=5, padx=5
        )

        # Middle frame: check-by-check status indicators
        status_frame = ttk.LabelFrame(
            self.root, text="Check Results", padding=10
        )
        status_frame.pack(fill="x", padx=10, pady=5)

        self.checks = [
            ("port",      "Open serial port"),
            ("ping",      "Ping round-trip"),
            ("connect",   "Connect-Request"),
            ("version",   "Get Software Version"),
            ("frequency", "Get Frequency"),
            ("power",     "Get Power-Level"),
            ("fault",     "Request-Fault"),
            ("disconnect","Disconnect"),
        ]
        self.status_labels = {}
        self.detail_labels = {}

        for i, (key, label) in enumerate(self.checks):
            ttk.Label(status_frame, text=label + ":").grid(
                row=i, column=0, sticky="w", pady=2
            )
            status = ttk.Label(status_frame, text="—", width=8,
                               foreground="gray")
            status.grid(row=i, column=1, sticky="w", padx=10)
            self.status_labels[key] = status

            detail = ttk.Label(status_frame, text="", foreground="#333")
            detail.grid(row=i, column=2, sticky="w")
            self.detail_labels[key] = detail

        # Summary banner
        self.summary = ttk.Label(
            self.root, text="Ready.", font=("TkDefaultFont", 11, "bold"),
            padding=10
        )
        self.summary.pack(fill="x")

        # Bottom frame: raw TX/RX log
        log_frame = ttk.LabelFrame(
            self.root, text="Transaction Log (TX / RX hex bytes)",
            padding=5
        )
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log = scrolledtext.ScrolledText(
            log_frame, wrap="word", height=15,
            font=("Consolas", 9)
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("tx", foreground="#0050a0")
        self.log.tag_config("rx", foreground="#006000")
        self.log.tag_config("err", foreground="#a00000")
        self.log.tag_config("info", foreground="#606060")
        self.log.tag_config("ok", foreground="#006000",
                            font=("Consolas", 9, "bold"))

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def log_line(self, text, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log.insert("end", f"[{ts}] {text}\n", tag)
        self.log.see("end")

    def clear_log(self):
        self.log.delete("1.0", "end")
        for key, _ in self.checks:
            self.status_labels[key].config(text="—", foreground="gray")
            self.detail_labels[key].config(text="")
        self.summary.config(text="Ready.", foreground="black")

    def set_check(self, key, ok, detail=""):
        if ok is None:
            self.status_labels[key].config(text="SKIP", foreground="gray")
        elif ok:
            self.status_labels[key].config(text="PASS", foreground="#006000")
        else:
            self.status_labels[key].config(text="FAIL", foreground="#a00000")
        self.detail_labels[key].config(text=detail)

    # ------------------------------------------------------------------
    # Port enumeration
    # ------------------------------------------------------------------

    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        items = []
        for p in ports:
            desc = p.description or ""
            items.append(f"{p.device} — {desc}")
        if not items:
            items = ["(no serial ports detected)"]
        self.port_combo["values"] = items
        if items and not self.port_var.get():
            self.port_combo.current(0)
        self.log_line(f"Found {len(ports)} serial port(s).", "info")
        for p in ports:
            self.log_line(f"  {p.device}  {p.description}  [{p.hwid}]",
                          "info")

    def selected_port_name(self):
        sel = self.port_var.get()
        if not sel or sel.startswith("("):
            return None
        return sel.split(" — ")[0].strip()

    # ------------------------------------------------------------------
    # Threaded runners (so UI stays responsive)
    # ------------------------------------------------------------------

    def run_preflight_threaded(self):
        self.run_btn.state(["disabled"])
        self.ping_btn.state(["disabled"])
        t = threading.Thread(target=self._run_preflight_safe, daemon=True)
        t.start()

    def run_ping_threaded(self):
        self.run_btn.state(["disabled"])
        self.ping_btn.state(["disabled"])
        t = threading.Thread(target=self._run_ping_safe, daemon=True)
        t.start()

    def _run_ping_safe(self):
        try:
            self._run_ping()
        finally:
            self.root.after(0, lambda: self.run_btn.state(["!disabled"]))
            self.root.after(0, lambda: self.ping_btn.state(["!disabled"]))

    def _run_preflight_safe(self):
        try:
            self._run_preflight()
        finally:
            self.root.after(0, lambda: self.run_btn.state(["!disabled"]))
            self.root.after(0, lambda: self.ping_btn.state(["!disabled"]))

    # ------------------------------------------------------------------
    # Command helpers (wrap send_recv + logging)
    # ------------------------------------------------------------------

    def _transact(self, label, packet):
        """Send a packet, log TX/RX, return parsed (status, opcode, payload).

        Use `extract_value(payload, expected_len)` on the result to get the
        actual value bytes (handles whether or not the param byte is echoed).

        Raises on timeout, bad checksum, or non-OK status.
        """
        self.log_line(f"TX  {label:28s}  {packet.hex(' ').upper()}", "tx")
        raw = send_recv(self.port, packet)
        self.log_line(f"RX  {label:28s}  {raw.hex(' ').upper()}", "rx")
        status, opcode, payload = parse_response(raw)
        if status != 0:
            status_name = STATUS_CODES.get(status, f"0x{status:02X}")
            raise RuntimeError(f"Device returned status {status_name}")
        return status, opcode, payload

    # ------------------------------------------------------------------
    # Preflight sequence
    # ------------------------------------------------------------------

    def _run_ping(self):
        """Just open port + Ping. Does not send Connect-Request."""
        # Reset all check indicators
        for key, _ in self.checks:
            self.root.after(0, self.set_check, key, None, "")

        self.root.after(0, self.summary.config,
                        {"text": "Running ping test…", "foreground": "black"})

        port_name = self.selected_port_name()
        if not port_name:
            self.log_line("No COM port selected.", "err")
            self.root.after(0, self.set_check, "port", False, "No port selected")
            self.root.after(0, self.summary.config,
                            {"text": "FAILED: no port selected",
                             "foreground": "#a00000"})
            return

        # Step 1: open port
        try:
            self.port = serial.Serial(
                port_name, BAUD, timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
            )
            self.log_line(f"Opened {port_name} at {BAUD} 8N1", "ok")
            self.root.after(0, self.set_check, "port", True,
                            f"{port_name} @ {BAUD} 8N1")
        except Exception as e:
            self.log_line(f"Failed to open {port_name}: {e}", "err")
            self.root.after(0, self.set_check, "port", False, str(e))
            self.root.after(0, self.summary.config,
                            {"text": "FAILED to open serial port",
                             "foreground": "#a00000"})
            return

        try:
            # Step 2: Ping
            try:
                self._transact("Ping", build_packet(0x01))
                self.root.after(0, self.set_check, "ping", True, "OK")
                self.root.after(0, self.summary.config,
                                {"text": "PING OK — device is responding",
                                 "foreground": "#006000"})
            except Exception as e:
                self.log_line(f"Ping failed: {e}", "err")
                self.root.after(0, self.set_check, "ping", False, str(e))
                self.root.after(0, self.summary.config,
                                {"text": "PING FAILED — check cable / power / baud",
                                 "foreground": "#a00000"})
        finally:
            try:
                self.port.close()
                self.log_line("Port closed.", "info")
            except Exception:
                pass
            self.port = None

    def _run_preflight(self):
        # Reset all check indicators
        for key, _ in self.checks:
            self.root.after(0, self.set_check, key, None, "")

        self.root.after(0, self.summary.config,
                        {"text": "Running full preflight…",
                         "foreground": "black"})

        port_name = self.selected_port_name()
        if not port_name:
            self.log_line("No COM port selected.", "err")
            self.root.after(0, self.set_check, "port", False,
                            "No port selected")
            self.root.after(0, self.summary.config,
                            {"text": "FAILED: no port selected",
                             "foreground": "#a00000"})
            return

        # --- Step 1: open port
        try:
            self.port = serial.Serial(
                port_name, BAUD, timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
            )
            self.log_line(f"Opened {port_name} at {BAUD} 8N1", "ok")
            self.root.after(0, self.set_check, "port", True,
                            f"{port_name} @ {BAUD} 8N1")
        except Exception as e:
            self.log_line(f"Failed to open {port_name}: {e}", "err")
            self.root.after(0, self.set_check, "port", False, str(e))
            self.root.after(0, self.summary.config,
                            {"text": "FAILED to open serial port",
                             "foreground": "#a00000"})
            return

        connected = False
        any_fail = False

        try:
            # --- Step 2: Ping
            try:
                self._transact("Ping", build_packet(0x01))
                self.root.after(0, self.set_check, "ping", True, "OK")
            except Exception as e:
                any_fail = True
                self.log_line(f"Ping failed: {e}", "err")
                self.root.after(0, self.set_check, "ping", False, str(e))
                # Without Ping, don't try further
                self.root.after(0, self.summary.config,
                                {"text": "FAILED at Ping — check cable/baud/power",
                                 "foreground": "#a00000"})
                return

            # --- Step 3: Connect-Request (MUST be first write of real session)
            try:
                self._transact("Connect-Request",
                               build_packet(0x06, 0x14, 0x01))
                connected = True
                self.root.after(0, self.set_check, "connect", True, "OK")
            except Exception as e:
                any_fail = True
                self.log_line(f"Connect-Request failed: {e}", "err")
                self.root.after(0, self.set_check, "connect", False, str(e))
                self.root.after(0, self.summary.config,
                                {"text": "FAILED at Connect — device may not be PC-control enabled",
                                 "foreground": "#a00000"})
                return

            # --- Step 4: Get Software Version (opcode 0x03 = get word, param 0x00)
            try:
                _, _, payload = self._transact(
                    "Get Software Version",
                    build_packet(0x03, 0x00),
                )
                value_bytes = extract_value(payload, expected_value_len=2)
                version_hex = int.from_bytes(value_bytes, "big")
                major = (version_hex >> 8) & 0xFF
                minor = version_hex & 0xFF
                version_str = f"0x{version_hex:04X} (major={major}, minor=0x{minor:02X})"
                self.root.after(0, self.set_check, "version", True, version_str)
            except Exception as e:
                any_fail = True
                self.log_line(f"Get Software Version failed: {e}", "err")
                self.root.after(0, self.set_check, "version", False, str(e))

            # --- Step 5: Get Frequency (opcode 0x03 = get word, param 0x02)
            try:
                _, _, payload = self._transact(
                    "Get Frequency",
                    build_packet(0x03, 0x02),
                )
                value_bytes = extract_value(payload, expected_value_len=2)
                raw_val = int.from_bytes(value_bytes, "big")
                freq_hz = raw_val * 10  # spec: units of 10 Hz
                self.root.after(0, self.set_check, "frequency", True,
                                f"{freq_hz} Hz  (raw=0x{raw_val:04X})")
            except Exception as e:
                any_fail = True
                self.log_line(f"Get Frequency failed: {e}", "err")
                self.root.after(0, self.set_check, "frequency", False, str(e))

            # --- Step 6: Get Power-Level % (opcode 0x02 = get byte, param 0x04)
            try:
                _, _, payload = self._transact(
                    "Get Power-Level",
                    build_packet(0x02, 0x04),
                )
                value_bytes = extract_value(payload, expected_value_len=1)
                pct = value_bytes[0]
                self.root.after(0, self.set_check, "power", True,
                                f"{pct}%")
            except Exception as e:
                any_fail = True
                self.log_line(f"Get Power-Level failed: {e}", "err")
                self.root.after(0, self.set_check, "power", False, str(e))

            # --- Step 7: Request-Fault (opcode 0x02 = get byte, param 0x16)
            try:
                _, _, payload = self._transact(
                    "Request-Fault",
                    build_packet(0x02, 0x16),
                )
                value_bytes = extract_value(payload, expected_value_len=1)
                fault = value_bytes[0]
                fault_name = FAULT_CODES.get(fault, f"Unknown ({fault})")
                fault_ok = fault in (0, 2)  # 0 or "probe not connected" is fine idle
                self.root.after(0, self.set_check, "fault", True,
                                f"code={fault}  ({fault_name})")
                if not fault_ok:
                    self.log_line(
                        f"NOTE: fault code {fault} ({fault_name}) while idle — "
                        f"unusual, investigate.",
                        "err")
            except Exception as e:
                any_fail = True
                self.log_line(f"Request-Fault failed: {e}", "err")
                self.root.after(0, self.set_check, "fault", False, str(e))

        finally:
            # --- Step 8: Disconnect (always attempt if connected, to unlock front panel)
            if connected:
                try:
                    self._transact("Disconnect",
                                   build_packet(0x06, 0x14, 0x00))
                    self.root.after(0, self.set_check, "disconnect", True, "OK")
                except Exception as e:
                    any_fail = True
                    self.log_line(f"Disconnect failed: {e}", "err")
                    self.root.after(0, self.set_check, "disconnect",
                                    False, str(e))
            else:
                self.root.after(0, self.set_check, "disconnect", None,
                                "(skipped: never connected)")

            try:
                if self.port:
                    self.port.close()
                    self.log_line("Port closed.", "info")
            except Exception:
                pass
            self.port = None

            # Final summary
            if any_fail:
                self.root.after(0, self.summary.config,
                                {"text": "COMPLETED WITH FAILURES — see log",
                                 "foreground": "#a00000"})
            else:
                self.root.after(0, self.summary.config,
                                {"text": "ALL CHECKS PASSED — device is ready",
                                 "foreground": "#006000"})


def main():
    root = tk.Tk()
    app = PreflightGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
