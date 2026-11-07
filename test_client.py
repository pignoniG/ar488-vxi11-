#!/usr/bin/env python3
"""Talk to the gateway from any machine:  pip install pyvisa pyvisa-py
   python3 test_client.py <pi-ip> [gpib0,16] [--scan gpib0]"""
import sys, pyvisa
host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
rm = pyvisa.ResourceManager("@py")
if "--scan" in sys.argv:
    bus = sys.argv[sys.argv.index("--scan") + 1]
    for a in range(1, 31):
        try:
            i = rm.open_resource(f"TCPIP::{host}::{bus},{a}::INSTR", timeout=500)
            print(a, i.query("*IDN?").strip())
        except Exception:
            pass
        else:
            i.close()
    sys.exit()
gw = rm.open_resource(f"TCPIP::{host}::inst0::INSTR")
print("gateway:", gw.query("*IDN?").strip(), "|", gw.query("INTERFACES?").strip())
dev = sys.argv[2] if len(sys.argv) > 2 else "gpib0,16"
inst = rm.open_resource(f"TCPIP::{host}::{dev}::INSTR", timeout=5000)
print(dev, "->", inst.query("*IDN?").strip())
