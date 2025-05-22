# main.py -- your main application
from ..networking import Wifi, WiFiDevice
import time


wifi = Wifi()
wifi.clone_network(device_to_clone='TP-Link_698E', new_ssid_prefix="CLONE_")
print("Cloned network. New SSID should start with 'CLONE_'.")
# wait for 60 seconds to see if the cloned network appears
print("Waiting for 60 seconds to see if the cloned network appears...")
time.sleep(60)

print("Disconnecting from the cloned network...")
wifi.disconnect()
print("Disconnected from the cloned network.")