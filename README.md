# ESP32-MicroPython Utility

**`esp32_micropython`** is an all-in-one command-line utility designed to simplify flashing MicroPython firmware and managing file deployments on ESP32-C3 SuperMini boards (and compatible variants) that feature a built-in USB-C connector for direct serial communication.

It leverages `esptool` for flashing firmware and `mpremote` for file system operations and REPL interaction.

## Features

*   Flash MicroPython firmware (downloads official ESP32-C3 USB-enabled firmware by default).
*   List available serial ports and set a default device.
*   Upload individual files, directory contents, or entire directories to the device.
*   Download files, directory contents, or entire directories from the device using a unified command.
*   List files, display directory trees, and delete files/directories on the device.
*   Run MicroPython scripts remotely.
*   Simplified commands for common operations.

## 1. Identifying Your Board

Before you begin, it's crucial to correctly identify your ESP32-C3 board and ensure it can be stably connected to your computer.

*   **Chip Markings**: These boards typically use the ESP32-C3 System-on-Chip. Look for silkscreen markings on the chip like `ESP32-C3 FH4...` or similar. The general pattern is `ESP32-C3 XX Y ZZZZZZ T U? VVVVVVV WWWWWWWWWW` where `XX` indicates flash/temperature, `Y` is flash size, `ZZZZZZ` is a date/lot code, etc.
*   **Visual Cues**:
    *   USB-C connector for power and data.
    *   Two push-buttons: `BOOT` (often IO0) and `RST` (Reset).
    *   Specific pin labels (refer to board documentation if available).
    *   A power LED.

**For a detailed guide on board identification, pinouts, and establishing a stable USB connection (especially the BOOT button procedure for flashing), please refer to the guide: [`docs_md/identify_board.md`](docs_md/identify_board.md).**

## 2. Installation

You can install the `esp32_micropython` utility and its dependencies (`esptool`, `mpremote`, `pyserial`) using pip:

```bash
pip install esp32_micropython
```
*(If installing from local source, you'd typically use `pip install .` or `python setup.py install` from the project root.)*

Ensure that Python and pip are correctly installed and configured in your system's PATH.

## 3. General Usage

The utility is invoked from your terminal or PowerShell:

```bash
esp32 [global_options] <command> [<args>...]
```

**Global Options:**

*   `--port <PORT_NAME>` or `-p <PORT_NAME>`: Temporarily overrides the configured COM port for the current command. For example, `esp32 --port COM7 flash`.

## 4. Commands

### 4.1 Selecting Your Device Port

Before most operations, you need to tell the tool which serial port your ESP32-C3 is connected to.

*   **`esp32 devices`**
    Lists all available serial (COM) ports detected on your system. The currently selected/configured port will be marked with an asterisk (`*`).

    ```bash
    esp32 devices
    ```

*   **`esp32 device [PORT_NAME] [--force]`**
    Sets or tests the COM port.
    *   `esp32 device COM5`: Sets `COM5` as the active port for subsequent commands and saves it to `.esp32_deploy_config.json`. It will test the port first.
    *   `esp32 device`: If a port is already configured, it tests the connection to the configured port. If no port is configured, it lists available ports.
    *   `esp32 device COM5 --force`: Sets `COM5` even if the initial connection test fails.

    **Tip for already flashed devices**: If your device is already flashed with MicroPython and running, it should respond to the test. If `mpremote` can't connect, ensure the device isn't in a tight loop or stuck. For a new or problematic device, you might need to set the port with `--force` before flashing.

### 4.2 Flashing MicroPython Firmware

This command erases the ESP32-C3's flash and installs MicroPython firmware.

*   **`esp32 flash [firmware_source] [--baud BAUD_RATE]`**
    *   `firmware_source` (optional):
        *   If omitted, the tool attempts to download the latest known official **USB-enabled** MicroPython firmware for ESP32-C3 from `micropython.org`.
        *   You can provide a direct URL to a `.bin` file.
        *   You can provide a path to a local `.bin` firmware file.
    *   `--baud BAUD_RATE` (optional): Sets the baud rate for flashing (default: `460800`).

    **Shorthand Usage:**
    ```bash
    # Ensure device port is set first (e.g., esp32 device COM5)
    esp32 flash
    ```
    This will use the default firmware URL.

    **Important:**
    *   The device **MUST** be in **bootloader mode** for flashing. Typically:
        1.  Unplug the ESP32.
        2.  Press and **hold** the `BOOT` (or IO0) button.
        3.  While still holding `BOOT`, plug in the USB-C cable.
        4.  Wait 2-3 seconds, then release the `BOOT` button.
    *   The tool will prompt for confirmation before erasing and writing.
    *   If you have many COM ports, and you're unsure which one is the ESP32, check Windows Device Manager (under "Ports (COM & LPT)") before and after plugging in the device (in bootloader mode) to see which port appears.

### 4.3 Uploading Files and Directories

*   **`esp32 upload <local_source> [remote_destination]`**
    Uploads files or directories from your computer to the ESP32's filesystem.

    **Understanding `local_source` and trailing slashes for directories:**
    *   If `local_source` is a **file**: It's always uploaded as that single file.
    *   If `local_source` is a **directory** and ends with a `/` (or `\` on Windows, e.g., `my_dir/`): The *contents* of `my_dir` are uploaded.
    *   If `local_source` is a **directory** and does *not* end with a `/` (e.g., `my_dir`): The directory `my_dir` *itself* (including its contents) is uploaded.

    **Understanding `remote_destination`:**
    *   If omitted, the destination is the root (`/`) of the ESP32's filesystem.
    *   If provided, it specifies the target directory on the ESP32. The tool will create this directory if it doesn't exist.

    **Scenarios & Examples:**
    1.  **Upload a single file to root:**
        ```bash
        esp32 upload main.py
        # Result on ESP32: /main.py
        ```
    2.  **Upload a single file to a specific remote directory:**
        ```bash
        esp32 upload utils.py lib
        # Result on ESP32: /lib/utils.py (lib/ will be created if needed)
        ```
    3.  **Upload contents of a local directory to root:**
        ```bash
        # Assuming local_project/ contains file1.py and subdir/file2.py
        esp32 upload local_project/
        # Result on ESP32: /file1.py, /subdir/file2.py
        ```
    4.  **Upload contents of a local directory to a specific remote directory:**
        ```bash
        esp32 upload local_project/ remote_app
        # Result on ESP32: /remote_app/file1.py, /remote_app/subdir/file2.py
        ```
    5.  **Upload a local directory itself to root:**
        ```bash
        esp32 upload my_library
        # Result on ESP32: /my_library/... (contains contents of local my_library)
        ```
    6.  **Upload a local directory itself into a specific remote directory:**
        ```bash
        esp32 upload my_library existing_remote_lib_folder
        # Result on ESP32: /existing_remote_lib_folder/my_library/...
        ```

*   **`esp32 upload_all_cwd`**
    A basic command that attempts to upload all eligible files and directories from your current working directory (CWD) on your computer to the root of the ESP32. It excludes common non-project files like `.git`, `__pycache__`, etc.

    ```bash
    # From your project's directory
    esp32 upload_all_cwd
    ```

### 4.4 Downloading Files and Directories

*   **`esp32 download <remote_source_path> [local_target_path]`**
    Downloads files or directories from the ESP32 to your computer. This command behaves similarly to `upload` but in reverse.

    **Understanding `remote_source_path` and trailing slashes for directories:**
    *   If `remote_source_path` points to a **file** on the ESP32 (e.g., `/data/log.txt`): The file is downloaded.
    *   If `remote_source_path` points to a **directory** on the ESP32 and *ends with a `/`* (e.g., `/logs/`): The *contents* of that remote directory are downloaded into the specified `local_target_path`.
        *   To download the contents of the root directory, use `//` (e.g., `esp32 download // local_root_backup`).
    *   If `remote_source_path` points to a **directory** on the ESP32 and *does not end with a `/`* (e.g., `/config`): The directory `config` *itself* (including its contents) is downloaded and created within the `local_target_path`.

    **Understanding `local_target_path`:**
    *   If omitted, the download target is the current working directory (`.`) on your computer.
    *   If provided, it specifies the local directory where items will be placed or the local filename if downloading a single file to a specific name. The tool will create this directory if it doesn't exist.

    **Scenarios & Examples:**

    1.  **Download a remote file to the current local directory:**
        ```bash
        esp32 download /boot.py
        # Result: ./boot.py locally
        ```
    2.  **Download a remote file to a specific local directory, keeping its name:**
        ```bash
        esp32 download /lib/utils.py my_local_lib
        # Result: ./my_local_lib/utils.py locally (my_local_lib/ created if needed)
        ```
    3.  **Download a remote file to a specific local path and name:** (mpremote behavior for `cp :remote_file local_file_path`)
        ```bash
        esp32 download /data/sensor.dat backup/latest_sensor.dat
        # Result: ./backup/latest_sensor.dat locally
        ```
    4.  **Download a remote directory (e.g., `logs`) and its contents into the current local directory:**
        ```bash
        esp32 download /logs
        # Result: ./logs/... locally (creates a 'logs' folder in CWD)
        ```
    5.  **Download a remote directory (e.g., `data`) and its contents into a specified local directory (`backup_data`):**
        ```bash
        esp32 download /data backup_data
        # Result: ./backup_data/data/... locally
        ```
    6.  **Download the *contents* of a remote directory (e.g., `/app/`) into the current local directory:**
        ```bash
        esp32 download /app/ .
        # Result: Files and subdirectories from /app/ on device are copied into ./ locally
        # Example: if /app/main.py and /app/gfx/img.png exist,
        # they become ./main.py and ./gfx/img.png
        ```
    7.  **Download the *contents* of a remote directory (e.g., `/lib/`) into a specified local directory (`local_libs_backup`):**
        ```bash
        esp32 download /lib/ local_libs_backup
        # Result: Contents of /lib/ on device are copied into ./local_libs_backup/ locally
        # Example: if /lib/tool.py exists, it becomes ./local_libs_backup/tool.py
        ```
    8.  **Download the *contents* of the device's root directory into a local directory `full_backup`:**
        ```bash
        esp32 download // full_backup
        # Result: All files and folders from device root copied into ./full_backup/
        ```

### 4.5 Managing Remote Filesystem

*   **`esp32 list [remote_directory]`** or **`esp32 ls [remote_directory]`**
    Lists files and directories on the ESP32. The listing is recursive from the given path.
    *   `remote_directory` (optional): The directory to list (e.g., `/lib`). Defaults to the root (`/`), listing top-level items.

    **Shorthand Usage:**
    ```bash
    esp32 list
    esp32 list lib
    ```

*   **`esp32 tree [remote_directory]`**
    Displays a tree-like view of files and subdirectories within the specified remote directory.
    *   `remote_directory` (optional): Defaults to root (`/`).

    **Shorthand Usage:**
    ```bash
    esp32 tree
    esp32 tree lib
    ```

*   **`esp32 delete [remote_path_to_delete]`**
    Deletes a file or directory (recursively) on the ESP32.
    *   `remote_path_to_delete` (optional): The file or directory to delete (e.g., `old_main.py`, `temp_files/`).
    *   If omitted or set to `/`, the command will prompt for confirmation to **delete all contents of the root directory**. **Use with extreme caution!**

    **Shorthand Usage:**
    ```bash
    esp32 delete old_script.py
    esp32 delete my_module/
    esp32 delete # Prompts to wipe root
    esp32 delete / # Also prompts to wipe root
    ```

### 4.6 Running Scripts

*   **`esp32 run [script_name]`**
    Executes a MicroPython script that exists on the ESP32's filesystem.
    *   `script_name` (optional): The path to the script on the device (e.g., `app.py`, `tests/run_tests.py`). Defaults to `main.py`. Path is relative to the device root.
    The script's output (and any errors) will be displayed in your terminal.

    **Shorthand Usage:**
    ```bash
    esp32 run
    # Executes /main.py on device

    esp32 run services/scanner.py
    # Executes /services/scanner.py on device
    ```

## 5. Troubleshooting

*   **Connection Issues / Device Not Detected:**
    *   Ensure the USB-C cable supports data transfer (not just charging).
    *   Verify the correct COM port is selected (`esp32 devices`, `esp32 device <PORT>`).
    *   For flashing or if the device is unresponsive, make sure it's in **bootloader mode**. See Section 4.2 or `docs_md/identify_board.md`.
    *   Check if other serial terminal programs (Arduino IDE Serial Monitor, PuTTY, etc.) are holding the port open. Close them.

*   **`esptool` or `mpremote` command not found:**
    *   Make sure `esptool` and `mpremote` are installed: `pip install esptool mpremote pyserial`.
    *   Ensure your Python scripts directory is in your system's PATH environment variable.

*   **Firmware Flashed, but Device Unresponsive or `test_device` Fails:**
    *   The default firmware URL points to a generic ESP32-C3 USB-enabled build. While it works for many "SuperMini" clones, some ESP32-C3 boards might require a specific version or a build with different options.
    *   **Try finding an alternative official MicroPython `.bin` file for ESP32-C3** from [micropython.org/download/esp32c3/](https://micropython.org/download/esp32c3/) that matches your board's specifications (e.g., flash size, specific features if known).
    *   Then, use the `flash` command with the path to your downloaded file:
        ```bash
        esp32 flash path/to/your_downloaded_firmware.bin
        ```
    *   After flashing, physically reset the device (unplug/replug or RST button) before testing.

*   **Upload/Download/List commands fail with "No response or mpremote error":**
    *   Ensure MicroPython is running correctly on the device. Try `esp32 device` to test basic connectivity.
    *   If the device was just flashed, it might need a manual reset.
    *   The MicroPython script on the device might be stuck in an infinite loop or has crashed. Try resetting the board and connecting again quickly.

---

This utility aims to streamline your ESP32-C3 MicroPython development workflow. Happy coding!
