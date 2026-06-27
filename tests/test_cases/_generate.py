"""Generate and validate the PCB test-case fixture JSON files.

Run once to (re)generate all test_cases/*.json files.
Usage: python _generate.py
"""

from __future__ import annotations
import json
import sys
import pathlib

OUT = pathlib.Path(__file__).parent

# ── helpers ────────────────────────────────────────────────────────────────

def c(ref, typ, value, package, purpose, fg=None, specs=None):
    d = {"ref": ref, "type": typ, "value": value, "package": package, "purpose": purpose}
    if fg:
        d["functional_group"] = fg
    if specs:
        d["specs"] = specs
    return d

def net(name, cls, pins):
    return {"net_name": name, "net_class": cls, "pins": pins}

def sig(name, pins):
    return net(name, "signal", pins)

def pwr(name, pins):
    return net(name, "power", pins)

def gnd(pins):
    return net("GND", "ground", pins)

def hint(ref, **kw):
    return {"ref": ref, **kw}

def validate(data):
    seen = {}
    errors = []
    for conn in data["connections"]:
        for p in conn["pins"]:
            if p in seen:
                errors.append(f"DUPLICATE pin {p!r} in nets {seen[p]!r} and {conn['net_name']!r}")
            seen[p] = conn["net_name"]
    if errors:
        raise ValueError("\n".join(errors))
    # Ensure each net has ≥2 pins
    for conn in data["connections"]:
        if len(conn["pins"]) < 2:
            raise ValueError(f"Net {conn['net_name']!r} has fewer than 2 pins")

def save(data, name):
    validate(data)
    path = OUT / name
    path.write_text(json.dumps(data, indent=2))
    ncomp = len(data["components"])
    nnets = len(data["connections"])
    layers = data.get("board", {}).get("layers", "?")
    print(f"  {name}  ({ncomp} components, {nnets} nets, {layers}L) — OK")


# ════════════════════════════════════════════════════════════════════════════
# TC1 — 2L minimal  (555 timer + 3 LEDs, regression baseline)
# ════════════════════════════════════════════════════════════════════════════

tc01 = {
    "project_name": "tc01_2l_minimal",
    "description": "NE555 astable multivibrator blinks three red LEDs at ~1.3 Hz from 5 V DC power — smallest possible regression baseline",
    "power": {"voltage": "5V", "source": "DC power jack"},
    "board": {"width_mm": 50.0, "height_mm": 35.0, "layers": 2, "corner_radius_mm": 1.0, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        c("J1",  "connector", "DC power jack 5V",     "DC_Jack_2.1x5.5", "5 V power input",              "power"),
        c("U1",  "ic",        "NE555",                "DIP8",             "Astable oscillator ~1.3 Hz",    "timer"),
        c("R1",  "resistor",  "10kohm",               "0805",             "Timing Ra (VCC→DISCH)",         "timer"),
        c("R2",  "resistor",  "100kohm",              "0805",             "Timing Rb (DISCH→TIMING node)", "timer"),
        c("R3",  "resistor",  "10kohm",               "0805",             "RESET pull-up to VCC",          "timer"),
        c("C1",  "capacitor", "10uF",                 "1206",             "Timing capacitor to GND",       "timer"),
        c("C2",  "capacitor", "100nF",                "0805",             "VCC bypass near U1",            "timer"),
        c("C3",  "capacitor", "10nF",                 "0805",             "CTRL pin noise suppression",    "timer"),
        c("R4",  "resistor",  "150ohm",               "0805",             "Current limit for D1",          "leds"),
        c("R5",  "resistor",  "150ohm",               "0805",             "Current limit for D2",          "leds"),
        c("R6",  "resistor",  "150ohm",               "0805",             "Current limit for D3",          "leds"),
        c("D1",  "led",       "red",                  "0805_LED",         "Blinking LED 1",                "leds"),
        c("D2",  "led",       "red",                  "0805_LED",         "Blinking LED 2",                "leds"),
        c("D3",  "led",       "red",                  "0805_LED",         "Blinking LED 3",                "leds"),
    ],
    "connections": [
        pwr("VCC",      ["J1.1", "U1.VCC", "R1.1", "R3.1", "C2.1"]),
        gnd(            ["J1.2", "U1.GND", "C1.2", "C2.2", "C3.2", "D1.cathode", "D2.cathode", "D3.cathode"]),
        sig("RESET",    ["U1.RESET", "R3.2"]),
        sig("DISCH",    ["U1.DISCH", "R1.2", "R2.1"]),
        sig("TIMING",   ["U1.TRIG", "U1.THRESH", "R2.2", "C1.1"]),
        sig("CTRL",     ["U1.CTRL", "C3.1"]),
        sig("LED_DRV",  ["U1.OUT", "R4.1", "R5.1", "R6.1"]),
        sig("D1_AN",    ["R4.2", "D1.anode"]),
        sig("D2_AN",    ["R5.2", "D2.anode"]),
        sig("D3_AN",    ["R6.2", "D3.anode"]),
    ],
    "placement_hints": [
        hint("J1", edge="left"),
        hint("U1", x_mm=25.0, y_mm=17.0),
        hint("D1", edge="right"),
        hint("D2", edge="right", near="D1"),
        hint("D3", edge="right", near="D2"),
    ],
    "attachments": [],
}


# ════════════════════════════════════════════════════════════════════════════
# TC2 — 2L moderate  (ATtiny85 I2C sensor module + LDO + ISP)
# ════════════════════════════════════════════════════════════════════════════

tc02 = {
    "project_name": "tc02_2l_moderate",
    "description": "ATtiny85 I2C sensor-node module: 3.3 V LDO, I2C header, ISP programming port, status LEDs, reset button — exercises mixed SMD/TH, functional-group affinity, and a 20-component 2-layer layout",
    "power": {"voltage": "3.3V", "source": "2-pin JST power connector"},
    "board": {"width_mm": 62.0, "height_mm": 46.0, "layers": 2, "corner_radius_mm": 1.0, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        # Power input + regulation
        c("J1",  "connector", "2-pin JST-PH",         "Connector_JST_PH_S2B",     "3.3–5 V power input",          "power"),
        c("U2",  "ic",        "MCP1700-3302",          "SOT-23-3",                 "3.3 V 250 mA LDO regulator",   "power"),
        c("C1",  "capacitor", "100nF",                 "0805",                     "LDO input bypass",             "power"),
        c("C2",  "capacitor", "1uF",                   "1206",                     "LDO output bulk cap",          "power"),
        c("C3",  "capacitor", "100nF",                 "0805",                     "LDO output HF bypass",         "power"),
        # MCU + decoupling
        c("U1",  "ic",        "ATtiny85-20SU",         "SOIC8",                    "8-bit AVR MCU, I2C/SPI capable","mcu"),
        c("C4",  "capacitor", "100nF",                 "0805",                     "U1 VCC bypass",                "mcu"),
        c("C5",  "capacitor", "10nF",                  "0402",                     "ADC reference bypass",         "mcu"),
        # Headers
        c("J2",  "connector", "4-pin I2C header",      "Connector_PinHeader_2.54mm_1x04", "I2C + power to sensor", "i2c"),
        c("J3",  "connector", "6-pin ISP 2x3",         "Connector_PinHeader_2.54mm_2x03", "In-circuit programming","isp"),
        c("J4",  "connector", "3-pin sensor connector","Connector_PinHeader_2.54mm_1x03", "Analog sensor input",   "sensor"),
        # Pull-ups + limits
        c("R1",  "resistor",  "4.7kohm",               "0805",                     "SDA pull-up",                  "i2c"),
        c("R2",  "resistor",  "4.7kohm",               "0805",                     "SCL pull-up",                  "i2c"),
        c("R3",  "resistor",  "100ohm",                "0805",                     "Status LED current limit",      "leds"),
        c("R4",  "resistor",  "100ohm",                "0805",                     "Power LED current limit",       "leds"),
        c("R5",  "resistor",  "10kohm",                "0805",                     "RESET pull-up",                "mcu"),
        c("R6",  "resistor",  "10kohm",                "0805",                     "ADC input divider",            "sensor"),
        # LEDs + button
        c("D1",  "led",       "green",                 "0805_LED",                 "Activity / status LED",         "leds"),
        c("D2",  "led",       "red",                   "0805_LED",                 "Power-on indicator LED",        "leds"),
        c("SW1", "switch",    "push button 6mm",       "SW_Push_6mm",              "Manual RESET button",          "mcu"),
    ],
    "connections": [
        pwr("VIN",       ["J1.1", "U2.VIN", "C1.1"]),
        gnd(             ["J1.2", "U2.GND", "U1.GND", "C1.2", "C2.2", "C3.2", "C4.2", "C5.2",
                          "D1.cathode", "D2.cathode", "J2.4", "J3.GND", "J4.3", "SW1.2"]),
        pwr("VCC_3V3",   ["U2.VOUT", "U1.VCC", "C2.1", "C3.1", "C4.1", "R1.1", "R2.1", "R4.1",
                          "R5.1", "J2.1", "J3.VCC"]),
        sig("RESET",     ["U1.RESET", "R5.2", "SW1.1", "J3.RESET"]),
        sig("SDA",       ["U1.PB0", "R1.2", "J2.2", "J3.MOSI"]),
        sig("SCL",       ["U1.PB1", "R2.2", "J2.3", "J3.MISO"]),
        sig("ISP_SCK",   ["U1.PB2", "J3.SCK"]),
        sig("LED_DRV",   ["U1.PB3", "R3.1"]),
        sig("LED_AN",    ["R3.2", "D1.anode"]),
        sig("ADC_IN",    ["U1.PB4", "R6.1", "C5.1"]),
        sig("SENSOR_IN", ["R6.2", "J4.2"]),
        pwr("SENS_VCC",  ["J4.1", "J2.1"]),   # sensor connector VCC taps J2.1 — same net alias
        sig("PWR_LED",   ["R4.2", "D2.anode"]),
    ],
    "placement_hints": [
        hint("J1",  edge="left"),
        hint("U2",  near="J1"),
        hint("U1",  x_mm=31.0, y_mm=23.0),
        hint("J2",  edge="right"),
        hint("J3",  edge="bottom"),
        hint("J4",  edge="right", near="J2"),
        hint("SW1", edge="top"),
    ],
    "attachments": [],
}

# TC2 has a logical issue: J4.1 = SENS_VCC but J2.1 = VCC_3V3 (both appear there)
# Fix: make SENS_VCC a separate net that taps VCC_3V3 via a short or just put J4.1 in VCC_3V3
tc02["connections"][11] = pwr("VCC_3V3", [p for p in tc02["connections"][2]["pins"]] + ["J4.1"])
tc02["connections"] = [c for c in tc02["connections"] if c["net_name"] != "SENS_VCC"]
# Now rebuild VCC_3V3 without duplicates
vcc33_pins = ["U2.VOUT", "U1.VCC", "C2.1", "C3.1", "C4.1", "R1.1", "R2.1", "R4.1",
              "R5.1", "J2.1", "J3.VCC", "J4.1"]
tc02["connections"] = [
    pwr("VIN",       ["J1.1", "U2.VIN", "C1.1"]),
    gnd(             ["J1.2", "U2.GND", "U1.GND", "C1.2", "C2.2", "C3.2", "C4.2", "C5.2",
                      "D1.cathode", "D2.cathode", "J2.4", "J3.GND", "J4.3", "SW1.2"]),
    pwr("VCC_3V3",   vcc33_pins),
    sig("RESET",     ["U1.RESET", "R5.2", "SW1.1", "J3.RESET"]),
    sig("SDA",       ["U1.PB0", "R1.2", "J2.2", "J3.MOSI"]),
    sig("SCL",       ["U1.PB1", "R2.2", "J2.3", "J3.MISO"]),
    sig("ISP_SCK",   ["U1.PB2", "J3.SCK"]),
    sig("LED_DRV",   ["U1.PB3", "R3.1"]),
    sig("LED_AN",    ["R3.2", "D1.anode"]),
    sig("ADC_IN",    ["U1.PB4", "R6.1", "C5.1"]),
    sig("SENSOR_IN", ["R6.2", "J4.2"]),
    sig("PWR_LED",   ["R4.2", "D2.anode"]),
]


# ════════════════════════════════════════════════════════════════════════════
# TC3 — 2L dense  (ATmega328P-AU dev board — escape-router stress test)
# ════════════════════════════════════════════════════════════════════════════

tc03 = {
    "project_name": "tc03_2l_dense",
    "description": "Arduino-class ATmega328P-AU (TQFP-32) development board with CH340G USB-UART, 16 MHz crystal, 5 V LDO, ISP header, digital/analog I/O headers — stresses the fine-pitch escape router and auto-reset circuit on 2 layers",
    "power": {"voltage": "5V", "source": "USB-B connector"},
    "board": {"width_mm": 82.0, "height_mm": 62.0, "layers": 2, "corner_radius_mm": 1.5, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        # Connectors
        c("J1",  "connector", "USB-B",                 "USB_B",                          "USB power + UART programming",   "usb"),
        c("J2",  "connector", "ISP 2x3",               "Connector_PinHeader_2.54mm_2x03","In-circuit AVR programming",     "isp"),
        c("J3",  "connector", "Digital I/O 1x10",      "Connector_PinHeader_2.54mm_1x10","PD2-PD7, PB0, PB1, VCC, GND",   "io"),
        c("J4",  "connector", "Analog I/O 1x8",        "Connector_PinHeader_2.54mm_1x08","PC0-PC5, VCC, GND",              "io"),
        c("J5",  "connector", "Power header 1x4",      "Connector_PinHeader_2.54mm_1x04","VBUS/5 V/GND power rails out",   "power"),
        # MCU
        c("U1",  "ic",        "ATmega328P-AU",         "TQFP32",                         "8-bit AVR MCU (Arduino bootloader)","mcu"),
        c("Y1",  "crystal",   "16MHz",                 "HC-49S",                         "MCU system clock",               "mcu"),
        c("C5",  "capacitor", "22pF",                  "0402",                           "Crystal load cap X1",            "mcu"),
        c("C6",  "capacitor", "22pF",                  "0402",                           "Crystal load cap X2",            "mcu"),
        c("C7",  "capacitor", "100nF",                 "0805",                           "U1 VCC bypass 1",                "mcu"),
        c("C8",  "capacitor", "100nF",                 "0805",                           "U1 VCC bypass 2",                "mcu"),
        c("C9",  "capacitor", "100nF",                 "0805",                           "AVCC bypass",                    "mcu"),
        c("C10", "capacitor", "4.7uF",                 "1206",                           "AVCC bulk cap",                  "mcu"),
        c("C11", "capacitor", "100nF",                 "0402",                           "AREF bypass",                    "mcu"),
        c("R2",  "resistor",  "10kohm",                "0805",                           "RESET pull-up",                  "mcu"),
        c("SW1", "switch",    "push button 6mm",       "SW_Push_6mm",                    "Manual RESET",                   "mcu"),
        # USB-UART
        c("U2",  "ic",        "CH340G",                "SOP16",                          "USB-to-UART bridge",             "usb"),
        c("C12", "capacitor", "100nF",                 "0805",                           "U2 VCC bypass",                  "usb"),
        c("C13", "capacitor", "100nF",                 "0805",                           "CH340G 3.3 V cap",               "usb"),
        c("C14", "capacitor", "100nF",                 "0402",                           "Auto-reset DTR coupling cap",    "usb"),
        c("R3",  "resistor",  "33ohm",                 "0402",                           "USB D- series termination",      "usb"),
        c("R4",  "resistor",  "33ohm",                 "0402",                           "USB D+ series termination",      "usb"),
        # Power regulation
        c("U3",  "ic",        "AMS1117-5.0",           "SOT-223",                        "5 V LDO (from higher VBUS)",     "power"),
        c("C1",  "capacitor", "10uF",                  "1206",                           "LDO input bulk cap",             "power"),
        c("C2",  "capacitor", "100nF",                 "0805",                           "LDO input HF bypass",            "power"),
        c("C3",  "capacitor", "10uF",                  "1206",                           "LDO output bulk cap",            "power"),
        c("C4",  "capacitor", "100nF",                 "0805",                           "LDO output HF bypass",           "power"),
        # Indicators
        c("D1",  "led",       "red",                   "0805_LED",                       "Power indicator LED",            "power"),
        c("R1",  "resistor",  "470ohm",                "0805",                           "Power LED current limit",        "power"),
    ],
    "connections": [
        pwr("VBUS",     ["J1.VBUS", "U3.VIN", "C1.1", "C2.1", "J5.1"]),
        gnd(            ["J1.GND", "U3.GND", "C1.2", "C2.2", "C3.2", "C4.2",
                         "U1.GND", "U1.AGND", "C5.2", "C6.2", "C7.2", "C8.2", "C9.2", "C10.2", "C11.2",
                         "U2.GND", "C12.2", "C13.2",
                         "D1.cathode", "SW1.2", "J2.GND", "J3.10", "J4.8", "J5.3", "J5.4"]),
        pwr("VCC_5V",   ["U3.VOUT", "C3.1", "C4.1",
                         "U1.VCC", "U1.AVCC", "C7.1", "C8.1", "C9.1", "C10.1",
                         "U2.VCC", "C12.1",
                         "R1.1", "R2.1",
                         "J2.VCC", "J3.1", "J4.1", "J5.2"]),
        sig("V3_USB",   ["U2.V3", "C13.1"]),
        sig("RESET",    ["U1.RESET", "R2.2", "SW1.1", "J2.RESET", "C14.1"]),
        sig("DTR_LINE", ["U2.DTR", "C14.2"]),
        sig("USB_DM",   ["J1.DM", "R3.1"]),
        sig("USB_DM_IC",["R3.2", "U2.UD_MINUS"]),
        sig("USB_DP",   ["J1.DP", "R4.1"]),
        sig("USB_DP_IC",["R4.2", "U2.UD_PLUS"]),
        sig("UART_TXD", ["U1.PD1", "U2.RXD"]),
        sig("UART_RXD", ["U1.PD0", "U2.TXD"]),
        sig("XTAL1",    ["U1.XTAL1", "Y1.1", "C5.1"]),
        sig("XTAL2",    ["U1.XTAL2", "Y1.2", "C6.1"]),
        sig("AREF",     ["U1.AREF", "C11.1"]),
        sig("PWR_LED",  ["R1.2", "D1.anode"]),
        sig("ISP_MOSI", ["U1.MOSI", "J2.MOSI"]),
        sig("ISP_MISO", ["U1.MISO", "J2.MISO"]),
        sig("ISP_SCK",  ["U1.SCK",  "J2.SCK"]),
        sig("PD2",      ["U1.PD2", "J3.2"]),
        sig("PD3",      ["U1.PD3", "J3.3"]),
        sig("PD4",      ["U1.PD4", "J3.4"]),
        sig("PD5",      ["U1.PD5", "J3.5"]),
        sig("PD6",      ["U1.PD6", "J3.6"]),
        sig("PD7",      ["U1.PD7", "J3.7"]),
        sig("PB0",      ["U1.PB0", "J3.8"]),
        sig("PB1",      ["U1.PB1", "J3.9"]),
        sig("PC0",      ["U1.PC0", "J4.2"]),
        sig("PC1",      ["U1.PC1", "J4.3"]),
        sig("PC2",      ["U1.PC2", "J4.4"]),
        sig("PC3",      ["U1.PC3", "J4.5"]),
        sig("PC4",      ["U1.PC4", "J4.6"]),
        sig("PC5",      ["U1.PC5", "J4.7"]),
    ],
    "placement_hints": [
        hint("J1",  edge="bottom"),
        hint("U2",  near="J1"),
        hint("U3",  near="J1"),
        hint("U1",  x_mm=41.0, y_mm=31.0),
        hint("Y1",  near="U1"),
        hint("J2",  edge="top"),
        hint("J3",  edge="right"),
        hint("J4",  edge="right", near="J3"),
        hint("J5",  edge="left"),
        hint("SW1", edge="top", near="U1"),
    ],
    "attachments": [],
}


# ════════════════════════════════════════════════════════════════════════════
# TC4 — 4L, plane_layers=0  (all 4 layers route signals, no copper planes)
# STM32F103C8T6 + SPI flash + I2C sensor + UART + SWD + power
# ════════════════════════════════════════════════════════════════════════════

tc04 = {
    "project_name": "tc04_4l_planes0",
    "description": "STM32F103C8T6 (LQFP-48) IoT sensor hub with SPI NOR flash, I2C environmental sensor, UART, SWD debug port — 4-layer all-signal board (plane_layers=0): all four copper layers route signals, no solid planes, tests the router's ability to use inner layers as signal layers",
    "power": {"voltage": "3.3V", "source": "USB Micro-B connector"},
    "board": {"width_mm": 65.0, "height_mm": 50.0, "layers": 4, "corner_radius_mm": 1.0, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        # Power
        c("J1",  "connector", "USB Micro-B",         "USB_Micro-B",          "USB power + data input",          "power"),
        c("U3",  "ic",        "AP2112K-3.3",         "SOT-23-5",             "3.3 V 600 mA LDO",               "power"),
        c("C1",  "capacitor", "10uF",                "1206",                  "LDO input bulk",                  "power"),
        c("C2",  "capacitor", "100nF",               "0805",                  "LDO input bypass",                "power"),
        c("C3",  "capacitor", "10uF",                "1206",                  "LDO output bulk",                 "power"),
        c("C4",  "capacitor", "100nF",               "0805",                  "LDO output bypass",               "power"),
        c("D1",  "led",       "green",               "0805_LED",              "Power-on indicator",              "power"),
        c("R1",  "resistor",  "470ohm",              "0805",                  "Power LED limit",                 "power"),
        # MCU
        c("U1",  "ic",        "STM32F103C8T6",       "LQFP-48",              "ARM Cortex-M3 MCU",               "mcu"),
        c("Y1",  "crystal",   "8MHz",                "HC-49S",                "HSE system clock",                "mcu"),
        c("C5",  "capacitor", "22pF",                "0402",                  "HSE load cap X1",                 "mcu"),
        c("C6",  "capacitor", "22pF",                "0402",                  "HSE load cap X2",                 "mcu"),
        c("C7",  "capacitor", "100nF",               "0805",                  "U1 VDD bypass 1",                 "mcu"),
        c("C8",  "capacitor", "100nF",               "0805",                  "U1 VDD bypass 2",                 "mcu"),
        c("C9",  "capacitor", "100nF",               "0805",                  "VDDA bypass",                     "mcu"),
        c("C10", "capacitor", "1uF",                 "1206",                  "VDDA bulk",                       "mcu"),
        c("R2",  "resistor",  "10kohm",              "0805",                  "NRST pull-up",                    "mcu"),
        c("SW1", "switch",    "push button 6mm",     "SW_Push_6mm",           "MCU RESET button",                "mcu"),
        # SPI NOR flash
        c("U2",  "ic",        "W25Q64JVSSIQ",       "SOIC8",                  "64 Mb SPI NOR flash",             "flash"),
        c("C11", "capacitor", "100nF",               "0402",                  "Flash VCC bypass",                "flash"),
        # I2C environmental sensor
        c("U4",  "ic",        "BME280",              "LGA-8",                 "Humidity/pressure/temp sensor",   "sensor"),
        c("C12", "capacitor", "100nF",               "0402",                  "Sensor VDD bypass",               "sensor"),
        c("R3",  "resistor",  "4.7kohm",             "0402",                  "I2C SDA pull-up",                 "sensor"),
        c("R4",  "resistor",  "4.7kohm",             "0402",                  "I2C SCL pull-up",                 "sensor"),
        # Connectors
        c("J2",  "connector", "SWD debug 2x5",      "Connector_PinHeader_1.27mm_2x05","ARM SWD/JTAG debug",     "debug"),
        c("J3",  "connector", "UART 1x4",           "Connector_PinHeader_2.54mm_1x04","USART1 UART header",     "uart"),
    ],
    "connections": [
        pwr("VBUS",    ["J1.VBUS", "U3.VIN", "C1.1", "C2.1"]),
        gnd(           ["J1.GND", "U3.GND", "C1.2", "C2.2", "C3.2", "C4.2",
                        "D1.cathode", "U1.VSS", "U1.VSSA", "C5.2", "C6.2",
                        "C7.2", "C8.2", "C9.2", "C10.2", "SW1.2",
                        "U2.GND", "C11.2", "U4.GND", "C12.2",
                        "J2.GND", "J3.4"]),
        pwr("VCC_3V3", ["U3.VOUT", "C3.1", "C4.1",
                        "U1.VDD", "U1.VDDA", "C7.1", "C8.1", "C9.1", "C10.1",
                        "R1.1", "R2.1", "R3.1", "R4.1",
                        "U2.VCC", "C11.1", "U4.VDD", "C12.1",
                        "J2.VCC", "J3.1"]),
        sig("HSE_IN",  ["U1.PD0_OSC_IN", "Y1.1", "C5.1"]),
        sig("HSE_OUT", ["U1.PD1_OSC_OUT", "Y1.2", "C6.1"]),
        sig("NRST",    ["U1.NRST", "R2.2", "SW1.1", "J2.RESET"]),
        sig("PWR_LED", ["R1.2", "D1.anode"]),
        # SPI flash (SPI1 on PA5/PA6/PA7, flash CS on PA4)
        sig("SPI1_SCK", ["U1.PA5", "U2.CLK"]),
        sig("SPI1_MISO",["U1.PA6", "U2.DO"]),
        sig("SPI1_MOSI",["U1.PA7", "U2.DI"]),
        sig("FLASH_CS", ["U1.PA4", "U2.CS"]),
        sig("FLASH_WP", ["U1.PB0", "U2.WP"]),
        sig("FLASH_HLD",["U1.PB1", "U2.HOLD"]),
        # I2C sensor (I2C1 on PB6/PB7)
        sig("I2C_SCL",  ["U1.PB6", "U4.SCK", "R4.2"]),
        sig("I2C_SDA",  ["U1.PB7", "U4.SDI", "R3.2"]),
        # UART
        sig("UART_TX",  ["U1.PA9",  "J3.2"]),
        sig("UART_RX",  ["U1.PA10", "J3.3"]),
        # SWD debug
        sig("SWDIO",    ["U1.PA13", "J2.SWDIO"]),
        sig("SWDCLK",   ["U1.PA14", "J2.SWDCLK"]),
        # BOOT0 pulled low (normal run mode)
        sig("BOOT0_GND",["U1.BOOT0", "J2.SWO"]),   # share J2.SWO pin net for pull-through
    ],
    "placement_hints": [
        hint("J1",  edge="left"),
        hint("U3",  near="J1"),
        hint("U1",  x_mm=33.0, y_mm=25.0),
        hint("Y1",  near="U1"),
        hint("U2",  near="U1"),
        hint("U4",  near="U1"),
        hint("J2",  edge="top"),
        hint("J3",  edge="right"),
        hint("SW1", edge="bottom"),
    ],
    "attachments": [],
}


# ════════════════════════════════════════════════════════════════════════════
# TC5 — 4L, plane_layers=1  (In1 = GND solid plane; In2 + F + B = signal)
# STM32F103 + CAN transceiver + SD card + power + headers
# ════════════════════════════════════════════════════════════════════════════

tc05 = {
    "project_name": "tc05_4l_planes1",
    "description": "STM32F103C8T6 (LQFP-48) data-logger board: CAN bus, SD-card SPI, USART, SWD — 4-layer with one inner GND plane (plane_layers=1); In1.Cu is a solid GND pour, In2.Cu and the outer layers route signals — tests GND-plane integrity path",
    "power": {"voltage": "5V", "source": "DC barrel jack 5.5/2.1mm"},
    "board": {"width_mm": 72.0, "height_mm": 56.0, "layers": 4, "corner_radius_mm": 1.5, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        # Power
        c("J1",  "connector", "DC barrel jack 5V",   "DC_Jack_2.1x5.5",       "5 V power input",                "power"),
        c("U3",  "ic",        "LM1117-3.3",          "SOT-223",               "3.3 V 800 mA LDO",               "power"),
        c("C1",  "capacitor", "10uF",                "1206",                   "LDO input bulk",                  "power"),
        c("C2",  "capacitor", "100nF",               "0805",                   "LDO input bypass",                "power"),
        c("C3",  "capacitor", "10uF",                "1206",                   "LDO output bulk",                 "power"),
        c("C4",  "capacitor", "100nF",               "0805",                   "LDO output bypass",               "power"),
        c("D1",  "led",       "red",                 "0805_LED",               "Power indicator",                 "power"),
        c("R1",  "resistor",  "470ohm",              "0805",                   "Power LED limit",                 "power"),
        # MCU
        c("U1",  "ic",        "STM32F103C8T6",       "LQFP-48",               "ARM Cortex-M3 MCU",               "mcu"),
        c("Y1",  "crystal",   "8MHz",                "HC-49S",                 "HSE system clock",                "mcu"),
        c("C5",  "capacitor", "22pF",                "0402",                   "HSE load cap X1",                 "mcu"),
        c("C6",  "capacitor", "22pF",                "0402",                   "HSE load cap X2",                 "mcu"),
        c("C7",  "capacitor", "100nF",               "0805",                   "U1 VDD bypass 1",                 "mcu"),
        c("C8",  "capacitor", "100nF",               "0805",                   "U1 VDD bypass 2",                 "mcu"),
        c("C9",  "capacitor", "100nF",               "0805",                   "VDDA bypass",                     "mcu"),
        c("R2",  "resistor",  "10kohm",              "0805",                   "NRST pull-up",                    "mcu"),
        c("SW1", "switch",    "push button 6mm",     "SW_Push_6mm",            "MCU RESET button",                "mcu"),
        # CAN transceiver
        c("U2",  "ic",        "SN65HVD230",          "SOIC8",                  "3.3 V CAN bus transceiver",       "can"),
        c("R3",  "resistor",  "120ohm",              "0805",                   "CAN bus 120 Ω termination",       "can"),
        c("C10", "capacitor", "100nF",               "0402",                   "CAN IC bypass",                   "can"),
        c("J3",  "connector", "CAN 1x3",            "Connector_PinHeader_2.54mm_1x03","CANH/CANL/GND header",    "can"),
        # SD card SPI
        c("J4",  "connector", "MicroSD card slot",  "MicroSD_HC_Vertical",    "MicroSD card via SPI",            "storage"),
        c("C11", "capacitor", "100nF",               "0402",                   "SD card VCC bypass",              "storage"),
        c("R4",  "resistor",  "10kohm",              "0402",                   "SD card CS pull-up",              "storage"),
        c("R5",  "resistor",  "10kohm",              "0402",                   "SD card MISO pull-up",            "storage"),
        # Debug + UART
        c("J2",  "connector", "SWD debug 2x5",      "Connector_PinHeader_1.27mm_2x05","ARM SWD debug",           "debug"),
        c("J5",  "connector", "UART 1x4",           "Connector_PinHeader_2.54mm_1x04","USART1 UART header",      "uart"),
        # Status LED
        c("D2",  "led",       "green",               "0805_LED",               "Activity indicator",              "mcu"),
        c("R6",  "resistor",  "100ohm",              "0805",                   "Activity LED limit",              "mcu"),
    ],
    "connections": [
        pwr("VIN",     ["J1.1", "U3.VIN", "C1.1", "C2.1"]),
        gnd(           ["J1.2", "U3.GND", "C1.2", "C2.2", "C3.2", "C4.2",
                        "D1.cathode", "D2.cathode",
                        "U1.VSS", "U1.VSSA", "C5.2", "C6.2",
                        "C7.2", "C8.2", "C9.2", "SW1.2",
                        "U2.GND", "C10.2", "R3.2", "J3.3",
                        "J4.GND", "C11.2",
                        "J2.GND", "J5.4"]),
        pwr("VCC_3V3", ["U3.VOUT", "C3.1", "C4.1",
                        "U1.VDD", "U1.VDDA", "C7.1", "C8.1", "C9.1",
                        "R1.1", "R2.1",
                        "U2.VCC", "C10.1",
                        "J4.VCC", "C11.1", "R4.1", "R5.1",
                        "J2.VCC", "J5.1"]),
        sig("HSE_IN",   ["U1.PD0_OSC_IN",  "Y1.1", "C5.1"]),
        sig("HSE_OUT",  ["U1.PD1_OSC_OUT", "Y1.2", "C6.1"]),
        sig("NRST",     ["U1.NRST", "R2.2", "SW1.1", "J2.RESET"]),
        sig("PWR_LED",  ["R1.2", "D1.anode"]),
        sig("ACT_LED",  ["R6.2", "D2.anode"]),
        sig("ACT_DRV",  ["U1.PC13", "R6.1"]),
        # CAN (CANRX=PA11, CANTX=PA12 on STM32F103)
        sig("CAN_TX",   ["U1.PA12", "U2.TXD"]),
        sig("CAN_RX",   ["U1.PA11", "U2.RXD"]),
        sig("CANH",     ["U2.CANH", "R3.1", "J3.1"]),
        sig("CANL",     ["U2.CANL", "J3.2"]),
        # SPI2 for SD card (PB13=SCK, PB14=MISO, PB15=MOSI, PB12=CS)
        sig("SD_SCK",   ["U1.PB13", "J4.SCK"]),
        sig("SD_MISO",  ["U1.PB14", "J4.MISO", "R5.2"]),
        sig("SD_MOSI",  ["U1.PB15", "J4.MOSI"]),
        sig("SD_CS",    ["U1.PB12", "J4.CS", "R4.2"]),
        sig("SD_CD",    ["U1.PB11", "J4.CD"]),
        # USART1
        sig("UART_TX",  ["U1.PA9",  "J5.2"]),
        sig("UART_RX",  ["U1.PA10", "J5.3"]),
        # SWD
        sig("SWDIO",    ["U1.PA13", "J2.SWDIO"]),
        sig("SWDCLK",   ["U1.PA14", "J2.SWDCLK"]),
        sig("BOOT0_LO", ["U1.BOOT0", "J2.SWO"]),
        # HSE control
        sig("HSE_EN",   ["U2.S", "U1.PA8"]),   # CAN transceiver slope control
    ],
    "placement_hints": [
        hint("J1",  edge="left"),
        hint("U3",  near="J1"),
        hint("U1",  x_mm=36.0, y_mm=28.0),
        hint("Y1",  near="U1"),
        hint("U2",  near="J3"),
        hint("J3",  edge="right"),
        hint("J4",  edge="bottom"),
        hint("J2",  edge="top"),
        hint("J5",  edge="right", near="J2"),
        hint("SW1", edge="top", near="U1"),
    ],
    "attachments": [],
}


# ════════════════════════════════════════════════════════════════════════════
# TC6 — 4L, plane_layers=2  (In1=GND plane, In2=PWR plane — full power integrity)
# STM32F407 + USB FS + Ethernet PHY + dual power rails + headers
# ════════════════════════════════════════════════════════════════════════════

tc06 = {
    "project_name": "tc06_4l_planes2",
    "description": "STM32F407VGT6 (LQFP-100) Ethernet+USB gateway — 4-layer with full GND+power planes (plane_layers=2): In1.Cu=GND plane, In2.Cu=3.3 V power plane, F.Cu+B.Cu=signal layers. Multiple power rails (3.3 V, 1.2 V core), LAN8720 Ethernet PHY, USB FS, SWD, CAN — high component density tests routing under ideal power-integrity constraints",
    "power": {"voltage": "5V", "source": "USB-C power delivery connector"},
    "board": {"width_mm": 76.0, "height_mm": 60.0, "layers": 4, "corner_radius_mm": 2.0, "outline_type": "rectangle"},
    "manufacturing": {"manufacturer": "jlcpcb_standard"},
    "components": [
        # Power input + filtering
        c("J1",  "connector", "USB-C 16-pin",       "USB_C_Receptacle_GCT_USB4135","USB-C power + data",          "power"),
        c("R_CC1","resistor", "5.1kohm",            "0402",                  "USB-C CC1 pull-down",             "power"),
        c("R_CC2","resistor", "5.1kohm",            "0402",                  "USB-C CC2 pull-down",             "power"),
        # 3.3 V LDO
        c("U4",  "ic",        "LM1117-3.3",          "SOT-223",              "3.3 V main LDO",                  "power"),
        c("C1",  "capacitor", "10uF",                "1206",                 "LDO input bulk",                  "power"),
        c("C2",  "capacitor", "100nF",               "0805",                 "LDO input bypass",                "power"),
        c("C3",  "capacitor", "10uF",                "1206",                 "LDO 3.3 V output bulk",           "power"),
        c("C4",  "capacitor", "100nF",               "0805",                 "LDO 3.3 V output bypass",         "power"),
        # 1.2 V core LDO for STM32F4 VCAP
        c("C_VCAP1","capacitor","2.2uF",             "1206",                 "STM32F4 VCAP1 internal regulator","power"),
        c("C_VCAP2","capacitor","2.2uF",             "1206",                 "STM32F4 VCAP2 internal regulator","power"),
        # MCU STM32F407VGT6 (LQFP-100)
        c("U1",  "ic",        "STM32F407VGT6",       "LQFP-100",            "ARM Cortex-M4 MCU with FPU",      "mcu"),
        c("Y1",  "crystal",   "25MHz",               "HC-49S",               "HSE system clock",                "mcu"),
        c("C5",  "capacitor", "22pF",                "0402",                 "HSE load cap X1",                 "mcu"),
        c("C6",  "capacitor", "22pF",                "0402",                 "HSE load cap X2",                 "mcu"),
        c("C7",  "capacitor", "100nF",               "0402",                 "U1 VDD bypass 1",                 "mcu"),
        c("C8",  "capacitor", "100nF",               "0402",                 "U1 VDD bypass 2",                 "mcu"),
        c("C9",  "capacitor", "100nF",               "0402",                 "U1 VDD bypass 3",                 "mcu"),
        c("C10", "capacitor", "100nF",               "0402",                 "VDDA bypass",                     "mcu"),
        c("R2",  "resistor",  "10kohm",              "0402",                 "NRST pull-up",                    "mcu"),
        c("SW1", "switch",    "push button 6mm",     "SW_Push_6mm",          "MCU RESET button",                "mcu"),
        # Ethernet PHY LAN8720A
        c("U2",  "ic",        "LAN8720A",            "QFN-24",              "10/100 Ethernet PHY (RMII)",       "eth"),
        c("Y2",  "crystal",   "25MHz",               "HC-49S",               "LAN8720 reference clock",         "eth"),
        c("C11", "capacitor", "22pF",                "0402",                 "ETH XTAL load X1",                "eth"),
        c("C12", "capacitor", "22pF",                "0402",                 "ETH XTAL load X2",                "eth"),
        c("C13", "capacitor", "100nF",               "0402",                 "LAN8720 VDD bypass",              "eth"),
        c("C14", "capacitor", "10uF",                "1206",                 "LAN8720 VDD bulk",                "eth"),
        c("R3",  "resistor",  "49.9ohm",             "0402",                 "ETH TX+ series resistor",         "eth"),
        c("R4",  "resistor",  "49.9ohm",             "0402",                 "ETH TX- series resistor",         "eth"),
        c("J2",  "connector", "RJ45 with magnetics", "RJ45_Vertical",        "Ethernet RJ45 jack",              "eth"),
        # USB FS (USB-HS PHY internal) — just the series resistors + ESD diode
        c("R5",  "resistor",  "22ohm",               "0402",                 "USB FS D+ series resistor",       "usb"),
        c("R6",  "resistor",  "22ohm",               "0402",                 "USB FS D- series resistor",       "usb"),
        # CAN transceiver
        c("U3",  "ic",        "SN65HVD230",          "SOIC8",               "3.3 V CAN bus transceiver",        "can"),
        c("R7",  "resistor",  "120ohm",              "0805",                 "CAN termination",                 "can"),
        c("C15", "capacitor", "100nF",               "0402",                 "CAN IC bypass",                   "can"),
        c("J4",  "connector", "CAN 1x3",            "Connector_PinHeader_2.54mm_1x03","CANH/CANL/GND",          "can"),
        # Debug + UART
        c("J3",  "connector", "SWD 2x5 1.27mm",     "Connector_PinHeader_1.27mm_2x05","ARM SWD debug",          "debug"),
        c("J5",  "connector", "UART 1x4",           "Connector_PinHeader_2.54mm_1x04","USART3 UART",            "uart"),
        # Indicators
        c("D1",  "led",       "red",                 "0805_LED",             "Power indicator",                 "power"),
        c("R8",  "resistor",  "470ohm",              "0805",                 "Power LED limit",                 "power"),
        c("D2",  "led",       "green",               "0805_LED",             "Ethernet link/activity",          "eth"),
        c("R9",  "resistor",  "100ohm",              "0805",                 "ETH LED limit",                   "eth"),
    ],
    "connections": [
        pwr("VBUS",    ["J1.VBUS", "U4.VIN", "C1.1", "C2.1"]),
        gnd(           ["J1.GND",
                        "R_CC1.2", "R_CC2.2",
                        "U4.GND", "C1.2", "C2.2", "C3.2", "C4.2",
                        "D1.cathode", "D2.cathode",
                        "U1.VSS", "U1.VSSA", "C5.2", "C6.2",
                        "C7.2", "C8.2", "C9.2", "C10.2",
                        "C_VCAP1.2", "C_VCAP2.2",
                        "SW1.2", "R2.2",
                        "U2.GND", "U2.EP", "C11.2", "C12.2", "C13.2", "C14.2",
                        "J2.GND",
                        "U3.GND", "C15.2", "R7.2", "J4.3",
                        "J3.GND", "J5.4"]),
        pwr("VCC_3V3", ["U4.VOUT", "C3.1", "C4.1",
                        "U1.VDD", "U1.VDDA", "C7.1", "C8.1", "C9.1", "C10.1",
                        "R2.1", "R8.1", "R9.1",
                        "U2.VDD", "C13.1", "C14.1",
                        "U3.VCC", "C15.1",
                        "J3.VCC", "J5.1"]),
        sig("CC1_PD",  ["J1.CC1", "R_CC1.1"]),
        sig("CC2_PD",  ["J1.CC2", "R_CC2.1"]),
        sig("HSE_IN",  ["U1.PH0_OSC_IN",  "Y1.1", "C5.1"]),
        sig("HSE_OUT", ["U1.PH1_OSC_OUT", "Y1.2", "C6.1"]),
        sig("VCAP1",   ["U1.VCAP1", "C_VCAP1.1"]),
        sig("VCAP2",   ["U1.VCAP2", "C_VCAP2.1"]),
        sig("NRST",    ["U1.NRST", "SW1.1", "J3.RESET"]),
        sig("PWR_LED", ["R8.2", "D1.anode"]),
        sig("ETH_LED", ["R9.2", "D2.anode"]),
        sig("ETH_LED_DRV", ["U1.PA1", "R9.1"]),   # PA1 drives ETH activity LED
        # RMII Ethernet (STM32F407 RMII on specific pins)
        sig("ETH_REFCLK",["U1.PA1_ETH_REF_CLK", "U2.REFCLK"]),
        sig("ETH_MDIO",  ["U1.PA2_ETH_MDIO",    "U2.MDIO"]),
        sig("ETH_MDC",   ["U1.PC1_ETH_MDC",     "U2.MDC"]),
        sig("ETH_CRS",   ["U1.PA7_ETH_CRS_DV",  "U2.CRS_DV"]),
        sig("ETH_RXD0",  ["U1.PC4_ETH_RXD0",    "U2.RXD0"]),
        sig("ETH_RXD1",  ["U1.PC5_ETH_RXD1",    "U2.RXD1"]),
        sig("ETH_TXEN",  ["U1.PB11_ETH_TXEN",   "U2.TXEN"]),
        sig("ETH_TXD0",  ["U1.PB12_ETH_TXD0",   "U2.TXD0", "R3.1"]),
        sig("ETH_TXD0B", ["R3.2", "J2.TX_PLUS"]),
        sig("ETH_TXD1",  ["U1.PB13_ETH_TXD1",   "U2.TXD1", "R4.1"]),
        sig("ETH_TXD1B", ["R4.2", "J2.TX_MINUS"]),
        sig("ETH_RX_P",  ["J2.RX_PLUS",  "U2.RX_PLUS"]),
        sig("ETH_RX_N",  ["J2.RX_MINUS", "U2.RX_MINUS"]),
        sig("ETH_CLK_IN", ["U2.XI",  "Y2.1", "C11.1"]),
        sig("ETH_CLK_OUT",["U2.XO", "Y2.2", "C12.1"]),
        sig("ETH_NRST", ["U1.PB14_ETH_NRST", "U2.NRST"]),
        # USB FS (D+/D- on PA11/PA12 on STM32F407)
        sig("USB_DP_MCU",["U1.PA12_USB_DP", "R5.1"]),
        sig("USB_DP_CONN",["R5.2", "J1.USB_DP"]),
        sig("USB_DM_MCU",["U1.PA11_USB_DM", "R6.1"]),
        sig("USB_DM_CONN",["R6.2", "J1.USB_DM"]),
        # CAN (PA11/PA12 are shared with USB on F407 — use PD0/PD1 for CAN instead)
        sig("CAN_TX",   ["U1.PD1_CAN_TX", "U3.TXD"]),
        sig("CAN_RX",   ["U1.PD0_CAN_RX", "U3.RXD"]),
        sig("CANH",     ["U3.CANH", "R7.1", "J4.1"]),
        sig("CANL",     ["U3.CANL", "J4.2"]),
        sig("CAN_S",    ["U3.S", "U1.PC0_CAN_S"]),
        # USART3 (PB10/PB11 — but PB11 is ETH_TXEN; use PD8/PD9)
        sig("UART3_TX", ["U1.PD8_UART3_TX", "J5.2"]),
        sig("UART3_RX", ["U1.PD9_UART3_RX", "J5.3"]),
        # SWD
        sig("SWDIO",    ["U1.PA13_SWDIO",  "J3.SWDIO"]),
        sig("SWDCLK",   ["U1.PA14_SWDCLK", "J3.SWDCLK"]),
    ],
    "placement_hints": [
        hint("J1",  edge="left"),
        hint("U4",  near="J1"),
        hint("U1",  x_mm=38.0, y_mm=30.0),
        hint("Y1",  near="U1"),
        hint("U2",  near="J2"),
        hint("Y2",  near="U2"),
        hint("J2",  edge="right"),
        hint("U3",  near="J4"),
        hint("J4",  edge="right", near="J2"),
        hint("J3",  edge="top"),
        hint("J5",  edge="top", near="J3"),
        hint("SW1", edge="bottom"),
    ],
    "attachments": [],
}

# TC6 has a conflict: ETH_LED_DRV uses U1.PA1 but so does ETH_REFCLK (PA1_ETH_REF_CLK).
# Fix: remove ETH_LED_DRV net and drive LED from a different pin (PC2)
tc06["connections"] = [c for c in tc06["connections"] if c["net_name"] != "ETH_LED_DRV"]
# ETH_LED now driven from PC2
tc06["connections"].append(sig("ETH_LED_DRV", ["U1.PC2_LED", "R9.1"]))
# Also R9.1 is already in VCC_3V3 — oops! LED connects VCC→resistor→LED→pin (active low) or pin→resistor→LED→GND (active high)
# Fix: remove R9.1 from VCC_3V3 and make it a pure signal path: MCU pin → R9 → D2 → GND
# This means D2.anode is on ETH_LED, D2.cathode is GND (already done), R9.1 is driven by MCU
# Rebuild VCC_3V3 without R9.1
for conn in tc06["connections"]:
    if conn["net_name"] == "VCC_3V3" and "R9.1" in conn["pins"]:
        conn["pins"].remove("R9.1")
    if conn["net_name"] == "ETH_LED":
        conn["pins"] = ["R9.2", "D2.anode"]


# ════════════════════════════════════════════════════════════════════════════
# TC7 — 4L, plane_layers=3  (clamped to 2 by router — same circuit as TC6)
# Verifies graceful handling of out-of-range plane_layers
# ════════════════════════════════════════════════════════════════════════════

import copy
tc07 = copy.deepcopy(tc06)
tc07["project_name"] = "tc07_4l_planes3"
tc07["description"] = (
    "Identical circuit to tc06 but the optimize_placement call will use plane_layers=3. "
    "The router clamps this to 2 (max for a 4-layer board with only 2 inner layers), "
    "so the result should match tc06. Verifies the graceful plane_layers clamping path "
    "in the routing engine without producing an error."
)


# ════════════════════════════════════════════════════════════════════════════
# Manifest
# ════════════════════════════════════════════════════════════════════════════

MANIFEST = {
    "description": "PCB test-case suite — covers 2L and 4L stackups with 0–3 inner planes. "
                   "Feed each requirements JSON into the MCP pipeline; use the plane_layers "
                   "column when calling optimize_placement.",
    "test_cases": [
        {
            "id": "TC1", "file": "tc01_2l_minimal.json",
            "layers": 2, "plane_layers": None,
            "components": 14, "board_mm": "50x35",
            "purpose": "Regression baseline — 555 timer + 3 LEDs; should always route 100%.",
        },
        {
            "id": "TC2", "file": "tc02_2l_moderate.json",
            "layers": 2, "plane_layers": None,
            "components": 20, "board_mm": "62x46",
            "purpose": "ATtiny85 I2C sensor module — mixed SMD/TH, functional-group affinity.",
        },
        {
            "id": "TC3", "file": "tc03_2l_dense.json",
            "layers": 2, "plane_layers": None,
            "components": 30, "board_mm": "82x62",
            "purpose": "ATmega328P-AU (TQFP-32) dev board — escape-router stress test, auto-reset circuit.",
        },
        {
            "id": "TC4", "file": "tc04_4l_planes0.json",
            "layers": 4, "plane_layers": 0,
            "components": 26, "board_mm": "65x50",
            "purpose": "4-layer all-signal (no planes): tests router using inner layers as signal layers.",
        },
        {
            "id": "TC5", "file": "tc05_4l_planes1.json",
            "layers": 4, "plane_layers": 1,
            "components": 29, "board_mm": "72x56",
            "purpose": "4-layer one GND plane (In1): data-logger with CAN + SD-card SPI.",
        },
        {
            "id": "TC6", "file": "tc06_4l_planes2.json",
            "layers": 4, "plane_layers": 2,
            "components": 40, "board_mm": "76x60",
            "purpose": "4-layer full GND+PWR planes: STM32F407 + Ethernet + USB — power-integrity gold standard.",
        },
        {
            "id": "TC7", "file": "tc07_4l_planes3.json",
            "layers": 4, "plane_layers": 3,
            "components": 40, "board_mm": "76x60",
            "purpose": "plane_layers=3 → clamped to 2; identical result to TC6. Verifies bounds handling.",
        },
    ],
}


# ════════════════════════════════════════════════════════════════════════════
# Write all files
# ════════════════════════════════════════════════════════════════════════════

print("Generating test cases…")
save(tc01, "tc01_2l_minimal.json")
save(tc02, "tc02_2l_moderate.json")
save(tc03, "tc03_2l_dense.json")
save(tc04, "tc04_4l_planes0.json")
save(tc05, "tc05_4l_planes1.json")
save(tc06, "tc06_4l_planes2.json")
save(tc07, "tc07_4l_planes3.json")
(OUT / "manifest.json").write_text(json.dumps(MANIFEST, indent=2))
print("  manifest.json")
print("Done.")
