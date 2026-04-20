"""
Sonaer Sonozap Ultrasonic Generator — Control GUI
==================================================

Full control GUI for operating a Sonaer ultrasonic generator (NS60K / etc.)
over RS-232. Allows setting power levels, starting/stopping, monitoring live
values, and configuring modes like AAPA, PWM, energy-limited runs, etc.

Dependencies: pyserial  (pip install pyserial)

Usage: python sonaer_control_gui.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
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
# Protocol layer (copied from preflight_gui.py)
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
    101: "Warning: more power required",
}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class ControlGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Sonaer Sonozap — Control")
        self.root.geometry("900x800")

        self.port = None  # type: serial.Serial | None
        self.connected = False
        self.monitoring = False
        self._build_ui()
        self.refresh_ports()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Top frame: port selection + connect/disconnect
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

        self.connect_btn = ttk.Button(
            top, text="Connect", command=self.connect_threaded
        )
        self.connect_btn.grid(row=0, column=3, padx=5)

        self.disconnect_btn = ttk.Button(
            top, text="Disconnect", command=self.disconnect_threaded, state="disabled"
        )
        self.disconnect_btn.grid(row=0, column=4, padx=5)

        # Control frame: power, start/stop
        control_frame = ttk.LabelFrame(
            self.root, text="Basic Controls", padding=10
        )
        control_frame.pack(fill="x", padx=10, pady=5)

        # Power level
        ttk.Label(control_frame, text="Power Level (%):").grid(row=0, column=0, sticky="w")
        self.power_var = tk.IntVar(value=40)
        self.power_scale = ttk.Scale(
            control_frame, from_=0, to=100, orient="horizontal",
            variable=self.power_var, command=self.on_power_change
        )
        self.power_scale.grid(row=0, column=1, sticky="ew", padx=5)
        self.power_label = ttk.Label(control_frame, text="40%")
        self.power_label.grid(row=0, column=2, padx=5)

        # Start/Stop
        self.start_btn = ttk.Button(
            control_frame, text="Start", command=self.start_threaded, state="disabled"
        )
        self.start_btn.grid(row=1, column=0, padx=5, pady=5)

        self.stop_btn = ttk.Button(
            control_frame, text="Stop", command=self.stop_threaded, state="disabled"
        )
        self.stop_btn.grid(row=1, column=1, padx=5, pady=5)

        # Modes frame
        modes_frame = ttk.LabelFrame(
            self.root, text="Modes", padding=10
        )
        modes_frame.pack(fill="x", padx=10, pady=5)

        # AAPA
        self.aapa_var = tk.BooleanVar()
        ttk.Checkbutton(modes_frame, text="AAPA (Auto Atomization Power Adjustment)",
                        variable=self.aapa_var, command=self.set_aapa).grid(row=0, column=0, sticky="w")

        # Constant Power
        self.constant_power_var = tk.BooleanVar()
        ttk.Checkbutton(modes_frame, text="Constant Power Mode",
                        variable=self.constant_power_var, command=self.set_constant_power).grid(row=1, column=0, sticky="w")

        # PWM
        self.pwm_var = tk.BooleanVar()
        ttk.Checkbutton(modes_frame, text="PWM",
                        variable=self.pwm_var, command=self.set_pwm).grid(row=2, column=0, sticky="w")

        # PWM controls
        self.pwm_frame = ttk.Frame(modes_frame)
        self.pwm_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20)

        ttk.Label(self.pwm_frame, text="Duty Cycle (%):").grid(row=0, column=0, sticky="w")
        self.duty_var = tk.IntVar(value=50)
        self.duty_scale = ttk.Scale(
            self.pwm_frame, from_=0, to=100, orient="horizontal",
            variable=self.duty_var, command=self.on_duty_change
        )
        self.duty_scale.grid(row=0, column=1, sticky="ew", padx=5)
        self.duty_label = ttk.Label(self.pwm_frame, text="50%")
        self.duty_label.grid(row=0, column=2, padx=5)

        ttk.Label(self.pwm_frame, text="Period (s):").grid(row=1, column=0, sticky="w")
        self.period_var = tk.IntVar(value=2)
        self.period_scale = ttk.Scale(
            self.pwm_frame, from_=1, to=100, orient="horizontal",
            variable=self.period_var, command=self.on_period_change
        )
        self.period_scale.grid(row=1, column=1, sticky="ew", padx=5)
        self.period_label = ttk.Label(self.pwm_frame, text="2 s")
        self.period_label.grid(row=1, column=2, padx=5)

        self.pwm_frame.grid_remove()  # Hide initially

        # Energy/Time limits
        ttk.Label(modes_frame, text="Energy Limit (J):").grid(row=4, column=0, sticky="w")
        self.energy_var = tk.IntVar(value=25)
        self.energy_entry = ttk.Entry(modes_frame, textvariable=self.energy_var, width=10)
        self.energy_entry.grid(row=4, column=1, padx=5)

        self.energy_state_var = tk.BooleanVar()
        ttk.Checkbutton(modes_frame, text="Enable Energy Limit",
                        variable=self.energy_state_var, command=self.set_energy_state).grid(row=5, column=0, sticky="w")

        ttk.Label(modes_frame, text="Time Limit (s):").grid(row=6, column=0, sticky="w")
        self.time_var = tk.IntVar(value=60)
        self.time_entry = ttk.Entry(modes_frame, textvariable=self.time_var, width=10)
        self.time_entry.grid(row=6, column=1, padx=5)

        self.time_state_var = tk.BooleanVar()
        ttk.Checkbutton(modes_frame, text="Enable Time Limit",
                        variable=self.time_state_var, command=self.set_time_state).grid(row=7, column=0, sticky="w")

        # Status frame: displays
        status_frame = ttk.LabelFrame(
            self.root, text="Status", padding=10
        )
        status_frame.pack(fill="x", padx=10, pady=5)

        self.status_labels = {}

        labels = [
            ("system_state", "System State:"),
            ("power_live", "Live Power (mW):"),
            ("frequency", "Frequency (Hz):"),
            ("fault", "Fault:"),
            ("energy_cnt", "Energy Remaining (J):"),
            ("time_cnt", "Time Remaining (s):"),
        ]

        for i, (key, text) in enumerate(labels):
            ttk.Label(status_frame, text=text).grid(row=i, column=0, sticky="w", pady=2)
            label = ttk.Label(status_frame, text="—")
            label.grid(row=i, column=1, sticky="w", padx=10)
            self.status_labels[key] = label

        ttk.Button(status_frame, text="Refresh Status", command=self.refresh_status_threaded).grid(
            row=len(labels), column=0, columnspan=2, pady=10
        )

        # Log frame
        log_frame = ttk.LabelFrame(
            self.root, text="Log", padding=5
        )
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log = scrolledtext.ScrolledText(
            log_frame, wrap="word", height=10,
            font=("Consolas", 9)
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("tx", foreground="#0050a0")
        self.log.tag_config("rx", foreground="#006000")
        self.log.tag_config("err", foreground="#a00000")
        self.log.tag_config("info", foreground="#606060")
        self.log.tag_config("ok", foreground="#006000",
                            font=("Consolas", 9, "bold"))

        # Disable controls initially
        self.set_connected_state(False)

    def set_connected_state(self, connected):
        self.connected = connected
        state = "normal" if connected else "disabled"
        self.start_btn.config(state=state)
        self.stop_btn.config(state=state)
        self.power_scale.config(state=state)
        for child in self.pwm_frame.winfo_children():
            child.config(state=state)
        self.energy_entry.config(state=state)
        self.time_entry.config(state=state)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_power_change(self, value):
        pct = int(float(value))
        self.power_label.config(text=f"{pct}%")
        if self.connected:
            self.set_power_level(pct)

    def on_duty_change(self, value):
        pct = int(float(value))
        self.duty_label.config(text=f"{pct}%")
        if self.connected and self.pwm_var.get():
            self.set_pwm_duty(pct)

    def on_period_change(self, value):
        sec = int(float(value))
        self.period_label.config(text=f"{sec} s")
        if self.connected and self.pwm_var.get():
            self.set_pwm_period(sec)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_line(self, text, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log.insert("end", f"[{ts}] {text}\n", tag)
        self.log.see("end")

    # ------------------------------------------------------------------
    # Port management
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

    def selected_port_name(self):
        sel = self.port_var.get()
        if not sel or sel.startswith("("):
            return None
        return sel.split(" — ")[0].strip()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_threaded(self):
        self.connect_btn.config(state="disabled")
        t = threading.Thread(target=self._connect_safe, daemon=True)
        t.start()

    def _connect_safe(self):
        try:
            self._connect()
        finally:
            self.root.after(0, lambda: self.connect_btn.config(state="normal"))

    def _connect(self):
        port_name = self.selected_port_name()
        if not port_name:
            messagebox.showerror("Error", "No COM port selected.")
            return

        try:
            self.port = serial.Serial(
                port_name, BAUD, timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
            )
            self.log_line(f"Opened {port_name} at {BAUD} 8N1", "ok")
        except Exception as e:
            self.log_line(f"Failed to open {port_name}: {e}", "err")
            messagebox.showerror("Error", f"Failed to open port: {e}")
            return

        try:
            # Ping
            self._transact("Ping", build_packet(0x01))
            # Connect
            self._transact("Connect-Request", build_packet(0x06, 0x14, 0x01))
            self.connected = True
            self.root.after(0, self.set_connected_state, True)
            self.root.after(0, lambda: self.connect_btn.config(state="disabled"))
            self.root.after(0, lambda: self.disconnect_btn.config(state="normal"))
            self.log_line("Connected to device", "ok")
            self.refresh_status()
        except Exception as e:
            self.log_line(f"Connection failed: {e}", "err")
            messagebox.showerror("Error", f"Connection failed: {e}")
            try:
                self.port.close()
            except:
                pass
            self.port = None

    def disconnect_threaded(self):
        self.disconnect_btn.config(state="disabled")
        t = threading.Thread(target=self._disconnect_safe, daemon=True)
        t.start()

    def _disconnect_safe(self):
        try:
            self._disconnect()
        finally:
            self.root.after(0, lambda: self.disconnect_btn.config(state="normal"))

    def _disconnect(self):
        if not self.port:
            return
        try:
            self._transact("Disconnect", build_packet(0x06, 0x14, 0x00))
            self.log_line("Disconnected from device", "ok")
        except Exception as e:
            self.log_line(f"Disconnect failed: {e}", "err")
        finally:
            try:
                self.port.close()
            except:
                pass
            self.port = None
            self.connected = False
            self.root.after(0, self.set_connected_state, False)
            self.root.after(0, lambda: self.connect_btn.config(state="normal"))
            self.root.after(0, lambda: self.disconnect_btn.config(state="disabled"))

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _transact(self, label, packet):
        self.log_line(f"TX  {label:28s}  {packet.hex(' ').upper()}", "tx")
        raw = send_recv(self.port, packet)
        self.log_line(f"RX  {label:28s}  {raw.hex(' ').upper()}", "rx")
        status, opcode, payload = parse_response(raw)
        if status != 0:
            status_name = STATUS_CODES.get(status, f"0x{status:02X}")
            raise RuntimeError(f"Device returned status {status_name}")
        return status, opcode, payload

    def set_power_level(self, pct):
        try:
            self._transact(f"Set Power-Level {pct}%", build_packet(0x06, 0x15, pct))
        except Exception as e:
            self.log_line(f"Set power failed: {e}", "err")

    def start_threaded(self):
        t = threading.Thread(target=lambda: self._set_system_state(2), daemon=True)
        t.start()

    def stop_threaded(self):
        t = threading.Thread(target=lambda: self._set_system_state(1), daemon=True)
        t.start()

    def _set_system_state(self, state):
        try:
            self._transact(f"Set System-State {state}", build_packet(0x06, 0x01, state))
            self.refresh_status()
        except Exception as e:
            self.log_line(f"Set system state failed: {e}", "err")

    def set_aapa(self):
        val = 1 if self.aapa_var.get() else 0
        try:
            self._transact(f"Set AAPA {val}", build_packet(0x06, 0x19, val))
        except Exception as e:
            self.log_line(f"Set AAPA failed: {e}", "err")

    def set_constant_power(self):
        val = 1 if self.constant_power_var.get() else 0
        try:
            self._transact(f"Set Constant Power {val}", build_packet(0x06, 0x1C, val))
        except Exception as e:
            self.log_line(f"Set Constant Power failed: {e}", "err")

    def set_pwm(self):
        val = 1 if self.pwm_var.get() else 0
        try:
            self._transact(f"Set PWM {val}", build_packet(0x06, 0x08, val))
            if val:
                self.root.after(0, self.pwm_frame.grid)
                self.set_pwm_duty(self.duty_var.get())
                self.set_pwm_period(self.period_var.get())
            else:
                self.root.after(0, self.pwm_frame.grid_remove)
        except Exception as e:
            self.log_line(f"Set PWM failed: {e}", "err")

    def set_pwm_duty(self, pct):
        try:
            self._transact(f"Set PWM Duty {pct}%", build_packet(0x06, 0x09, pct))
        except Exception as e:
            self.log_line(f"Set PWM duty failed: {e}", "err")

    def set_pwm_period(self, sec):
        try:
            self._transact(f"Set PWM Period {sec}s", build_packet(0x06, 0x0A, sec))
        except Exception as e:
            self.log_line(f"Set PWM period failed: {e}", "err")

    def set_energy_state(self):
        val = 1 if self.energy_state_var.get() else 0
        try:
            self._transact(f"Set Energy-State {val}", build_packet(0x06, 0x0B, val))
            if val:
                energy = self.energy_var.get()
                self.set_energy_run(energy)
        except Exception as e:
            self.log_line(f"Set Energy-State failed: {e}", "err")

    def set_energy_run(self, joules):
        try:
            # Word in big-endian
            data = joules.to_bytes(2, "big")
            self._transact(f"Set Energy-Run {joules}J", build_packet(0x07, 0x0D, *data))
        except Exception as e:
            self.log_line(f"Set Energy-Run failed: {e}", "err")

    def set_time_state(self):
        val = 1 if self.time_state_var.get() else 0
        try:
            self._transact(f"Set Time-State {val}", build_packet(0x06, 0x0E, val))
            if val:
                time_s = self.time_var.get()
                self.set_time_run(time_s)
        except Exception as e:
            self.log_line(f"Set Time-State failed: {e}", "err")

    def set_time_run(self, seconds):
        try:
            # Word in big-endian
            data = seconds.to_bytes(2, "big")
            self._transact(f"Set Time-Run {seconds}s", build_packet(0x07, 0x10, *data))
        except Exception as e:
            self.log_line(f"Set Time-Run failed: {e}", "err")

    def refresh_status_threaded(self):
        t = threading.Thread(target=self.refresh_status, daemon=True)
        t.start()

    def refresh_status(self):
        if not self.connected:
            return
        try:
            # System-State
            _, _, payload = self._transact("Get System-State", build_packet(0x02, 0x01))
            value_bytes = extract_value(payload, 1)
            state = value_bytes[0]
            state_str = "Running" if state == 2 else "Stopped"
            self.root.after(0, self.status_labels["system_state"].config, {"text": state_str})

            # Power live
            _, _, payload = self._transact("Get Power (live)", build_packet(0x04, 0x03))
            value_bytes = extract_value(payload, 4)
            power_mw = int.from_bytes(value_bytes, "big")
            self.root.after(0, self.status_labels["power_live"].config, {"text": f"{power_mw} mW"})

            # Frequency
            _, _, payload = self._transact("Get Frequency", build_packet(0x03, 0x02))
            value_bytes = extract_value(payload, 2)
            raw_val = int.from_bytes(value_bytes, "big")
            freq_hz = raw_val * 10
            self.root.after(0, self.status_labels["frequency"].config, {"text": f"{freq_hz} Hz"})

            # Fault
            _, _, payload = self._transact("Request-Fault", build_packet(0x02, 0x16))
            value_bytes = extract_value(payload, 1)
            fault = value_bytes[0]
            fault_name = FAULT_CODES.get(fault, f"Unknown ({fault})")
            self.root.after(0, self.status_labels["fault"].config, {"text": f"{fault} ({fault_name})"})

            # Energy-Cnt
            _, _, payload = self._transact("Get Energy-Cnt", build_packet(0x03, 0x0C))
            value_bytes = extract_value(payload, 2)
            energy = int.from_bytes(value_bytes, "big")
            self.root.after(0, self.status_labels["energy_cnt"].config, {"text": f"{energy} J"})

            # Time-Cnt
            _, _, payload = self._transact("Get Time-Cnt", build_packet(0x03, 0x0F))
            value_bytes = extract_value(payload, 2)
            time_s = int.from_bytes(value_bytes, "big")
            self.root.after(0, self.status_labels["time_cnt"].config, {"text": f"{time_s} s"})

        except Exception as e:
            self.log_line(f"Refresh status failed: {e}", "err")


def main():
    root = tk.Tk()
    app = ControlGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()