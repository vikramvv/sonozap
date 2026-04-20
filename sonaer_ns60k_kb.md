# Sonaer NS60K + Sonozap Ultrasonic Generator — Control KB

**Setup context:** Sonaer NS60K narrow-spray nozzle (60 kHz), feed via DWK pressure pot directly into the Sonaer generator liquid inlet, Sonozap generator controlled over RS-232 from PC. Target application: Cu-GNP suspensions in acetone for EPD/spray-coating precursor work.

**Protocol reference:** Sonaer Ultrasonic Device Interface Protocol Specification, Rev F (1.5), April 2024.

---

## 1. Hardware & Serial Configuration

| Parameter | Value |
|---|---|
| Interface | RS-232 (DE-9), straight cable or USB-serial adapter |
| Baud rate | 38,400 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| Endianness | Big-endian (MSB first) for WORD/DWORD values |
| Transaction | Host-initiated, every command has a response, <20 ms turnaround |
| Inter-character delay | Not required |

**USB-serial notes:** FTDI FT232 and Prolific PL2303 chipsets both work. Avoid unbranded CH340 if you can — they sometimes introduce framing errors at 38,400 under Windows. Check Device Manager → Ports (COM & LPT) to find the assigned COM number.

---

## 2. Pre-flight: Verify the Connection from PowerShell

Before writing any Python, confirm the OS sees the device and that the serial port opens cleanly. Run these in a PowerShell terminal.

### 2.1 List available COM ports

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

Or with more detail (finds description strings, useful when multiple USB-serial adapters are plugged in):

```powershell
Get-WmiObject Win32_SerialPort | Select-Object Name, DeviceID, Description, PNPDeviceID | Format-Table -AutoSize
```

Alternative using CIM (modern, preferred on Win10+):

```powershell
Get-CimInstance -ClassName Win32_SerialPort | Select-Object Name, DeviceID, Description | Format-Table -AutoSize
```

Note the COM port (e.g., `COM4`) for the Sonaer generator. If the device isn't listed, the USB-serial driver isn't installed or the cable isn't seated.

### 2.2 Open the port and send a Ping

The Ping command (`02 01 FF`) is the safest round-trip test — it doesn't require Connect-Request and doesn't change generator state.

```powershell
# Edit $portName to match your device
$portName = "COM4"

$port = New-Object System.IO.Ports.SerialPort $portName, 38400, None, 8, One
$port.ReadTimeout  = 500
$port.WriteTimeout = 500
$port.Open()

# Ping: Length=02, Opcode=01, Checksum=FF
$cmd = [byte[]](0x02, 0x01, 0xFF)
$port.Write($cmd, 0, $cmd.Length)

Start-Sleep -Milliseconds 50

$buf = New-Object byte[] 16
$n = $port.Read($buf, 0, $buf.Length)
Write-Host ("Received {0} bytes: {1}" -f $n, (($buf[0..($n-1)] | ForEach-Object { '{0:X2}' -f $_ }) -join ' '))

$port.Close()
$port.Dispose()
```

**Expected response:** `03 00 01 FF` (Length=3, Status=OK, Opcode=Ping, Checksum).

If you get nothing back → wrong COM port, wrong baud, cable problem, or generator powered off / still booting.
If you get garbage → baud/parity mismatch or a bad USB-serial adapter.
If you get a non-standard response like `03 00 00 00` → device is not PC-Control enabled, contact Sonaer for firmware unlock.

### 2.3 Try a real handshake (Connect-Request + Get Software Version)

Once Ping works, confirm full protocol compliance:

```powershell
$portName = "COM4"
$port = New-Object System.IO.Ports.SerialPort $portName, 38400, None, 8, One
$port.ReadTimeout  = 500
$port.WriteTimeout = 500
$port.Open()

function Send-Cmd($port, $bytes, $label) {
    $port.Write($bytes, 0, $bytes.Length)
    Start-Sleep -Milliseconds 50
    $buf = New-Object byte[] 32
    $n = $port.Read($buf, 0, $buf.Length)
    $hex = ($buf[0..($n-1)] | ForEach-Object { '{0:X2}' -f $_ }) -join ' '
    Write-Host ("{0,-25} -> {1}" -f $label, $hex)
}

Send-Cmd $port ([byte[]](0x04, 0x06, 0x14, 0x01, 0xE5)) "Connect-Request"
Send-Cmd $port ([byte[]](0x03, 0x03, 0x00, 0xFD))       "Get Software Version"
Send-Cmd $port ([byte[]](0x03, 0x03, 0x02, 0xFB))       "Get Frequency"
Send-Cmd $port ([byte[]](0x03, 0x02, 0x16, 0xE8))       "Request-Fault"
Send-Cmd $port ([byte[]](0x04, 0x06, 0x14, 0x00, 0xE6)) "Disconnect"

$port.Close()
$port.Dispose()
```

**Expected:** Connect returns `03 00 06 FA`. Software Version returns 6 bytes, last two (before checksum) are the version (e.g., `03 14` = v3.20). Frequency returns a word in 10-Hz units — for a healthy NS60K sitting idle, expect something close to 6000 (= 60,000 Hz = 0x1770). Fault should be 00 with nothing connected to the probe, or possibly 02 (probe not connected) depending on cabling state.

**Always disconnect at the end** — without it the front panel stays locked and the next session may fail to reconnect.

---

## 3. Protocol Primer

### 3.1 Packet format

**Command:**
```
[Length] [Opcode] [Parameter?] [Data...] [Checksum]
```
- `Length` = byte count from Opcode through Checksum (i.e., total packet length − 1)
- `Checksum` = two's complement of the sum of all bytes between Length and Checksum. Equivalent to: `(-sum) & 0xFF`. Sum of Opcode + Parameter + Data + Checksum should equal 0 mod 256.

**Response:**
```
[Length] [Status] [Opcode] [Data...] [Checksum]
```
- `Status` = 0x00 on success; see status code table below.

### 3.2 Opcodes

| Opcode | Meaning |
|---|---|
| 0x01 | Ping |
| 0x02 | Get byte |
| 0x03 | Get word (2 bytes) |
| 0x04 | Get dword (4 bytes) |
| 0x06 | Set byte |
| 0x07 | Set word |
| 0x08 | Set dword |

### 3.3 Response status codes

| Code | Meaning |
|---|---|
| 0x00 | OK |
| 0x11 | Unknown opcode |
| 0x12 | Unknown parameter |
| 0x13 | Invalid value |
| 0x40 | General comms error |
| 0x41 | Device command timeout |
| 0x42 | Length incorrect |
| 0x43 | Checksum failed |

On error, retry the command.

---

## 4. Complete Parameter Reference

| Parameter | ID | Type | Range | R/W | Purpose |
|---|---|---|---|---|---|
| Software Version | 0x00 | Word | 0x0000–0x9999 | R | Firmware version (0x0314 = 3.20) |
| System-State | 0x01 | Byte | 1=Stop, 2=Run | R/W | Primary run control |
| Frequency | 0x02 | Word | 0–60000 | R | Actual operating freq, units of 10 Hz |
| Power (live) | 0x03 | DWord | 0–9,999,999 | R | Live power in milliwatts |
| Get Power-Level | 0x04 | Byte | 0–100 | R | Current power setpoint (%) |
| Power Units (display) | 0x06 | Byte | 0=W, 1=J/s, 2=dBm | R/W | Front panel display units |
| Power Decimal Places | 0x07 | Byte | 0–3 | R/W | Decimals on power display |
| PWM State | 0x08 | Byte | 0=off, 1=on | R/W | Enable generator PWM |
| PWM Duty Cycle | 0x09 | Byte | 0–100 | R/W | PWM duty (%) |
| PWM Period | 0x0A | Byte | 1–100 | R/W | PWM period (seconds) |
| Energy-State | 0x0B | Byte | 0=off, 1=on | R/W | Enable energy-limited shutoff |
| Energy-Cnt | 0x0C | Word | 0–10000 | R | Remaining energy (J) |
| Energy-Run | 0x0D | Word | 0–10000 | R/W | Energy target (J) |
| Time-State | 0x0E | Byte | 0=off, 1=on | R/W | Enable time-limited shutoff |
| Time-Cnt | 0x0F | Word | 0–39000 | R | Remaining time (s) |
| Time-Run | 0x10 | Word | 0–39000 | R/W | Time target (s, ≤11 hr) |
| Contrast | 0x12 | Byte | 1–12 | R/W | LCD brightness |
| PC Controls Power | 0x13 | Byte | 0 or 1 | R/W | Allow PC to change power mid-PWM |
| Connect-Request | 0x14 | Byte | 0=disc, 1=conn | W | Must be first command; locks front panel |
| Set Power-Level | 0x15 | Byte | 0–100 | W | Set power setpoint (%) |
| Request-Fault | 0x16 | Byte | 0–255 | R | Fault/warning status |
| Standard / Turbo | 0x18 | Byte | 0=std, 1=turbo | R/W | Drive mode (Turbo requires unlock) |
| AAPA Mode | 0x19 | Byte | 0=off, 1=on | R/W | Auto Atomization Power Adjustment |
| Drop Size Simulator | 0x1B | Byte | 0 or 1 | R/W | Display visualization only (no process effect) |
| Constant Power Mode | 0x1C | Byte | 0=off, 1=on | R/W | Hold output power steady under load change |

### 4.1 Fault codes (from Request-Fault / 0x16)

| Code | Meaning | Typical Cu-GNP / acetone cause |
|---|---|---|
| 0 | No fault / Normal | — |
| 1 | Current overload | Short in probe cable, or extreme over-loading |
| 2 | Probe not connected | Cable unseated, or probe failure |
| 3 | Incorrect freq / excessive load | Tip flooded, wrong probe for generator, acoustic mismatch |
| 4 | Internal error | Cycle generator power |
| 5 | Under-voltage | Mains supply issue |
| 6 | Line voltage | Mains supply issue |
| 101 | Warning: more power required | Liquid load exceeds current power setting — increase power or reduce feed |

### 4.2 Mutual exclusions and ordering rules

- **Connect-Request MUST be first.** Nothing else works before it.
- **AAPA and Constant Power Mode are mutually exclusive.** Enabling one forces the other off.
- **Turbo requires an unlock code** entered on the device front panel; Sonaer issues these per unit.
- **Always send Disconnect (0x14 = 0)** before closing the app or front panel may remain locked.
- Power on the generator, let it finish boot, **then** start serial comms.

---

## 5. Operating Notes for NS60K + DWK Pressure Pot Feed

### 5.1 Why this setup drips (and how the generator helps)

Dripping on a 60 kHz narrow-spray nozzle is a **feed-rate vs atomization-capacity mismatch**. The DWK + regulator gives you a steady liquid feed; the Sonaer gives you atomization throughput. When feed > atomization, acetone + GNP accumulates at the tip and sheds as drips instead of aerosolizing.

Because the DWK is a pressurized reservoir (not a pulsed valve), you have two control knobs:

1. **DWK regulator pressure** — sets the steady feed rate.
2. **Generator power & PWM** — sets atomization capacity.

You're matching these two. The generator has more direct knobs (power, PWM) than the DWK (pressure only, no valve pulsing in your current config), so start with a conservative DWK pressure and sweep generator parameters.

### 5.2 Recommended initial settings for NS60K with acetone-based GNP suspension

**Starting point (before any optimization):**

| Setting | Value | Rationale |
|---|---|---|
| DWK pressure | Low end of regulator (~2–5 psi for acetone) | Acetone has low viscosity; easy to flood |
| Power-Level | 35–40% | NS60K operates well mid-range; leaves AAPA headroom |
| AAPA | Enabled | Compensates for GNP concentration drift and transients |
| Constant Power | Disabled | Mutually exclusive with AAPA; use AAPA for exploration |
| Standard/Turbo | Standard | Preserves narrow cone geometry |
| PWM | Disabled initially | Add only if you need lower average deposition rate |

**Poll for faults every 1 s while running.** Fault code 101 ("more power required") is the canonical "you are flooding the tip" signal — either bump power or drop DWK pressure.

### 5.3 PWM strategy for thin-film deposition

When the minimum DWK pressure still over-feeds the tip, use generator PWM to reduce average atomization time:

- **Period = 2 s, Duty = 50%** → 1 s on, 1 s off. Each on-burst runs at full power (no drip regime). Off-time lets residual liquid finish atomizing and tip re-wet before the next burst.
- For very thin layers: Period = 5 s, Duty = 20%.
- Enable **PC Controls Power (0x13)** if you want to modulate power during PWM cycles from the PC.

During PWM off-phases, liquid continues feeding from the DWK. If you see accumulation during off-phase, shorten the period or further reduce DWK pressure. The cleanest solution is adding a solenoid valve between the DWK and the Sonaer inlet synced to the PWM on-phase, but that is beyond the current config.

### 5.4 Reproducibility mode: Energy-limited deposition

Once you have found a working (power, PWM, DWK pressure) point, use **Energy-State** to deposit a fixed total energy per substrate. This is more reproducible than time-limited runs when AAPA is active, because AAPA varies instantaneous power — energy integrates over that variation.

Sequence:
1. Connect
2. Set Power-Level
3. Enable AAPA (or Constant Power for strict reproducibility)
4. Set Energy-Run = e.g., 25 J
5. Enable Energy-State
6. Start (System-State = 2)
7. Poll Energy-Cnt until it reaches 0, or poll System-State until it goes back to 1
8. Stop, Disconnect

### 5.5 Dripping-characterization sweep matrix

To map your no-drip operating window for a specific GNP suspension:

- **Fix DWK pressure** at a conservative value.
- **Sweep Power-Level:** 25, 30, 35, 40, 45, 50% (six steps).
- At each power, run for ~30 s, then:
  - Log live Power (0x03) — watch for oscillation (AAPA hunting) or steady drift
  - Log Frequency (0x02) — should stay near 6000 (×10 Hz); drift away from 60 kHz indicates detuning under load
  - Log Fault (0x16) — any non-zero code, especially 3 (excessive load) or 101 (more power)
  - Visual: dry tip between observations? spray cone geometry? drips at tip, drips at rim, or satellites?

Then bump DWK pressure one step and repeat. The **no-drip window** is the contiguous region where fault stays 0, live power is stable (±10% of setpoint), and visual inspection shows a dry tip + stable cone.

**For Cu-GNP specifically:** GNPs tend to settle in the DWK over minutes, so either recirculate/stir the pot between sweeps or limit each sweep to <10 min of continuous feed to keep concentration consistent.

---

## 6. Python Implementation Notes

When you build the Python driver, these are the gotchas that matter.

### 6.1 Library choice

Use `pyserial`:
```
pip install pyserial
```

### 6.2 Packet builder

```python
def build_packet(opcode: int, *data: int) -> bytes:
    """Build a Sonaer command packet with correct length and checksum."""
    body = bytes([opcode, *data])
    length = len(body) + 1  # +1 for checksum byte
    checksum = (-sum(body)) & 0xFF
    return bytes([length, *body, checksum])
```

### 6.3 Response parser

Response format: `[Length] [Status] [Opcode] [Data...] [Checksum]`. Read the first byte (Length), then read Length more bytes. Verify checksum: sum of Status + Opcode + Data + Checksum should be 0 mod 256.

```python
def read_response(port) -> tuple[int, int, bytes]:
    """Returns (status, opcode, data_bytes). Raises on timeout or checksum fail."""
    length_byte = port.read(1)
    if not length_byte:
        raise TimeoutError("No response from device")
    length = length_byte[0]
    rest = port.read(length)
    if len(rest) != length:
        raise TimeoutError(f"Short response: got {len(rest)}/{length}")
    if (sum(rest) & 0xFF) != 0:
        raise ValueError(f"Bad checksum in response: {rest.hex()}")
    status = rest[0]
    opcode = rest[1]
    data = rest[2:-1]  # everything between opcode and checksum
    return status, opcode, data
```

### 6.4 Endianness

Word and Dword values are **big-endian**. Use `int.from_bytes(data, "big")` to parse, `value.to_bytes(n, "big")` to serialize.

### 6.5 Timing

- Sleep 30–50 ms between write and read (spec says <20 ms, but give margin for USB-serial latency).
- Between successive commands, no extra delay needed — back-to-back is fine.
- Use a 500 ms read timeout to catch dead ports quickly.

### 6.6 Session lifecycle — always wrap in try/finally

```python
port = serial.Serial("COM4", 38400, timeout=0.5)
try:
    connect(port)
    # ... your run logic ...
finally:
    try:
        disconnect(port)
    finally:
        port.close()
```

Skipping Disconnect is the #1 way to end up with a locked front panel on the next session.

### 6.7 Fault polling

Poll Request-Fault (0x16) at **1 Hz** during a run in a separate thread or async task. Treat code 101 as a warning (log it, maybe auto-bump power). Treat codes 1, 3, 4, 5, 6 as errors — stop the probe immediately (`System-State = 1`) and surface to the user.

### 6.8 Integration with your Sensirion SLF3S flow logging

If you add the SLF3S downstream of the DWK later, log flow rate alongside Sonaer live Power (0x03) and Fault (0x16) on the same 1 Hz timebase. The product `flow_rate × atomization_efficiency` is what determines drip onset — having both lets you compute capacity margin in real time.

---

## 7. Quick Reference: Constructed Command Bytes

All values hex. Copy-paste ready.

| Action | Bytes |
|---|---|
| Ping | `02 01 FF` |
| Connect | `04 06 14 01 E5` |
| Disconnect | `04 06 14 00 E6` |
| Get Software Version | `03 03 00 FD` |
| Get Frequency | `03 03 02 FB` |
| Get Power (mW, live) | `03 04 03 F9` |
| Get Power-Level (%) | `03 02 04 FA` |
| Set Power-Level 40% | `04 06 15 28 BD` |
| Set Power-Level 50% | `04 06 15 32 B3` |
| Set Power-Level 65% | `04 06 15 41 A4` |
| Start (Run) | `04 06 01 02 F7` |
| Stop | `04 06 01 01 F8` |
| Request-Fault | `03 02 16 E8` |
| Enable AAPA | `04 06 19 01 E0` |
| Disable AAPA | `04 06 19 00 E1` |
| Enable Constant Power | `04 06 1C 01 DD` |
| Set Standard mode | `04 06 18 00 E2` |
| Set Turbo mode | `04 06 18 01 E1` |
| Enable PWM | `04 06 08 01 F1` |
| Disable PWM | `04 06 08 00 F2` |
| Set PWM Period 2 s | `04 06 0A 02 EE` |
| Set PWM Duty 50% | `04 06 09 32 BF` |
| Enable Energy-State | `04 06 0B 01 EE` |
| Set Energy-Run 25 J | `05 07 0D 00 19 D3` |

To compute a new checksum for any variant: `cs = (-(opcode + parameter + data_bytes)) & 0xFF`.

---

## 8. References

- Sonaer Ultrasonic Device Interface Protocol Specification, Rev F (1.5), 4/23/2024.
- Sonozap Ultrasonic Generator Control PC Application, v1.5.
- Atomizer firmware 3.14+.
