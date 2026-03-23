**LED Current-Limiting Resistors**
- R = (V_supply - V_forward) / I_forward
- P = I² × R → package rating must be ≥ 2× calculated power
- V_forward defaults: red/yellow/orange ≈ 2.0V, green/blue/white ≈ 3.2V
- I_forward: 20mA default unless specified

**Resistor Power Ratings**
- P = V²/R or P = I²×R
- Required: rated power ≥ 2× calculated power
- Package ratings: 0402 → 63mW, 0603 → 100mW, 0805 → 125mW, 1206 → 250mW, 1210 → 500mW, 2512 → 1W

**Capacitor Voltage Ratings**
- Ceramic: V_rated ≥ 1.5× V_supply
- Electrolytic: V_rated ≥ 2× V_supply

**Decoupling Capacitors**: 100nF ceramic per IC VCC pin, ≥10µF bulk cap per power rail

**Pull-up/Pull-down Resistors**: I²C: 4.7kΩ (3.3V) or 2.2kΩ (5V). RESET: 10kΩ to VCC.
