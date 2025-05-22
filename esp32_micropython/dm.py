# File: esp32_deploy_manager/dm.py

#!/usr/bin/env python3
"""
esp32_deploy_manager (dm.py)

Manage deployment of MicroPython files to an ESP32-C3 via mpremote.
Includes functionality to flash MicroPython firmware using esptool.

Usage:
  esp32 [--port PORT] <command> [<args>...]
"""
from pathlib import Path
import json
import os
import subprocess
import argparse
import sys
import serial.tools.list_ports
import urllib.request
import tempfile
import shutil
import re # Added for parsing stat output
import time # Added for delays

CONFIG_FILE = Path(__file__).parent / ".esp32_deploy_config.json"
DEVICE_PORT = None # Will be set by main after parsing args or loading config
DEFAULT_FIRMWARE_URL = "https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20250415-v1.25.0.bin"

# Constants for file modes (from uos.stat results)
S_IFDIR = 0x4000  # Directory
S_IFREG = 0x8000  # Regular file

# Constants for file operations and timeouts
FS_OPERATION_DELAY = 0.3  # Delay in seconds between filesystem operations on the ESP32
MP_TIMEOUT_EXEC = 20      # Timeout for general exec commands (incl. uos.stat)
MP_TIMEOUT_LS_EXEC = 45   # Increased timeout for exec-based listing, can be slow for many files
MP_TIMEOUT_LS_MPREMOTE = 20 # For mpremote fs ls, fs ls -r (if we revert to it)
MP_TIMEOUT_MKDIR = 15
MP_TIMEOUT_CP_FILE = 120  # Timeout for copying a single file
MP_TIMEOUT_RM = 60        # Timeout for mpremote fs rm -r
MP_TIMEOUT_DF = 10

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            print(f"Warning: Config file {CONFIG_FILE} is corrupted. Using defaults.", file=sys.stderr)
    return {}

def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except IOError as e:
        print(f"Error saving config file {CONFIG_FILE}: {e}", file=sys.stderr)

def list_ports():
    return list(serial.tools.list_ports.comports())

def run_mpremote_command(mpremote_args_list, connect_port=None, suppress_output=False, timeout=None, working_dir=None):
    global DEVICE_PORT
    port_to_use = connect_port or DEVICE_PORT
    if not port_to_use:
        print("Error: Device port not set for mpremote command.", file=sys.stderr)
        return subprocess.CompletedProcess(mpremote_args_list, -99, stdout="", stderr="Device port not set")

    base_cmd = ["mpremote", "connect", port_to_use]
    full_cmd = base_cmd + mpremote_args_list
    # print(f"DEBUG: Running mpremote: {' '.join(full_cmd)}", file=sys.stderr)

    try:
        if suppress_output:
            process = subprocess.run(full_cmd, capture_output=True, text=True, check=False, timeout=timeout, cwd=working_dir)
        else:
            process = subprocess.run(full_cmd, text=True, check=False, timeout=timeout, cwd=working_dir)
        return process
    except FileNotFoundError:
        print("Error: mpremote command not found. Is it installed and in PATH?", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(full_cmd, -1, stdout="", stderr=f"TimeoutExpired ({timeout}s) executing mpremote")
    except Exception as e:
        return subprocess.CompletedProcess(full_cmd, -2, stdout="", stderr=f"Unexpected error: {e}")

def run_esptool_command(esptool_args_list, suppress_output=False, timeout=None, working_dir=None):
    base_cmd = ["esptool"]
    full_cmd = base_cmd + esptool_args_list
    try:
        if suppress_output:
            process = subprocess.run(full_cmd, capture_output=True, text=True, check=False, timeout=timeout, cwd=working_dir)
        else:
            process = subprocess.run(full_cmd, text=True, check=False, timeout=timeout, cwd=working_dir)
        return process
    except FileNotFoundError:
        print("Error: esptool command not found. Is it installed and in PATH? (esptool is required for flashing).", file=sys.stderr)
        sys.exit(1) 
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(full_cmd, -1, stdout="", stderr=f"TimeoutExpired ({timeout}s) executing esptool")
    except Exception as e:
        return subprocess.CompletedProcess(full_cmd, -2, stdout="", stderr=f"Unexpected error: {e}")

def get_remote_path_stat(target_path_on_device):
    """
    Gets the type ('file', 'dir', 'unknown', or None) of a remote path using `mpremote exec uos.stat()`.
    target_path_on_device: Path string relative to root (e.g., "main.py", "lib/foo", "")
    Returns a string: "file", "dir", "unknown", or None if not found/error.
    """
    global DEVICE_PORT
    if not DEVICE_PORT:
        return None

    if not target_path_on_device or target_path_on_device.strip() == "/":
        path_for_uos = "/"
    else:
        path_for_uos = f"/{target_path_on_device.strip('/')}"
    
    escaped_path_for_uos = path_for_uos.replace("'", "\\'")
    code = f"import uos; print(uos.stat('{escaped_path_for_uos}'))"
    
    result = run_mpremote_command(["exec", code], suppress_output=True, timeout=MP_TIMEOUT_EXEC)

    if result and result.returncode == 0 and result.stdout:
        stat_tuple_str = result.stdout.strip()
        try:
            if stat_tuple_str.startswith("(") and stat_tuple_str.endswith(")"):
                numbers = re.findall(r'-?\d+', stat_tuple_str)
                if not numbers: return None
                mode = int(numbers[0])
                if (mode & S_IFDIR) == S_IFDIR: return "dir"
                elif (mode & S_IFREG) == S_IFREG: return "file"
                else: return "unknown"
            else: return None
        except (IndexError, ValueError): return None
    elif result and result.stderr and ("ENOENT" in result.stderr or "No such file or directory" in result.stderr):
        return None 
    
    return None


def cmd_devices():
    cfg = load_config()
    selected_port = cfg.get("port")
    available_ports = list_ports()
    if not available_ports:
        print("No serial ports found.")
        return

    print("Available COM ports:")
    for p in available_ports:
        marker = "*" if p.device == selected_port else ""
        print(f"  {marker}{p.device}{marker} - {p.description}")

    if selected_port and selected_port not in [p.device for p in available_ports]:
        print(f"\nWarning: The selected COM port '{selected_port}' is not available. Please reconfigure.")
    elif not selected_port:
        print(f"\nNo COM port selected. Use 'esp32 device <PORT_NAME>' to set one.")
    else:
        print(f"\nSelected COM port: {selected_port} (use 'esp32 device <PORT_NAME>' to change it).")

def test_device(port, timeout=MP_TIMEOUT_LS_MPREMOTE): # Using direct mpremote ls for test
    result = run_mpremote_command(["fs", "ls", ":"], connect_port=port, suppress_output=True, timeout=timeout)
    time.sleep(FS_OPERATION_DELAY / 2) 
    if result and result.returncode == 0:
        return True, f"Device on {port} responded (mpremote fs ls successful)."
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "No response or mpremote error."
        if result and result.returncode == -99: err_msg = result.stderr 
        suggestion = (
            "Ensure the device is properly connected (try holding BOOT while plugging in, then release BOOT after a few seconds) "
            "and flashed with MicroPython. You can use the 'esp32 flash <firmware_file_or_url>' command to flash it."
        )
        return False, f"No response or error on {port}. Details: {err_msg}\n{suggestion}"

def test_micropython_presence(port, timeout=MP_TIMEOUT_EXEC):
    global DEVICE_PORT 
    port_to_test = port or DEVICE_PORT
    if not port_to_test:
        return False, "Device port not set for MicroPython presence test."

    code_to_run = "import sys; print(sys.implementation.name)"
    print(f"Verifying MicroPython presence on {port_to_test}...")
    result = run_mpremote_command(["exec", code_to_run], connect_port=port_to_test, suppress_output=True, timeout=timeout)
    time.sleep(FS_OPERATION_DELAY / 2) 
    
    if result and result.returncode == 0 and result.stdout:
        output_name = result.stdout.strip().lower()
        if "micropython" in output_name:
            return True, f"MicroPython confirmed on {port_to_test} (sys.implementation.name: '{output_name}')."
        else:
            return False, f"Connected to {port_to_test}, but unexpected response for MicroPython check: {result.stdout.strip()}"
    elif result and result.returncode == -99: 
         return False, f"Failed to query MicroPython presence: {result.stderr}"
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "No response or mpremote error during verification."
        return False, f"Failed to query MicroPython presence on {port_to_test}. Details: {err_msg}"

def cmd_device(port_arg, force=False):
    global DEVICE_PORT
    available = [p.device for p in list_ports()]
    if port_arg not in available:
        print(f"Error: Port {port_arg} not found among available ports: {', '.join(available) if available else 'None'}", file=sys.stderr)
        sys.exit(1)
    
    ok, result_msg = test_device(port_arg) 
    print(result_msg)
    
    if not ok and not force:
        print(f"Device test failed. To set {port_arg} anyway, use --force.", file=sys.stderr)
        sys.exit(1)
        
    cfg = load_config()
    cfg["port"] = port_arg
    save_config(cfg)
    DEVICE_PORT = port_arg 
    if ok:
        print(f"Selected COM port set to {port_arg}.")
    else:
        print(f"Selected COM port set to {port_arg} (forced).")

def ensure_remote_dir(remote_dir_to_create):
    global DEVICE_PORT
    if not DEVICE_PORT:
        print("Error: Device port not set. Cannot ensure remote directory.", file=sys.stderr)
        return False

    normalized_path = remote_dir_to_create.strip("/")
    if not normalized_path: 
        return True

    parts = Path(normalized_path).parts
    current_remote_path_str = ""

    for part in parts:
        if not current_remote_path_str:
            current_remote_path_str = part
        else:
            current_remote_path_str = f"{current_remote_path_str}/{part}"
        
        path_type_check = get_remote_path_stat(current_remote_path_str)
        time.sleep(FS_OPERATION_DELAY / 4)

        if path_type_check == "dir":
            continue 
        if path_type_check == "file":
            print(f"Error: Remote path ':{current_remote_path_str}' exists and is a file, cannot create directory.", file=sys.stderr)
            return False
        
        print(f"    Creating remote directory component ':{current_remote_path_str}'...")
        result = run_mpremote_command(["fs", "mkdir", f":{current_remote_path_str}"], suppress_output=True, timeout=MP_TIMEOUT_MKDIR)
        time.sleep(FS_OPERATION_DELAY) 

        if result and result.returncode == 0:
            # print(f"    Created remote directory component ':{current_remote_path_str}'") # Redundant with above print
            pass
        elif result and result.stderr and ("EEXIST" in result.stderr or "File exists" in result.stderr):
            path_type_check_after_mkdir = get_remote_path_stat(current_remote_path_str)
            time.sleep(FS_OPERATION_DELAY / 4) 
            if path_type_check_after_mkdir == "dir":
                continue 
            else:
                err_msg = f"Path ':{current_remote_path_str}' exists but is not a directory (it is {path_type_check_after_mkdir or 'of an unknown type'})."
                print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
                return False
        else:
            err_msg = result.stderr.strip() if result and result.stderr else f"Unknown error creating ':{current_remote_path_str}'"
            if result and not err_msg and result.stdout: 
                 err_msg = result.stdout.strip()
            print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
            return False
            
    return True

def cmd_upload(local_src_arg, remote_dest_arg=None):
    global DEVICE_PORT
    
    had_trailing_slash_local = local_src_arg.endswith(("/", os.sep))
    original_local_src_display = local_src_arg
    local_src_for_path_obj = local_src_arg
    if had_trailing_slash_local:
        local_src_for_path_obj = local_src_arg.rstrip("/" + os.sep)
        if not local_src_for_path_obj and Path(original_local_src_display).is_absolute():
             local_src_for_path_obj = original_local_src_display
        if not local_src_for_path_obj and original_local_src_display in (".", "./", ".\\"):
            local_src_for_path_obj = "."

    abs_local_path = Path(os.path.abspath(local_src_for_path_obj))

    if not abs_local_path.exists():
        print(f"Error: Local path '{original_local_src_display}' (resolved to '{abs_local_path}') does not exist.", file=sys.stderr)
        sys.exit(1)
    
    is_local_file = abs_local_path.is_file()
    is_local_dir = abs_local_path.is_dir()

    if not is_local_file and not is_local_dir:
        print(f"Error: Local path '{original_local_src_display}' is neither a file nor a directory.", file=sys.stderr)
        sys.exit(1)

    effective_remote_parent_dir_str = ""
    if remote_dest_arg:
        effective_remote_parent_dir_str = remote_dest_arg.replace(os.sep, "/").strip("/")

    if is_local_file:
        if had_trailing_slash_local: 
            print(f"Warning: Trailing slash on a local file path '{original_local_src_display}' is ignored. Treating as file '{abs_local_path.name}'.")

        if effective_remote_parent_dir_str:
            print(f"Ensuring remote target directory ':{effective_remote_parent_dir_str}' exists...")
            if not ensure_remote_dir(effective_remote_parent_dir_str): 
                sys.exit(1)

        local_file_basename = abs_local_path.name
        mpremote_target_path_on_device = f":{effective_remote_parent_dir_str}/{local_file_basename}" if effective_remote_parent_dir_str else f":{local_file_basename}"
        
        print(f"Uploading file '{abs_local_path}' to '{mpremote_target_path_on_device}' on device...")
        cp_args = ["fs", "cp", str(abs_local_path).replace(os.sep, '/'), mpremote_target_path_on_device]
        result = run_mpremote_command(cp_args, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
        time.sleep(FS_OPERATION_DELAY) 
        
        if result and result.returncode == 0:
            print("File upload complete.")
        else:
            err_msg = result.stderr.strip() if result and result.stderr else "File upload failed"
            if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
            print(f"Error uploading file '{original_local_src_display}': {err_msg}", file=sys.stderr)
            sys.exit(1)

    elif is_local_dir:
        remote_base_for_items_str: str 
        
        if had_trailing_slash_local:
            remote_base_for_items_str = effective_remote_parent_dir_str
            print(f"Uploading contents of local directory '{abs_local_path}' to ':{remote_base_for_items_str or '/'}' on device...")
            if remote_base_for_items_str:
                print(f"Ensuring remote base directory ':{remote_base_for_items_str}' exists...")
                if not ensure_remote_dir(remote_base_for_items_str): sys.exit(1)
        else:
            src_dir_name = abs_local_path.name
            remote_base_for_items_str = f"{effective_remote_parent_dir_str}/{src_dir_name}".strip("/") if effective_remote_parent_dir_str else src_dir_name
            print(f"Uploading local directory '{abs_local_path}' as ':{remote_base_for_items_str}' on device...")
            
            if effective_remote_parent_dir_str:
                print(f"Ensuring remote parent directory ':{effective_remote_parent_dir_str}' exists...")
                if not ensure_remote_dir(effective_remote_parent_dir_str): sys.exit(1)

            print(f"Ensuring remote target directory ':{remote_base_for_items_str}' exists...")
            if not ensure_remote_dir(remote_base_for_items_str): sys.exit(1)

        files_uploaded_count = 0
        for root, dirs, files in os.walk(str(abs_local_path)):
            root_path = Path(root)
            relative_dir_path_from_src = root_path.relative_to(abs_local_path)
            current_remote_target_dir_str = remote_base_for_items_str
            if str(relative_dir_path_from_src) != ".":
                current_remote_target_dir_str = f"{remote_base_for_items_str}/{relative_dir_path_from_src.as_posix()}" if remote_base_for_items_str else relative_dir_path_from_src.as_posix()

            for dir_name in sorted(dirs):
                remote_subdir_to_ensure = Path(current_remote_target_dir_str) / dir_name
                # No print here, ensure_remote_dir will print if it creates something
                if not ensure_remote_dir(remote_subdir_to_ensure.as_posix()):
                    print(f"    Failed to create remote subdirectory ':{remote_subdir_to_ensure.as_posix()}'. Skipping its contents.", file=sys.stderr)
                    dirs.remove(dir_name) # Don't try to recurse into it

            for file_name in sorted(files):
                local_file_full_path = root_path / file_name
                remote_file_target_on_device_str = f":{current_remote_target_dir_str}/{file_name}" if current_remote_target_dir_str else f":{file_name}"

                print(f"  Uploading '{local_file_full_path.relative_to(abs_local_path)}' to '{remote_file_target_on_device_str}'...")
                cp_args_file = ["fs", "cp", str(local_file_full_path).replace(os.sep, '/'), remote_file_target_on_device_str]
                result_file = run_mpremote_command(cp_args_file, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
                time.sleep(FS_OPERATION_DELAY) 
                
                if result_file and result_file.returncode == 0:
                    files_uploaded_count += 1
                else:
                    err_msg = result_file.stderr.strip() if result_file and result_file.stderr else "File upload failed"
                    if result_file and not err_msg and result_file.stdout: err_msg = result_file.stdout.strip()
                    print(f"    Error uploading file '{local_file_full_path.relative_to(abs_local_path)}': {err_msg}", file=sys.stderr)

        print(f"Directory upload processed. {files_uploaded_count} files uploaded.")
    else: 
        print(f"Error: Unhandled local source type for '{original_local_src_display}'.", file=sys.stderr)
        sys.exit(1)

def cmd_download(remote_src_arg, local_dest_arg=None):
    global DEVICE_PORT
    had_trailing_slash_remote = remote_src_arg.endswith("/")
    
    path_for_stat = ""
    if remote_src_arg == "/" or remote_src_arg == "//":
        path_for_stat = "" 
    else:
        path_for_stat = remote_src_arg.strip("/")

    if remote_src_arg == "/" and not had_trailing_slash_remote and path_for_stat == "": 
        print("Error: Ambiguous command 'download /'.", file=sys.stderr)
        print("  To download contents of the root directory, use 'download // [local_path]' or 'download / [local_path/]'.", file=sys.stderr)
        print("  To download a specific item from root, use 'download /item_name [local_path]'.", file=sys.stderr)
        sys.exit(1)

    print(f"Checking remote path ':{path_for_stat or '/'}'...")
    remote_type = get_remote_path_stat(path_for_stat)
    time.sleep(FS_OPERATION_DELAY / 2) 
    
    if remote_type is None:
        print(f"Error: Remote path ':{path_for_stat or '/'}' not found or not accessible on device.", file=sys.stderr)
        sys.exit(1)

    if remote_type == "file":
        remote_basename = Path(path_for_stat).name 
        local_target_path_obj: Path
        if local_dest_arg:
            local_dest_path_obj = Path(os.path.abspath(local_dest_arg))
            if local_dest_arg.endswith(("/", os.sep)) or local_dest_path_obj.is_dir():
                local_dest_path_obj.mkdir(parents=True, exist_ok=True)
                local_target_path_obj = local_dest_path_obj / remote_basename
            else: 
                local_dest_path_obj.parent.mkdir(parents=True, exist_ok=True)
                local_target_path_obj = local_dest_path_obj
        else: 
            local_target_path_obj = Path.cwd() / remote_basename

        final_mpremote_local_dest_str = str(local_target_path_obj).replace(os.sep, '/')
        # For mpremote fs cp, source path needs to be :/path_from_root for non-root files
        mpremote_remote_source_str = f":/{path_for_stat}" if path_for_stat else ":" # For root files, if any could exist (not typical)

        print(f"Downloading remote file '{mpremote_remote_source_str}' to local path '{final_mpremote_local_dest_str}'...")
        cp_args = ["fs", "cp", mpremote_remote_source_str, final_mpremote_local_dest_str]
        result = run_mpremote_command(cp_args, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
        time.sleep(FS_OPERATION_DELAY) 
        
        if result and result.returncode == 0:
            print("File download complete.")
        else:
            err_parts = []
            if result and result.stdout: err_parts.append(result.stdout.strip())
            if result and result.stderr: err_parts.append(result.stderr.strip())
            err_msg = "; ".join(filter(None, err_parts))
            if not err_msg : err_msg = f"File download failed with mpremote exit code {result.returncode if result else 'N/A'}"
            print(f"Error downloading from '{mpremote_remote_source_str}': {err_msg}", file=sys.stderr)
            sys.exit(1)

    elif remote_type == "dir":
        local_base_dir_for_items: Path
        
        if had_trailing_slash_remote or (remote_src_arg in ["/", "//"] and path_for_stat == ""):
            local_base_dir_for_items = Path(os.path.abspath(local_dest_arg or "."))
            print(f"Downloading contents of remote directory ':{path_for_stat or '/'}' to local directory '{local_base_dir_for_items}'...")
        else:
            remote_dir_name = Path(path_for_stat).name
            parent_local_dir = Path(os.path.abspath(local_dest_arg or "."))
            local_base_dir_for_items = parent_local_dir / remote_dir_name
            print(f"Downloading remote directory ':{path_for_stat}' as local directory '{local_base_dir_for_items}'...")

        local_base_dir_for_items.mkdir(parents=True, exist_ok=True)
        time.sleep(FS_OPERATION_DELAY / 4) 

        print(f"  Fetching file list from remote ':{path_for_stat or '/'}'...")
        all_remote_items_abs = list_remote_capture(path_for_stat) 
        time.sleep(FS_OPERATION_DELAY) 

        if not all_remote_items_abs:
            is_valid_dir_check = get_remote_path_stat(path_for_stat)
            time.sleep(FS_OPERATION_DELAY / 2) 
            if is_valid_dir_check == 'dir':
                 print(f"Remote directory ':{path_for_stat or '/'}' is empty. Nothing to download.")
                 return
            else:
                 print(f"Failed to list contents or ':{path_for_stat or '/'}' is not a directory (type: {is_valid_dir_check}).", file=sys.stderr)
                 sys.exit(1)

        base_remote_path_obj = Path("/") / path_for_stat

        files_downloaded_count = 0
        dirs_created_count = 0
        
        processed_items = [] 
        
        for remote_item_abs_str in sorted(list(set(all_remote_items_abs))):
            try:
                relative_to_download_base = Path(remote_item_abs_str).relative_to(base_remote_path_obj)
            except ValueError:
                if str(base_remote_path_obj) == "/":
                     relative_to_download_base = Path(remote_item_abs_str.lstrip('/'))
                else:
                    continue 
            local_target_path = local_base_dir_for_items / relative_to_download_base
            
            # Check type of remote_item_abs_str
            # remote_item_abs_str from list_remote_capture includes trailing / for dirs.
            # get_remote_path_stat expects path without leading / and without trailing / for its internal logic.
            item_type = get_remote_path_stat(remote_item_abs_str.lstrip('/').rstrip('/'))
            time.sleep(FS_OPERATION_DELAY / 4) 

            if item_type == "dir":
                processed_items.append((True, remote_item_abs_str, local_target_path))
            elif item_type == "file":
                processed_items.append((False, remote_item_abs_str, local_target_path))

        for is_dir, remote_abs_path_str, local_target_path_obj in processed_items:
            if is_dir:
                print(f"  Ensuring local directory '{local_target_path_obj}' exists...")
                local_target_path_obj.mkdir(parents=True, exist_ok=True)
                dirs_created_count += 1
            else: 
                local_target_path_obj.parent.mkdir(parents=True, exist_ok=True)
                mpremote_remote_source_for_file = ":" + remote_abs_path_str.lstrip('/')
                
                print(f"  Downloading remote file '{mpremote_remote_source_for_file}' to '{local_target_path_obj}'...")
                cp_args_file = ["fs", "cp", mpremote_remote_source_for_file, str(local_target_path_obj).replace(os.sep, '/')]
                result_file = run_mpremote_command(cp_args_file, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
                time.sleep(FS_OPERATION_DELAY) 
                
                if result_file and result_file.returncode == 0:
                    files_downloaded_count += 1
                else:
                    err_msg = result_file.stderr.strip() if result_file and result_file.stderr else "File download failed"
                    if result_file and not err_msg and result_file.stdout: err_msg = result_file.stdout.strip()
                    print(f"    Error downloading file '{mpremote_remote_source_for_file}': {err_msg}", file=sys.stderr)

        print(f"Directory download processed. {dirs_created_count} local directories created/ensured, {files_downloaded_count} files downloaded.")
    else: 
        print(f"Error: Unhandled remote source type '{remote_type}' for ':{path_for_stat or '/'}'.", file=sys.stderr)
        sys.exit(1)


def run_script(script="main.py"):
    global DEVICE_PORT
    script_on_device_norm = script.strip('/') 
    
    print(f"Checking for '{script_on_device_norm}' on device...")
    path_type = get_remote_path_stat(script_on_device_norm)
    time.sleep(FS_OPERATION_DELAY / 2) 

    if path_type is None:
        print(f"Error: Script ':{script_on_device_norm}' not found on device.", file=sys.stderr)
        sys.exit(1)
    if path_type == 'dir':
        print(f"Error: Path ':{script_on_device_norm}' on device is a directory, not a runnable script.", file=sys.stderr)
        sys.exit(1)
    if path_type != 'file':
        print(f"Error: Path ':{script_on_device_norm}' on device is not a file (type: {path_type}).", file=sys.stderr)
        sys.exit(1)

    abs_script_path_on_device = f"/{script_on_device_norm}" 
    escaped_script_path_for_exec = abs_script_path_on_device.replace("'", "\\'")
    python_code = f"exec(open('{escaped_script_path_for_exec}').read())"
    
    print(f"Running '{script_on_device_norm}' on {DEVICE_PORT}...")
    result = run_mpremote_command(["exec", python_code], suppress_output=False, timeout=None) 
    if result and result.returncode != 0:
        pass


def list_remote_capture(remote_dir_arg=""): 
    global DEVICE_PORT
    if not DEVICE_PORT: return []

    if not remote_dir_arg or remote_dir_arg.strip() == "/":
        path_for_walk_start = "/"
    else:
        path_for_walk_start = f"/{remote_dir_arg.strip('/')}"
    
    escaped_path_for_walk_start = path_for_walk_start.replace("'", "\\'")
    code = f"""\\
import uos
def _walk(p):
    try:
        current_path_prefix = p if p == '/' else p.rstrip('/') + '/'
        # For uos.ilistdir, root is '', not '/' for some versions. Let's try '' for root.
        items_path_for_ilistdir = '' if p == '/' else p 
        items = uos.ilistdir(items_path_for_ilistdir)
    except OSError as e:
        return
    for item_info in items:
        name = item_info[0]
        typ = item_info[1]
        item_full_path = current_path_prefix + name
        if typ == {S_IFDIR}: 
            print(item_full_path + '/') 
            _walk(item_full_path)
        elif typ == {S_IFREG}: 
            print(item_full_path)
_walk('{escaped_path_for_walk_start}')
"""
    result = run_mpremote_command(["exec", code], suppress_output=True, timeout=MP_TIMEOUT_LS_EXEC)
    
    if result and result.returncode == 0 and result.stdout:
        return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith('/')]
    elif result and result.stderr:
        if not ("No such file or directory" in result.stderr or "ENOENT" in result.stderr):
             print(f"Error during remote listing execution for '{path_for_walk_start}': {result.stderr.strip()}", file=sys.stderr)
    return []


def list_remote(remote_dir=None):
    global DEVICE_PORT
    normalized_remote_dir_str = (remote_dir or "").strip("/") 
    display_dir_name = f":{normalized_remote_dir_str or '/'}"
    
    if normalized_remote_dir_str: 
        path_type = get_remote_path_stat(normalized_remote_dir_str)
        time.sleep(FS_OPERATION_DELAY / 4) 
        if path_type is None:
            print(f"Error: Remote path '{display_dir_name}' not found.", file=sys.stderr)
            return
        if path_type != 'dir':
            print(f"Error: '{display_dir_name}' is a {path_type}, not a directory. Use 'download' for files.", file=sys.stderr)
            return
    
    print(f"Listing contents of '{display_dir_name}' (recursive, using on-device uos.ilistdir)...")
    all_paths_abs = list_remote_capture(normalized_remote_dir_str) 
    time.sleep(FS_OPERATION_DELAY / 2) 

    if not all_paths_abs:
        is_valid_empty_dir = False
        if not normalized_remote_dir_str: is_valid_empty_dir = True 
        else:
            path_type_check = get_remote_path_stat(normalized_remote_dir_str)
            time.sleep(FS_OPERATION_DELAY / 4) 
            if path_type_check == 'dir': is_valid_empty_dir = True
        
        if is_valid_empty_dir: print(f"Directory '{display_dir_name}' is empty.")
        else: print(f"Directory '{display_dir_name}' is empty or no items could be listed.")
        return
    
    base_display_path = Path("/") / normalized_remote_dir_str
    displayed_count = 0
    for path_str_abs in sorted(all_paths_abs):
        path_obj_abs = Path(path_str_abs)
        try:
            relative_path_for_display = path_obj_abs.relative_to(base_display_path)
            path_to_print = str(relative_path_for_display)
            if path_str_abs.endswith('/') and not path_to_print.endswith('/') and path_to_print != ".":
                path_to_print += "/"
            if path_to_print == "." and not path_str_abs.endswith('/'): 
                continue
            if path_to_print == "." and path_str_abs.endswith('/') and normalized_remote_dir_str: 
                path_to_print = "./" 
            print(path_to_print)
            displayed_count +=1
        except ValueError: 
            print(path_str_abs) 
            displayed_count +=1
    if displayed_count == 0 and all_paths_abs: 
        print(f"Directory '{display_dir_name}' contains no listable sub-items after filtering.")

def tree_remote(remote_dir=None):
    global DEVICE_PORT
    base_path_str_norm = (remote_dir or "").strip("/") 
    display_root_name = f":{base_path_str_norm or '/'}"

    if base_path_str_norm: 
        path_type = get_remote_path_stat(base_path_str_norm)
        time.sleep(FS_OPERATION_DELAY / 4)
        if path_type is None:
            print(f"Error: Remote directory '{display_root_name}' not found.", file=sys.stderr)
            return
        if path_type != 'dir':
            print(f"Error: '{display_root_name}' is a {path_type}. Cannot display as tree.", file=sys.stderr)
            return
    
    print(f"Tree for '{display_root_name}' on device (using on-device uos.ilistdir):")
    lines_abs = list_remote_capture(base_path_str_norm)
    time.sleep(FS_OPERATION_DELAY / 2)
    
    if not lines_abs:
        is_valid_empty_dir = False
        if not base_path_str_norm: is_valid_empty_dir = True
        else:
            if get_remote_path_stat(base_path_str_norm) == 'dir': is_valid_empty_dir = True
            time.sleep(FS_OPERATION_DELAY / 4)
        if is_valid_empty_dir: print(f"Directory '{display_root_name}' is empty.")
        else: print(f"Could not retrieve contents for '{display_root_name}'.")
        return

    structure = {}
    tree_base_path_obj = Path("/") / base_path_str_norm

    for abs_path_str in lines_abs:
        is_dir_node = abs_path_str.endswith('/')
        path_obj_for_parts = Path(abs_path_str.rstrip('/')) # Use rstripped for parts
        
        try:
            relative_path = path_obj_for_parts.relative_to(tree_base_path_obj)
        except ValueError:
            if str(tree_base_path_obj) == "/":
                relative_path = path_obj_for_parts # Path objects from / already 'relative'
            else:
                continue
        
        if str(relative_path) == ".": # Skip the base directory itself in the tree structure
            continue

        current_level = structure
        parts = relative_path.parts
        
        for i, part_name in enumerate(parts):
            is_last_part = (i == len(parts) - 1)
            # Node name for the dictionary key, includes trailing / if it's a directory
            node_key = part_name + ("/" if is_last_part and is_dir_node else "")
            
            if is_last_part:
                current_level.setdefault(node_key, {}) 
            else:
                # Intermediate parts must be directories, ensure key reflects this
                dir_key_for_intermediate = part_name + "/"
                current_level = current_level.setdefault(dir_key_for_intermediate, {})
            
    def print_tree_nodes(node_dict, prefix=""):
        children_names = sorted(node_dict.keys(), key=lambda k: (not k.endswith('/'), k.lower()))
        for i, child_name_with_type in enumerate(children_names):
            connector = "└── " if i == len(children_names) - 1 else "├── "
            print(f"{prefix}{connector}{child_name_with_type.rstrip('/')}") # Print without trailing slash for neatness
            if node_dict[child_name_with_type]: 
                new_prefix = prefix + ("    " if i == len(children_names) - 1 else "│   ")
                print_tree_nodes(node_dict[child_name_with_type], new_prefix)

    if not base_path_str_norm: print(".") 
    else: print(f"{Path(base_path_str_norm).name}/") 
    print_tree_nodes(structure)


def delete_remote(remote_path_arg):
    global DEVICE_PORT
    is_root_delete_all_contents = remote_path_arg is None or remote_path_arg.strip() in ["", "/"]
    
    if is_root_delete_all_contents:
        print("WARNING: You are about to delete all files and directories from the root of the device.")
        confirm = input("Are you sure? Type 'yes' to proceed: ")
        if confirm.lower() != 'yes':
            print("Operation cancelled.")
            return
        
        print("Fetching root directory contents for deletion...")
        # Get direct children of root. list_remote_capture("") gives all descendants.
        # We need to delete top-level items first. If a dir is deleted, its contents go with it.
        # Using mpremote fs ls for direct children is fine here.
        ls_result = run_mpremote_command(["fs", "ls", ":"], suppress_output=True, timeout=MP_TIMEOUT_LS_MPREMOTE)
        time.sleep(FS_OPERATION_DELAY /2)

        if not ls_result or ls_result.returncode != 0:
            err = ls_result.stderr.strip() if ls_result and ls_result.stderr else "Failed to list root."
            print(f"Error listing root directory for deletion: {err}", file=sys.stderr)
            sys.exit(1)
        
        items_to_delete_from_root_names = []
        if ls_result.stdout:
            for line in ls_result.stdout.splitlines():
                line_content = line.strip()
                if not line_content or line_content.lower().startswith("ls ") or line_content == ":" or line_content == "stat":
                    continue
                parts = line_content.split(maxsplit=1)
                item_name = parts[1] if len(parts) == 2 and parts[0].isdigit() else line_content
                if item_name and item_name != '/': 
                    items_to_delete_from_root_names.append(item_name.rstrip('/'))
        
        if not items_to_delete_from_root_names:
            print("Root directory is already empty or no valid items found by ls.")
            return

        print(f"Top-level items to delete from root: {items_to_delete_from_root_names}")
        all_successful = True
        for item_name_to_delete in items_to_delete_from_root_names:
            # mpremote fs rm -r :item_name
            item_target_for_mpremote = ":" + item_name_to_delete
            
            print(f"Deleting '{item_target_for_mpremote}'...")
            del_result = run_mpremote_command(
                ["fs", "rm", "-r", item_target_for_mpremote], 
                suppress_output=True, 
                timeout=MP_TIMEOUT_RM 
            )
            time.sleep(FS_OPERATION_DELAY) 
            if del_result and del_result.returncode == 0:
                pass 
            else:
                all_successful = False
                err_msg = del_result.stderr.strip() if del_result and del_result.stderr else "Deletion failed"
                if del_result and not err_msg and del_result.stdout: err_msg = del_result.stdout.strip()
                print(f"  Error deleting '{item_target_for_mpremote}': {err_msg}", file=sys.stderr)
        
        if all_successful: print("Deletion of root contents complete.")
        else:
            print("Deletion of root contents attempted, but some errors occurred.", file=sys.stderr)
            sys.exit(1) 
    else: 
        normalized_path_for_stat = remote_path_arg.strip('/') 
        mpremote_target_path = ":" + normalized_path_for_stat
        
        path_type = get_remote_path_stat(normalized_path_for_stat)
        time.sleep(FS_OPERATION_DELAY / 2)

        if path_type is None:
            print(f"Error: Remote path '{mpremote_target_path}' does not exist on device.", file=sys.stderr)
            sys.exit(1) 
            
        print(f"Deleting '{mpremote_target_path}' (type detected: {path_type})...")
        del_args = ["fs", "rm", "-r", mpremote_target_path] 
        del_result = run_mpremote_command(del_args, suppress_output=True, timeout=MP_TIMEOUT_RM)
        time.sleep(FS_OPERATION_DELAY)
        
        if del_result and del_result.returncode == 0:
            print(f"Deleted '{mpremote_target_path}'.")
        else:
            err_msg = del_result.stderr.strip() if del_result and del_result.stderr else "Deletion failed"
            if del_result and not err_msg and del_result.stdout: err_msg = del_result.stdout.strip()
            print(f"Error deleting '{mpremote_target_path}': {err_msg}", file=sys.stderr)
            sys.exit(1)
def cmd_diagnostics():
    global DEVICE_PORT
    if not DEVICE_PORT:
        print("Error: Device port not set. Cannot run diagnostics.", file=sys.stderr)
        sys.exit(1)
    print(f"Running diagnostics on {DEVICE_PORT}...")
    diag_steps = [
        {"desc": "Memory Info (micropython.mem_info(1))", "type": "exec", "code": "import micropython; micropython.mem_info(1)", "timeout": MP_TIMEOUT_EXEC},
        # Corrected: Added ":" as the path argument for 'fs df'
        {"desc": "Filesystem Usage (mpremote fs df)", "type": "mpremote_cmd", "args": ["fs", "df", ":"], "timeout": MP_TIMEOUT_DF},
        {"desc": "Free GC Memory (gc.mem_free())", "type": "exec", "code": "import gc; gc.collect(); print(gc.mem_free())", "timeout": MP_TIMEOUT_EXEC},
        # Using ":/" for 'fs ls' is also fine, though just ":" often works for root with mpremote
        {"desc": "List Root (mpremote fs ls :/)", "type": "mpremote_cmd", "args": ["fs", "ls", ":/"], "timeout": MP_TIMEOUT_LS_MPREMOTE}
    ]
    all_ok = True
    for step in diag_steps:
        print(f"\n--- {step['desc']} ---")
        result = None
        if step["type"] == "exec":
            result = run_mpremote_command(["exec", step["code"]], suppress_output=False, timeout=step["timeout"])
        elif step["type"] == "mpremote_cmd":
            result = run_mpremote_command(step["args"], suppress_output=False, timeout=step["timeout"])
        
        time.sleep(FS_OPERATION_DELAY / 2) # Small delay between diagnostic commands

        if result is None or result.returncode != 0:
            all_ok = False
            print(f"Error running diagnostic for {step['desc']}.", file=sys.stderr)
            if result and result.stderr:
                # For fs df error, stderr might not be very informative if it's just the usage.
                # Check if stdout also has content, as mpremote sometimes prints usage to stdout.
                if result.stdout and "usage: fs" in result.stdout:
                     print(f"Details: {result.stdout.strip()}", file=sys.stderr)
                else:
                     print(f"Details: {result.stderr.strip()}", file=sys.stderr)

            elif result and result.stdout and step["type"] == "exec": 
                print(f"Output (may contain error): {result.stdout.strip()}", file=sys.stderr)
            elif result is None:
                 print("Failed to get a process result.", file=sys.stderr)
        # No need for 'elif result.stdout and step["type"] == "mpremote_cmd": pass'
        # as suppress_output is False, so stdout is already printed if successful.
    
    if all_ok:
        print("\nDiagnostics completed. Review output above.")
    else:
        print("\nDiagnostics completed with some errors.")
        
def cmd_flash(firmware_source, baud_rate_str="230400"):
    global DEVICE_PORT
    if not DEVICE_PORT:
        print("Error: Device port not set. Cannot proceed with flashing.", file=sys.stderr)
        print("Use 'esp32 devices` to see available devices then `esp32 device <PORT_NAME>' to set the port.")
        sys.exit(1)
    if firmware_source == DEFAULT_FIRMWARE_URL or "micropython.org/resources/firmware/" in firmware_source: 
        print(f"Using official default firmware URL: {firmware_source}")
    
    print("\nIMPORTANT: Ensure your ESP32-C3 is in bootloader mode.")
    print("To do this: Unplug USB, press and HOLD the BOOT button, plug in USB, wait 2-3 seconds, then RELEASE BOOT button.")
    
    try: run_esptool_command(["--version"], suppress_output=True, timeout=5) 
    except SystemExit: 
        print("You can install it with: pip install esptool")
        sys.exit(1)

    if input("Proceed with flashing? (yes/no): ").lower() != 'yes':
        print("Flashing cancelled by user.")
        sys.exit(0)
    
    actual_firmware_file_to_flash = None
    downloaded_temp_file = None
    try:
        if firmware_source.startswith("http://") or firmware_source.startswith("https://"):
            print(f"Downloading firmware from: {firmware_source}")
            try:
                with urllib.request.urlopen(firmware_source) as response, \
                     tempfile.NamedTemporaryFile(delete=False, suffix=".bin", mode='wb') as tmp_file:
                    total_size = response.getheader('Content-Length')
                    if total_size:
                        total_size = int(total_size)
                        print("File size:", total_size // 1024, "KB")
                    else:
                        print("File size: Unknown (Content-Length header not found)")
                    downloaded_size = 0; chunk_size = 8192; progress_ticks = 0
                    sys.stdout.write("Downloading: ["); sys.stdout.flush()
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk: break
                        tmp_file.write(chunk); downloaded_size += len(chunk)
                        if total_size:
                            current_progress_pct = (downloaded_size / total_size) * 100
                            if int(current_progress_pct / 5) > progress_ticks:
                                sys.stdout.write("#"); sys.stdout.flush(); progress_ticks = int(current_progress_pct / 5)
                        elif downloaded_size // (chunk_size * 10) > progress_ticks:
                            sys.stdout.write("."); sys.stdout.flush(); progress_ticks +=1
                    sys.stdout.write("] Done.\n"); sys.stdout.flush()
                    actual_firmware_file_to_flash = tmp_file.name
                    downloaded_temp_file = actual_firmware_file_to_flash
                print(f"Firmware downloaded successfully to temporary file: {actual_firmware_file_to_flash}")
            except urllib.error.URLError as e:
                print(f"\nError downloading firmware: {e.reason}", file=sys.stderr)
                if hasattr(e, 'code'): print(f"HTTP Error Code: {e.code}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"\nAn unexpected error occurred during download: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            local_firmware_path = Path(firmware_source)
            if not local_firmware_path.is_file():
                print(f"Error: Local firmware file not found at '{local_firmware_path}'", file=sys.stderr)
                sys.exit(1)
            actual_firmware_file_to_flash = str(local_firmware_path.resolve()) 
            print(f"Using local firmware file: {actual_firmware_file_to_flash}")
        
        esptool_timeout = 180
        print(f"\nStep 1: Erasing flash on {DEVICE_PORT}...")
        erase_args = ["--chip", "esp32c3", "--port", DEVICE_PORT, "erase_flash"]
        erase_result = run_esptool_command(erase_args, timeout=esptool_timeout) 
        if not erase_result or erase_result.returncode != 0:
            err_msg = erase_result.stderr.strip() if erase_result and erase_result.stderr else "Erase command failed."
            print(f"Error erasing flash. esptool said: {err_msg}", file=sys.stderr)
            if "A fatal error occurred: Could not connect to an Espressif device" in err_msg or \
               "Failed to connect to ESP32-C3" in err_msg :
                 print("This commonly indicates the device is not in bootloader mode or a connection issue.", file=sys.stderr)
            sys.exit(1)
        print("Flash erase completed successfully.")
        
        print(f"\nStep 2: Writing firmware '{Path(actual_firmware_file_to_flash).name}' to {DEVICE_PORT} at baud {baud_rate_str}...")
        write_args = ["--chip", "esp32c3", "--port", DEVICE_PORT, "--baud", baud_rate_str, "write_flash", "-z", "0x0", actual_firmware_file_to_flash]
        write_result = run_esptool_command(write_args, timeout=esptool_timeout)
        if not write_result or write_result.returncode != 0:
            err_msg = write_result.stderr.strip() if write_result and write_result.stderr else "Write flash command failed."
            print(f"Error writing firmware. esptool said: {err_msg}", file=sys.stderr)
            sys.exit(1)
        print("Firmware writing completed successfully.")
        
        print("\nStep 3: Verifying MicroPython installation...")
        print("Waiting a few seconds for device to reboot...")
        time.sleep(5) 
        verified, msg = test_micropython_presence(DEVICE_PORT, timeout=MP_TIMEOUT_EXEC + 5)
        print(msg)
        if not verified:
            print("MicroPython verification failed.", file=sys.stderr)
            sys.exit(1)
        print("\nMicroPython flashed and verified successfully!")
    finally:
        if downloaded_temp_file:
            try: os.remove(downloaded_temp_file)
            except OSError as e: print(f"Warning: Could not delete temporary firmware file {downloaded_temp_file}: {e}", file=sys.stderr)


def main():
    global DEVICE_PORT
    cfg = load_config()
    
    parser = argparse.ArgumentParser(
        prog="esp32",
        description="Manage deployment of MicroPython files to an ESP32 device via mpremote. Also supports flashing MicroPython firmware.",
        epilog="Use 'esp32 <command> --help' for more information on a specific command."
    )
    parser.add_argument("--port", "-p", help="Override default/configured COM port for this command instance.")
    subparsers = parser.add_subparsers(dest="cmd", required=True, title="Available commands", metavar="<command>")

    subparsers.add_parser("help", help="Show this help message and exit.")
    subparsers.add_parser("devices",help="List available COM ports and show the selected COM port.")
    
    dev_parser = subparsers.add_parser("device", help="Set or test the selected COM port for operations.")
    dev_parser.add_argument("port_name", nargs='?', metavar="PORT", help="The COM port to set. If omitted, tests current.")
    dev_parser.add_argument("--force", "-f", action="store_true", help="Force set port even if test fails.")
    
    flash_parser = subparsers.add_parser("flash", help="Download (if URL) and flash MicroPython firmware to the ESP32.")
    flash_parser.add_argument("firmware_source", default=DEFAULT_FIRMWARE_URL, nargs='?', help=f"URL or local path for firmware .bin. Default: official ESP32_GENERIC_C3")
    flash_parser.add_argument("--baud", default="230400", help="Baud rate for flashing (Default: 230400).")
    
    up_parser = subparsers.add_parser("upload", help="Upload file/directory to ESP32. Iterative with delays.")
    up_parser.add_argument("local_source", help="Local file/dir. Trailing '/' on dir (e.g. 'mydir/') uploads contents. No trailing slash (e.g. 'mydir') uploads dir itself.")
    up_parser.add_argument("remote_destination", nargs='?', default=None, help="Remote parent directory path (e.g. '/lib'). If omitted, uploads to device root.")
    
    dl_parser = subparsers.add_parser("download", help="Download file/directory from ESP32. Iterative with delays.")
    dl_parser.add_argument("remote_source_path", metavar="REMOTE_PATH", help="Remote file/dir path. Trailing '/' on dir (e.g., '/logs/', '//' for root contents) downloads its contents. No trailing slash (e.g. '/logs') downloads the directory itself.")
    dl_parser.add_argument("local_target_path", nargs='?', default=None, metavar="LOCAL_PATH", help="Local directory to download into, or local filename for a single remote file. If omitted, uses current directory.")

    run_parser = subparsers.add_parser("run", help="Run Python script on ESP32.")
    run_parser.add_argument("script_name", nargs='?', default="main.py", metavar="SCRIPT", help="Script to run (default: main.py). Path relative to device root.")
    
    list_parser = subparsers.add_parser("list", help="List files/dirs on ESP32 (recursively from given path).") 
    list_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory path (e.g., '/lib', or omit for root).")
    
    tree_parser = subparsers.add_parser("tree", help="Display remote file tree.")
    tree_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory path (default: root).")
    
    del_parser = subparsers.add_parser("delete", help="Delete file/directory on ESP32. Uses recursive delete with delays.")
    del_parser.add_argument("remote_path_to_delete", metavar="REMOTE_PATH", nargs='?', default=None, help="Remote path (e.g. '/main.py', '/lib'). Omitting or '/' deletes root contents (requires confirmation).")
    
    subparsers.add_parser("diagnostics", help="Run diagnostic commands on the ESP32 device.")
    
    args = parser.parse_args()

    if args.port: DEVICE_PORT = args.port
    elif "port" in cfg: DEVICE_PORT = cfg["port"]
    
    commands_needing_port = [
        "device", "upload", "run", "list", "tree", 
        "download", "delete", "flash", "diagnostics" 
    ]
    is_device_command_setting_port = args.cmd == "device" and args.port_name
    
    if args.cmd in commands_needing_port and not DEVICE_PORT and not is_device_command_setting_port:
        if args.cmd == "device" and not args.port_name: pass 
        elif args.cmd == "flash" and not DEVICE_PORT: pass
        else:
            print("Error: No COM port selected or configured.", file=sys.stderr)
            print("Use 'esp32 devices' to list available ports, then 'esp32 device <PORT_NAME>' to set one.", file=sys.stderr)
            sys.exit(1)

    if args.cmd == "help": parser.print_help()
    elif args.cmd == "devices": cmd_devices()
    elif args.cmd == "device":
        if args.port_name: cmd_device(args.port_name, args.force)
        elif DEVICE_PORT: 
            print(f"Current selected COM port is {DEVICE_PORT}. Testing...")
            ok, msg = test_device(DEVICE_PORT); print(msg)
        else: 
            print("No COM port currently selected or configured."); cmd_devices(); print(f"\nUse 'esp32 device <PORT_NAME>' to set one.")
    elif args.cmd == "flash": cmd_flash(args.firmware_source, args.baud)
    elif args.cmd == "upload": cmd_upload(args.local_source, args.remote_destination)
    elif args.cmd == "run": run_script(args.script_name)
    elif args.cmd == "list": list_remote(args.remote_directory)
    elif args.cmd == "tree": tree_remote(args.remote_directory)
    elif args.cmd == "download": cmd_download(args.remote_source_path, args.local_target_path)
    elif args.cmd == "delete": delete_remote(args.remote_path_to_delete)
    elif args.cmd == "diagnostics": cmd_diagnostics() 

if __name__ == "__main__":
    main()