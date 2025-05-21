**Guide: Flashing MicroPython on Your ESP32-C3 SuperMini**

Once your ESP32-C3 SuperMini is stably connected (COM port solid, BOOT-override applied), follow these steps to erase any existing firmware and flash the official MicroPython build.

---

## 1. Install the Flashing Tool (`esptool`)

1. Open **PowerShell** (Win + X → Windows Terminal / PowerShell).
2. Install or upgrade **esptool** via pip:

   ```powershell
   pip install --upgrade esptool==4.8.1
   ```

   This is the official Espressif utility for writing firmware to ESP32-series chips.

---

## 2. Download the MicroPython Firmware

1. In your browser, navigate to the MicroPython download page for ESP32-C3:
   [https://micropython.org/download/esp32c3/](https://micropython.org/download/esp32c3/)
2. Under the “ESP32-C3” section, download the latest **USB-enabled** `.bin` file (for example: `esp32-c3-20241129-v1.24.1.bin`).
3. Save it to a folder you’ll remember (e.g., `C:\Users\<you>\Downloads\`) and rename it to:

   ```
   firmware.bin
   ```

---

## 3. Erase Existing Flash

1. In PowerShell, change into your download folder:

   ```powershell
   cd $HOME\Downloads
   ```
2. Run the erase command (replace **COM5** with your actual port):

   ```powershell
   esptool --chip esp32c3 --port COM5 erase_flash
   ```
3. Wait until you see:

   ```
   Erase completed successfully
   ```

---

## 4. Write (Flash) MicroPython

1. Still in the same folder, flash the firmware:

   ```powershell
   esptool --chip esp32c3 --port COM5 --baud 460800 write_flash -z 0x0 firmware.bin
   ```
2. You should see progress messages ending with:

   ```
   Hash of data verified.
   Leaving…
   Hard resetting via RTS pin…
   ```
3. At this point, MicroPython is installed!

---

## 5. Verify the Flash

1. Open a new PowerShell window.
2. Connect to the REPL to confirm the prompt:

   ```powershell
   mpremote connect COM5 repl
   ```
3. You should see the MicroPython prompt:

   ```
   >>> 
   ```
4. Exit with **Ctrl-]**.

---

### Useful Links

* **esptool PyPI** (install & docs): [https://pypi.org/project/esptool/](https://pypi.org/project/esptool/)
* **MicroPython ESP32-C3 Downloads**: [https://micropython.org/download/esp32c3/](https://micropython.org/download/esp32c3/)
* **mpremote (REPL tool)**: [https://docs.micropython.org/en/latest/reference/mpremote.html](https://docs.micropython.org/en/latest/reference/mpremote.html)

You’ve now successfully flashed MicroPython—next up is uploading your own `main.py` and exploring Python on the ESP32-C3!
