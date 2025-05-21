[esp32-micropython on PyPI](https://pypi.org/project/esp32-micropython/0.2.4/) • [GitHub repo](https://github.com/laelhalawani/esp32_micropython)

I automated the tedious steps of flashing and managing files on ESP32-C3 boards with a simple CLI tool.

1. Install the utility:
```bash
pip install esp32_micropython
```

2. Connect your board via USB-C.
- Verify it appears under “Ports (COM & LPT)” in Device Manager.
- If it keeps reconnecting, hold the BOOT (power) button while plugging in.

3. List available ports:
```bash
esp32 devices
```

4. Select your board’s port (example uses COM5):
```bash
esp32 device COM5 --force
```

5. Flash MicroPython firmware:
```bash
esp32 flash
```
By default, this downloads and installs the official USB-enabled build.  
To use a custom firmware:
```bash
esp32 flash https://example.com/your_firmware.bin
```

6. Verify the connection (no `--force` needed if already flashed):
```bash
esp32 device COM5
```

---

## Uploading files

### Upload a single file to the root
```bash
esp32 upload main.py
```
Result on ESP32: `/main.py`

### Upload a single file to a specific remote directory
```bash
esp32 upload utils.py lib
```
Result on ESP32: `/lib/utils.py` (directory `lib/` created if needed)

### Upload contents of a local directory to root
```bash
esp32 upload local_project/
```
Assuming `local_project/` contains `file1.py` and `subdir/file2.py`, result:
```
/file1.py
/subdir/file2.py
```

### Upload contents of a local directory to a specific remote directory
```bash
esp32 upload local_project/ remote_app
```
Result:
```
/remote_app/file1.py
/remote_app/subdir/file2.py
```

### Upload a local directory itself to root
```bash
esp32 upload my_library
```
Result:
```
/my_library/...
```

### Upload a local directory into a specific remote directory
```bash
esp32 upload my_library existing_remote_lib_folder
```
Result:
```
/existing_remote_lib_folder/my_library/...
```

---

## Downloading files

### Download a remote file to the current local directory
```bash
esp32 download /boot.py
```
Result: `./boot.py`

### Download a remote file to a specific local directory
```bash
esp32 download /lib/utils.py my_local_lib
```
Result: `./my_local_lib/utils.py`

### Download a remote file to a specific local path and name
```bash
esp32 download /data/sensor.dat backup/latest_sensor.dat
```
Result: `./backup/latest_sensor.dat`

### Download a remote directory and its contents into the current local directory
```bash
esp32 download /logs
```
Result:
```
./logs/...
```

### Download a remote directory and its contents into a specified local directory
```bash
esp32 download /data backup_data
```
Result:
```
./backup_data/data/...
```

### Download the contents of a remote directory into the current local directory
```bash
esp32 download /app/ .
```
If `/app/main.py` and `/app/gfx/img.png` exist, they become:
```
./main.py
./gfx/img.png
```

### Download the contents of a remote directory into a specified local directory
```bash
esp32 download /lib/ local_libs_backup
```
Result:
```
./local_libs_backup/tool.py
```

### Download the contents of the device’s root directory into a local directory
```bash
esp32 download // full_backup
```
Result:
```
./full_backup/...
```

---

## Running scripts

Execute any uploaded Python script and view its output:
```bash
esp32 run path/to/script.py
```

---

## Exploring the device

### List files
```bash
esp32 list
```
Optionally pass a path:
```bash
esp32 list lib
```

### Show directory tree
```bash
esp32 tree
```
Optionally pass a path:
```bash
esp32 tree lib
```
Example output:
```
Tree for ':/' on device:
.
├── __init__.py
├── boot.py
├── main.py
└── networking
    ├── __init__.py
    ├── models.py
    └── wifi.py
```

---

Feel free to adapt this workflow to your needs. Contributions and feedback are welcome—see the full docs on GitHub!
