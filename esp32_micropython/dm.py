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
import re
import time # Added for delays

CONFIG_FILE = Path(__file__).parent / ".esp32_deploy_config.json"
DEVICE_PORT = None # Will be set by main after parsing args or loading config
DEFAULT_FIRMWARE_URL = "https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20250415-v1.25.0.bin"

# Constants for file modes (from stat module)
S_IFDIR = 0x4000  # Directory
S_IFREG = 0x8000  # Regular file

# Constants for file operations and timeouts
FS_OPERATION_DELAY = 0.3  # Delay in seconds between filesystem operations on the ESP32
MP_TIMEOUT_STAT = 10
MP_TIMEOUT_LS = 20      # For ls, ls -r
MP_TIMEOUT_MKDIR = 15
MP_TIMEOUT_CP_FILE = 120 # Timeout for copying a single file
MP_TIMEOUT_RM = 60      # Increased for recursive rm
MP_TIMEOUT_EXEC = 20    # Timeout for general exec commands
MP_TIMEOUT_DF = 10      # Timeout for fs df


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


def get_remote_path_stat(target_path_on_device): # E.g., "main.py", "foo/bar.py", "somedir/", "" (for root)
    global DEVICE_PORT
    if not DEVICE_PORT:
        return None

    # Normalize target_path_on_device: remove leading/trailing slashes for path components.
    # If the result is empty, it means root.
    normalized_target_str = target_path_on_device.strip('/')

    mpremote_path_for_cmd: str
    if not normalized_target_str:  # True if input was "" or "/" or "//" etc.
        mpremote_path_for_cmd = ":"  # For root, mpremote uses ":" for stat
    else:
        # For non-root paths like "main.py" or "lib/foo.py"
        mpremote_path_for_cmd = f":/{normalized_target_str}"

    # print(f"DEBUG get_remote_path_stat: input='{target_path_on_device}', normalized_target_str='{normalized_target_str}', mpremote_cmd_path='{mpremote_path_for_cmd}'", file=sys.stderr)

    result = run_mpremote_command(["fs", "stat", mpremote_path_for_cmd], suppress_output=True, timeout=MP_TIMEOUT_STAT)

    if result and result.returncode == 0 and result.stdout:
        match = re.search(r'\(([\s\S]*?)\)', result.stdout)
        if match:
            try:
                stat_tuple_str = match.group(1)
                mode_str = stat_tuple_str.split(',')[0].strip()
                mode = int(mode_str)
                if (mode & S_IFDIR) == S_IFDIR:
                    return "dir"
                elif (mode & S_IFREG) == S_IFREG:
                    return "file"
                else:
                    # print(f"DEBUG get_remote_path_stat: Unknown mode {mode_str} from {result.stdout.strip()} for {mpremote_path_for_cmd}", file=sys.stderr)
                    return "unknown"
            except (IndexError, ValueError) as e:
                # print(f"DEBUG get_remote_path_stat: Error parsing stat tuple '{stat_tuple_str}' from '{result.stdout.strip()}': {e} for {mpremote_path_for_cmd}", file=sys.stderr)
                return None
        else:
            # print(f"DEBUG get_remote_path_stat: Could not parse tuple from stat output: {result.stdout.strip()} for {mpremote_path_for_cmd}", file=sys.stderr)
            return None
    elif result and result.stderr and ("No such file or directory" in result.stderr or "ENOENT" in result.stderr):
        # print(f"DEBUG get_remote_path_stat: Path not found '{mpremote_path_for_cmd}': {result.stderr.strip()}", file=sys.stderr)
        return None
    # Fallback for other errors or unexpected mpremote result
    # print(f"DEBUG get_remote_path_stat: Command failed or unexpected result for '{mpremote_path_for_cmd}'. RC: {result.returncode if result else 'N/A'}, Stderr: {result.stderr.strip() if result and result.stderr else 'N/A'}, Stdout: {result.stdout.strip() if result and result.stdout else 'N/A'}", file=sys.stderr)
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

def test_device(port, timeout=MP_TIMEOUT_LS): # Using LS timeout for a general test
    result = run_mpremote_command(["fs", "ls", ":"], connect_port=port, suppress_output=True, timeout=timeout)
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
        return True # Root directory always exists

    parts = Path(normalized_path).parts
    current_remote_path_str = ""

    for part in parts:
        if not current_remote_path_str:
            current_remote_path_str = part
        else:
            current_remote_path_str = f"{current_remote_path_str}/{part}"
        
        path_type_check = get_remote_path_stat(current_remote_path_str) 
        if path_type_check == "dir":
            continue 
        if path_type_check == "file":
            print(f"Error: Remote path ':{current_remote_path_str}' exists and is a file, cannot create directory.", file=sys.stderr)
            return False
        
        # print(f"    Attempting to create remote directory ':{current_remote_path_str}'...") # Debugging
        result = run_mpremote_command(["fs", "mkdir", f":{current_remote_path_str}"], suppress_output=True, timeout=MP_TIMEOUT_MKDIR)

        if result and result.returncode == 0:
            print(f"    Created remote directory component ':{current_remote_path_str}'")
            time.sleep(FS_OPERATION_DELAY) 
            continue 
        elif result and result.stderr and ("EEXIST" in result.stderr or "File exists" in result.stderr):
            # Possible race condition or mpremote quirk; re-check type
            path_type_check_after_mkdir = get_remote_path_stat(current_remote_path_str)
            if path_type_check_after_mkdir == "dir":
                # print(f"    Remote directory ':{current_remote_path_str}' already existed.") # Debugging
                time.sleep(FS_OPERATION_DELAY / 2) # Shorter delay if it already existed
                continue 
            else:
                err_msg = f"Path ':{current_remote_path_str}' exists but is not a directory (it is {path_type_check_after_mkdir or 'of an unknown type'})."
                print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
                return False
        else:
            err_msg = result.stderr.strip() if result and result.stderr else f"Unknown error creating ':{current_remote_path_str}'"
            if result and not result.stderr and result.stdout: 
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
        # Handle case like "/" becoming "" after rstrip, but it's a valid absolute path
        if not local_src_for_path_obj and Path(original_local_src_display).is_absolute():
             local_src_for_path_obj = original_local_src_display
        # Handle "." or "./" becoming "" after rstrip
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
            # No extra delay here, ensure_remote_dir handles its own delays

        local_file_basename = abs_local_path.name
        mpremote_target_path_on_device = f":{effective_remote_parent_dir_str}/{local_file_basename}" if effective_remote_parent_dir_str else f":{local_file_basename}"
        
        print(f"Uploading file '{abs_local_path}' to '{mpremote_target_path_on_device}' on device...")
        cp_args = ["fs", "cp", str(abs_local_path).replace(os.sep, '/'), mpremote_target_path_on_device]
        result = run_mpremote_command(cp_args, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
        
        if result and result.returncode == 0:
            print("File upload complete.")
            time.sleep(FS_OPERATION_DELAY)
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
            if remote_base_for_items_str: # If not uploading to root contents
                print(f"Ensuring remote base directory ':{remote_base_for_items_str}' exists...")
                if not ensure_remote_dir(remote_base_for_items_str):
                    sys.exit(1)
        else:
            src_dir_name = abs_local_path.name
            remote_base_for_items_str = f"{effective_remote_parent_dir_str}/{src_dir_name}".strip("/") if effective_remote_parent_dir_str else src_dir_name
            print(f"Uploading local directory '{abs_local_path}' as ':{remote_base_for_items_str}' on device...")
            
            # Ensure parent of this new dir exists
            if effective_remote_parent_dir_str: # i.e. remote_base_for_items_str is not just "src_dir_name" at root
                # This is actually creating the parent of remote_base_for_items_str
                # e.g. if remote_base_for_items_str = "foo/bar", effective_remote_parent_dir_str = "foo"
                print(f"Ensuring remote parent directory ':{effective_remote_parent_dir_str}' exists...")
                if not ensure_remote_dir(effective_remote_parent_dir_str):
                    sys.exit(1)

            # Now create the directory remote_base_for_items_str itself
            print(f"Ensuring remote target directory ':{remote_base_for_items_str}' exists...")
            if not ensure_remote_dir(remote_base_for_items_str): # This creates the actual "src_dir_name" under parent
                 sys.exit(1)

        files_uploaded_count = 0
        dirs_created_count = 0 # ensure_remote_dir handles its own prints for components
        
        for root, dirs, files in os.walk(str(abs_local_path)):
            root_path = Path(root)
            relative_dir_path_from_src = root_path.relative_to(abs_local_path)

            current_remote_target_dir_str = remote_base_for_items_str
            if str(relative_dir_path_from_src) != ".": # if not in the top-level of abs_local_path
                if remote_base_for_items_str: # If base is not root
                    current_remote_target_dir_str = f"{remote_base_for_items_str}/{relative_dir_path_from_src.as_posix()}"
                else: # If base is root
                    current_remote_target_dir_str = relative_dir_path_from_src.as_posix()

            # Directories are created by ensure_remote_dir as it walks parts.
            # Here, we ensure the *specific* subdirectories from os.walk exist.
            for dir_name in sorted(dirs):
                # Path for ensure_remote_dir needs to be relative to device root
                remote_subdir_to_ensure = Path(current_remote_target_dir_str) / dir_name
                print(f"  Ensuring remote subdirectory ':{remote_subdir_to_ensure.as_posix()}' exists...")
                if ensure_remote_dir(remote_subdir_to_ensure.as_posix()):
                    # ensure_remote_dir prints "Created..." and delays internally
                    dirs_created_count +=1 # Count distinct ensure_remote_dir calls for subdirs
                else:
                    print(f"    Failed to create remote subdirectory ':{remote_subdir_to_ensure.as_posix()}'. Skipping its contents.", file=sys.stderr)
                    dirs.remove(dir_name) # Don't descend into this dir if creation failed

            for file_name in sorted(files):
                local_file_full_path = root_path / file_name
                
                # Construct remote file path
                remote_file_target_on_device_str: str
                if current_remote_target_dir_str:
                    remote_file_target_on_device_str = f":{current_remote_target_dir_str}/{file_name}"
                else: # Target is root
                    remote_file_target_on_device_str = f":{file_name}"

                print(f"  Uploading '{local_file_full_path.relative_to(abs_local_path)}' to '{remote_file_target_on_device_str}'...")
                cp_args_file = ["fs", "cp", str(local_file_full_path).replace(os.sep, '/'), remote_file_target_on_device_str]
                result_file = run_mpremote_command(cp_args_file, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
                
                if result_file and result_file.returncode == 0:
                    files_uploaded_count += 1
                    time.sleep(FS_OPERATION_DELAY)
                else:
                    err_msg = result_file.stderr.strip() if result_file and result_file.stderr else "File upload failed"
                    if result_file and not err_msg and result_file.stdout: err_msg = result_file.stdout.strip()
                    print(f"    Error uploading file '{local_file_full_path.relative_to(abs_local_path)}': {err_msg}", file=sys.stderr)
                    # Consider if we should exit or continue. For now, continue and report at end.

        print(f"Directory upload processed. {files_uploaded_count} files uploaded.")
        # Potentially add more detailed success/failure summary here if errors occurred but didn't exit.

    else: 
        print(f"Error: Unhandled local source type for '{original_local_src_display}'.", file=sys.stderr)
        sys.exit(1)

def cmd_download(remote_src_arg, local_dest_arg=None):
    global DEVICE_PORT
    had_trailing_slash_remote = remote_src_arg.endswith("/")
    
    # Normalize remote_src_for_checks_str: "" for root, "path" for /path, "path/sub" for /path/sub
    if remote_src_arg == "/" or remote_src_arg == "//": # Explicit root content listing or root itself
        remote_src_for_checks_str = "" 
        path_for_stat = "" # get_remote_path_stat handles "" as root for stat
    else:
        remote_src_for_checks_str = remote_src_arg.strip("/")
        path_for_stat = remote_src_for_checks_str # Used for initial stat

    if remote_src_arg == "/" and not had_trailing_slash_remote and remote_src_for_checks_str == "": 
        # This case means user typed "download /" (no trailing slash for root itself)
        # It's ambiguous: copy root dir "as root" (not possible) or contents of root?
        # For contents, we standardized on "//" or "/dir/"
        print("Error: Ambiguous command 'download /'.", file=sys.stderr)
        print("  To download contents of the root directory, use 'download // [local_path]' or 'download / [local_path/]'.", file=sys.stderr)
        print("  To download a specific item from root, use 'download /item_name [local_path]'.", file=sys.stderr)
        sys.exit(1)

    print(f"Checking remote path ':{path_for_stat or '/'}'...")
    remote_type = get_remote_path_stat(path_for_stat) # path_for_stat is "" for root
    
    if remote_type is None:
        print(f"Error: Remote path ':{path_for_stat or '/'}' not found or not accessible on device.", file=sys.stderr)
        sys.exit(1)

    if remote_type == "file":
        # remote_src_for_checks_str here is the full path of the file, e.g., "some/file.txt" or "file.txt"
        remote_basename = Path(remote_src_for_checks_str).name 
        
        local_target_path_obj: Path
        if local_dest_arg:
            local_dest_path_obj = Path(os.path.abspath(local_dest_arg))
            # If local_dest_arg ends with / or is an existing dir, treat as target directory
            if local_dest_arg.endswith(("/", os.sep)) or local_dest_path_obj.is_dir():
                local_dest_path_obj.mkdir(parents=True, exist_ok=True)
                local_target_path_obj = local_dest_path_obj / remote_basename
            else: # Treat as target file path
                local_dest_path_obj.parent.mkdir(parents=True, exist_ok=True)
                local_target_path_obj = local_dest_path_obj
        else: # No local_dest_arg, download to CWD
            local_target_path_obj = Path.cwd() / remote_basename

        final_mpremote_local_dest_str = str(local_target_path_obj).replace(os.sep, '/')
        mpremote_remote_source_str = f":{path_for_stat}"

        print(f"Downloading remote file '{mpremote_remote_source_str}' to local path '{final_mpremote_local_dest_str}'...")
        cp_args = ["fs", "cp", mpremote_remote_source_str, final_mpremote_local_dest_str]
        result = run_mpremote_command(cp_args, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
        
        if result and result.returncode == 0:
            print("File download complete.")
            time.sleep(FS_OPERATION_DELAY)
        else:
            err_parts = []
            if result and result.stdout: err_parts.append(result.stdout.strip())
            if result and result.stderr: err_parts.append(result.stderr.strip())
            err_msg = "; ".join(filter(None, err_parts))
            if not err_msg : err_msg = f"File download failed with mpremote exit code {result.returncode if result else 'N/A'}"
            print(f"Error downloading from '{mpremote_remote_source_str}': {err_msg}", file=sys.stderr)
            sys.exit(1)

    elif remote_type == "dir":
        # remote_src_for_checks_str is like "logs", "foo/bar", or "" (for root)
        local_base_dir_for_items: Path # This is where the content/dir itself will be placed locally.
        
        # Determine if copying contents or the directory itself
        # had_trailing_slash_remote applies to the original remote_src_arg
        # remote_src_for_checks_str is "" if original was "/" or "//"
        if had_trailing_slash_remote or (remote_src_arg in ["/", "//"] and remote_src_for_checks_str == ""):
            # Download contents of remote_src_for_checks_str into local_base_dir_for_items
            local_base_dir_for_items = Path(os.path.abspath(local_dest_arg or "."))
            print(f"Downloading contents of remote directory ':{remote_src_for_checks_str or '/'}' to local directory '{local_base_dir_for_items}'...")
        else:
            # Download remote_src_for_checks_str AS a directory into local_dest_arg (or CWD)
            remote_dir_name = Path(remote_src_for_checks_str).name # Name of the dir being downloaded
            parent_local_dir = Path(os.path.abspath(local_dest_arg or "."))
            local_base_dir_for_items = parent_local_dir / remote_dir_name
            print(f"Downloading remote directory ':{remote_src_for_checks_str}' as local directory '{local_base_dir_for_items}'...")

        local_base_dir_for_items.mkdir(parents=True, exist_ok=True)
        # Small delay after local mkdir, not strictly necessary for ESP32 but good for rhythm
        time.sleep(FS_OPERATION_DELAY / 4) 

        print(f"  Fetching file list from remote ':{remote_src_for_checks_str or '/'}'...")
        # list_remote_capture needs "" for root, or "somedir" (which is remote_src_for_checks_str)
        all_remote_items_abs = list_remote_capture(remote_src_for_checks_str)
        time.sleep(FS_OPERATION_DELAY) # Delay after remote listing operation

        if not all_remote_items_abs:
            # Check if the directory genuinely exists and is empty
            # This is to differentiate "empty dir" from "dir not found by list_remote_capture"
            is_valid_dir_check = get_remote_path_stat(remote_src_for_checks_str) # path_for_stat could be used here too
            if is_valid_dir_check == 'dir':
                 print(f"Remote directory ':{remote_src_for_checks_str or '/'}' is empty. Nothing to download.")
                 return # Successfully "downloaded" an empty directory
            else:
                 print(f"Failed to list contents or ':{remote_src_for_checks_str or '/'}' is not a directory (type: {is_valid_dir_check}).", file=sys.stderr)
                 sys.exit(1)

        # base_remote_path_obj is Path object representing the source dir on remote, e.g., Path("/logs") or Path("/")
        base_remote_path_obj = Path("/") / remote_src_for_checks_str

        files_downloaded_count = 0
        dirs_created_count = 0
        
        remote_dirs_to_create_locally = []
        remote_files_to_download = []

        for remote_item_abs_str in all_remote_items_abs:
            if remote_item_abs_str.endswith('/'):
                remote_dirs_to_create_locally.append(remote_item_abs_str)
            else:
                remote_files_to_download.append(remote_item_abs_str)
        
        for remote_dir_abs_str in sorted(remote_dirs_to_create_locally):
            # remote_dir_abs_str is like "/logs/subdir/"
            # Make it relative to the source directory: e.g. "subdir/" if base_remote_path_obj is "/logs"
            relative_path_obj = Path(remote_dir_abs_str).relative_to(base_remote_path_obj)
            local_target_dir_path = local_base_dir_for_items / relative_path_obj
            
            print(f"  Ensuring local directory '{local_target_dir_path}' exists...")
            local_target_dir_path.mkdir(parents=True, exist_ok=True)
            dirs_created_count += 1
            # time.sleep(FS_OPERATION_DELAY / 4) # Optional small delay for local ops

        for remote_file_abs_str in sorted(remote_files_to_download):
            relative_path_obj = Path(remote_file_abs_str).relative_to(base_remote_path_obj)
            local_target_file_path = local_base_dir_for_items / relative_path_obj
            
            local_target_file_path.parent.mkdir(parents=True, exist_ok=True) # Ensure parent for file

            mpremote_remote_source_for_file = ":" + remote_file_abs_str.lstrip('/') # Needs leading ":"
            
            print(f"  Downloading remote file '{mpremote_remote_source_for_file}' to '{local_target_file_path}'...")
            cp_args_file = ["fs", "cp", mpremote_remote_source_for_file, str(local_target_file_path).replace(os.sep, '/')]
            result_file = run_mpremote_command(cp_args_file, suppress_output=True, timeout=MP_TIMEOUT_CP_FILE)
            
            if result_file and result_file.returncode == 0:
                files_downloaded_count += 1
                time.sleep(FS_OPERATION_DELAY)
            else:
                err_msg = result_file.stderr.strip() if result_file and result_file.stderr else "File download failed"
                if result_file and not err_msg and result_file.stdout: err_msg = result_file.stdout.strip()
                print(f"    Error downloading file '{mpremote_remote_source_for_file}': {err_msg}", file=sys.stderr)
                # Consider sys.exit(1) or continue

        print(f"Directory download processed. {dirs_created_count} local directories created/ensured, {files_downloaded_count} files downloaded.")
    else: 
        print(f"Error: Unhandled remote source type '{remote_type}' for ':{path_for_stat or '/'}'.", file=sys.stderr)
        sys.exit(1)


def run_script(script="main.py"):
    global DEVICE_PORT
    script_on_device_norm = script.lstrip('/')
    print(f"Checking for '{script_on_device_norm}' on device...")
    path_type = get_remote_path_stat(script_on_device_norm) 
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
    escaped_script_path = abs_script_path_on_device.replace("'", "\\'")
    python_code = f"exec(open('{escaped_script_path}').read())"
    print(f"Running '{script_on_device_norm}' on {DEVICE_PORT}...")
    result = run_mpremote_command(["exec", python_code], suppress_output=False, timeout=None) # Allow script to run indefinitely unless user Ctr-C
    if result and result.returncode != 0:
        # Non-zero usually means script error, mpremote still often exits 0 unless connection issue
        # stderr from script already printed if suppress_output=False
        pass


def list_remote_capture(remote_dir_arg=""): 
    global DEVICE_PORT
    if not DEVICE_PORT: return []

    base_for_mpremote_ls = remote_dir_arg.strip('/')
    mpremote_target_path_for_ls = f":/{base_for_mpremote_ls}".rstrip("/")
    if not base_for_mpremote_ls: 
         mpremote_target_path_for_ls = ":/"

    result = run_mpremote_command(["fs", "ls", "-r", mpremote_target_path_for_ls], suppress_output=True, timeout=MP_TIMEOUT_LS)
    
    abs_paths = []
    if result and result.returncode == 0 and result.stdout:
        lines = result.stdout.splitlines()
        listed_dir_abs_path_obj = Path("/") / base_for_mpremote_ls

        for line in lines:
            line_content = line.strip()
            if not line_content or \
               line_content.lower().startswith("ls ") or \
               line_content.lower().startswith("stat ") or \
               line_content == mpremote_target_path_for_ls :
                continue
            
            parts = line_content.split(maxsplit=1)
            item_name_relative_to_listed_dir = ""
            if len(parts) == 2 and parts[0].isdigit():
                item_name_relative_to_listed_dir = parts[1]
            elif len(parts) == 1: 
                item_name_relative_to_listed_dir = parts[0]
            else: 
                continue 
            
            if not item_name_relative_to_listed_dir:
                continue

            abs_item_path_obj = listed_dir_abs_path_obj / item_name_relative_to_listed_dir
            abs_path_str = abs_item_path_obj.as_posix() 

            if item_name_relative_to_listed_dir.endswith('/') and not abs_path_str.endswith('/'):
                 if abs_path_str != "/": 
                     abs_path_str += "/"
            
            abs_paths.append(abs_path_str)
        return abs_paths
    elif result and result.stderr:
        if not ("No such file or directory" in result.stderr or "ENOENT" in result.stderr):
             print(f"Error during 'mpremote fs ls -r {mpremote_target_path_for_ls}': {result.stderr.strip()}", file=sys.stderr)
    return []


def list_remote(remote_dir=None):
    global DEVICE_PORT
    normalized_remote_dir_str = (remote_dir or "").strip("/") 
    display_dir_name = f":{normalized_remote_dir_str or '/'}"
    
    if normalized_remote_dir_str: 
        path_type = get_remote_path_stat(normalized_remote_dir_str) 
        if path_type is None:
            print(f"Error: Remote path '{display_dir_name}' not found.", file=sys.stderr)
            return
        if path_type != 'dir':
            print(f"Error: '{display_dir_name}' is a {path_type}, not a directory. Use 'download' for files.", file=sys.stderr)
            return
    
    print(f"Listing contents of '{display_dir_name}' (recursive, using mpremote fs ls -r)...")
    all_paths_abs = list_remote_capture(normalized_remote_dir_str)
    time.sleep(FS_OPERATION_DELAY / 2) # Small delay after listing

    if not all_paths_abs:
        is_valid_empty_dir = False
        if not normalized_remote_dir_str: 
            is_valid_empty_dir = True
        else:
            path_type_check = get_remote_path_stat(normalized_remote_dir_str)
            if path_type_check == 'dir':
                is_valid_empty_dir = True
        
        if is_valid_empty_dir:
            print(f"Directory '{display_dir_name}' is empty.")
        else:
            print(f"Directory '{display_dir_name}' is empty or no items could be listed.")
        return
    
    displayed_something = False
    base_display_path = Path("/") / normalized_remote_dir_str

    for path_str_abs in sorted(all_paths_abs):
        path_obj_abs = Path(path_str_abs)
        try:
            path_to_print_obj = path_obj_abs.relative_to(base_display_path)
            path_to_print = str(path_to_print_obj.as_posix())
            if path_str_abs.endswith('/') and not path_to_print.endswith('/') and path_to_print != ".":
                path_to_print += "/"
        except ValueError: 
            path_to_print = path_str_abs 
        
        if path_to_print == ".": 
             if path_str_abs.endswith('/'):
                path_to_print = Path(path_str_abs).name + ("/" if path_str_abs.endswith('/') else "")
             else: 
                path_to_print = Path(path_str_abs).name

        if path_to_print: 
            print(path_to_print)
            displayed_something = True

    if not displayed_something and all_paths_abs :
        print(f"Directory '{display_dir_name}' contains no listable content items after filtering.")
    elif not displayed_something and not all_paths_abs:
        print(f"Directory '{display_dir_name}' is empty.")


def tree_remote(remote_dir=None):
    global DEVICE_PORT
    base_path_str_norm = (remote_dir or "").strip("/") 
    display_root_name = f":{base_path_str_norm or '/'}"

    if base_path_str_norm: 
        path_type = get_remote_path_stat(base_path_str_norm) 
        if path_type is None:
            print(f"Error: Remote directory '{display_root_name}' not found.", file=sys.stderr)
            return
        if path_type != 'dir':
            print(f"Error: '{display_root_name}' is a {path_type}. Cannot display as tree.", file=sys.stderr)
            return
    
    print(f"Tree for '{display_root_name}' on device:")
    lines_abs = list_remote_capture(base_path_str_norm) 
    time.sleep(FS_OPERATION_DELAY / 2) # Small delay after listing
    
    if not lines_abs:
        # Check if dir is genuinely empty
        is_valid_empty_dir = False
        if not base_path_str_norm: is_valid_empty_dir = True
        else:
            if get_remote_path_stat(base_path_str_norm) == 'dir': is_valid_empty_dir = True
        
        if is_valid_empty_dir:
            print(f"Directory '{display_root_name}' is empty.")
        else: # list_remote_capture failed for some other reason
            print(f"Could not retrieve contents for '{display_root_name}'.")
        return

    paths_for_tree_build = []
    tree_base_path_obj = Path("/") / base_path_str_norm

    for abs_path_str in lines_abs:
        abs_path_obj = Path(abs_path_str)
        try:
            relative_path_obj = abs_path_obj.relative_to(tree_base_path_obj)
            if str(relative_path_obj) != ".": 
                # Preserve trailing slash by using original string for check
                if abs_path_str.endswith('/'):
                    paths_for_tree_build.append(Path(str(relative_path_obj) + "/"))
                else:
                    paths_for_tree_build.append(relative_path_obj)
        except ValueError: 
            if not base_path_str_norm:
                 # For root listing, abs_path_obj is already effectively relative to conceptual root.
                 # We need to get its parts relative to Path("/")
                 # e.g. Path("/foo/bar.py").relative_to(Path("/")) -> "foo/bar.py"
                 root_relative_path = abs_path_obj.relative_to(Path("/"))
                 if abs_path_str.endswith('/'):
                     paths_for_tree_build.append(Path(str(root_relative_path) + "/"))
                 else:
                     paths_for_tree_build.append(root_relative_path)


    if not paths_for_tree_build and lines_abs: # Should mean lines_abs only contained the base dir itself
        print(f"Directory '{display_root_name}' contains no listable sub-items for tree display.")
        return
    elif not paths_for_tree_build and not lines_abs: # Should be caught by earlier empty check
        print(f"Directory '{display_root_name}' is empty.")
        return
        
    structure = {}
    sorted_paths_for_tree = sorted(list(set(paths_for_tree_build)), key=lambda p: p.parts)

    for p_obj in sorted_paths_for_tree:
        current_level = structure
        # Path("dir/").parts might be ("dir",) or ("dir", "") depending on Python/Pathlib version.
        # Let's use string representation to check for trailing slash for explicit dir marking in tree.
        path_str_for_parts_check = str(p_obj)
        is_dir_node = path_str_for_parts_check.endswith('/')
        
        # Use .parts from Path object for splitting, but on a version without trailing slash for parts.
        # e.g. Path("foo/bar/") -> parts ("foo", "bar")
        # Path("foo/file.txt") -> parts ("foo", "file.txt")
        cleaned_path_for_parts = Path(path_str_for_parts_check.rstrip('/'))
        
        for part_idx, part_name in enumerate(cleaned_path_for_parts.parts):
            is_last_part = (part_idx == len(cleaned_path_for_parts.parts) - 1)
            
            # Mark directories in the structure for print_tree_nodes to use
            # Store as (name, is_directory_type)
            # This is getting complex. Simpler: rely on key name and if value is {} or not.
            # Let's use the previous simple structure and try to infer type later or adjust print_tree_nodes.
            # For now, the node name will include trailing / if it's a directory from list_remote_capture
            
            # We need consistent names for keys in `structure`.
            # If `p_obj` comes from `Path("somedir/")`, its str might be "somedir/" or "somedir".
            # Let's ensure keys are just names, and handle the dir marker in `print_tree_nodes` or by how `structure` is built.

            # The `paths_for_tree_build` list has Path objects.
            # If Path("somedir/") was added, p_obj might be Path("somedir").
            # The original `abs_path_str` from `list_remote_capture` had the trailing slash.
            # This info is better passed through.
            # Instead of `paths_for_tree_build` containing Path objects, let it contain strings.
            
            # Revisit tree data structure if print_tree_nodes is difficult.
            # Current structure: dict of dicts. Files are terminal empty dicts. Empty dirs are also empty dicts.
            # This makes distinguishing empty dir from file hard.

            current_level = current_level.setdefault(part_name, {}) # All nodes are dicts
            
    # print_tree_nodes needs a way to know if a name is a dir or file.
    # Let's pass the original `lines_abs` and `tree_base_path_obj` to `print_tree_nodes` or a helper.
    # This is getting too complex for a quick fix. The existing tree might be "good enough" for now.
    # The primary goal was upload/download.
            
    def print_tree_nodes(node_dict, current_path_prefix_obj, prefix_str=""):
        children_names = sorted(node_dict.keys())
        for i, child_name in enumerate(children_names):
            connector = "└── " if i == len(children_names) - 1 else "├── "
            
            # Try to determine if child_name represents a directory
            # This requires checking against the original `lines_abs` data.
            # A child_name in node_dict is a directory if its corresponding full path in lines_abs ends with '/'
            # or if it has children in node_dict (i.e. node_dict[child_name] is not empty)
            
            child_is_dir_type = False
            if node_dict[child_name]: # Has children, must be a dir
                child_is_dir_type = True
            else: # No children in our tree, could be file or empty dir. Check original listing.
                # Reconstruct potential full path relative to tree_base_path_obj
                # then check if (tree_base_path_obj / current_path_prefix_obj / child_name + "/") was in lines_abs
                # This is also complex. For now, just print.
                # Simplification: If list_remote_capture can ensure items in `lines_abs`
                # that are directories are *always* suffixed with '/', then Path objects made from them
                # and then their names can be used. My list_remote_capture attempts this.
                # So, if child_name derived from such a path ends with '/', it's a dir.
                # However, p_obj.parts used above strips these.
                
                # Fallback: assume leaf node is file unless name itself implies dir (less robust)
                pass # Keep it simple for now.

            print(f"{prefix_str}{connector}{child_name}") # Print name as is from dict keys
            if node_dict[child_name]: 
                new_prefix_str = prefix_str + ("    " if i == len(children_names) - 1 else "│   ")
                print_tree_nodes(node_dict[child_name], current_path_prefix_obj / child_name, new_prefix_str)

    if not base_path_str_norm: # Root
        print(".") 
    else:
        print(f"{Path(base_path_str_norm).name}") # Print name of the dir being treed.
    
    print_tree_nodes(structure, Path(".")) # Start with empty current_path_prefix


def delete_remote(remote_path_arg):
    global DEVICE_PORT
    is_root_delete_all_contents = remote_path_arg is None or remote_path_arg.strip() in ["", "/"]
    DELETE_OPERATION_TIMEOUT = MP_TIMEOUT_RM 

    if is_root_delete_all_contents:
        print("WARNING: You are about to delete all files and directories from the root of the device.")
        confirm = input("Are you sure? Type 'yes' to proceed: ")
        if confirm.lower() != 'yes':
            print("Operation cancelled.")
            return
        print("Fetching root directory contents for deletion...")
        ls_result = run_mpremote_command(["fs", "ls", ":"], suppress_output=True, timeout=MP_TIMEOUT_LS)
        if not ls_result or ls_result.returncode != 0:
            err = ls_result.stderr.strip() if ls_result and ls_result.stderr else "Failed to connect or list root."
            print(f"Error listing root directory for deletion: {err}", file=sys.stderr)
            sys.exit(1)
        items_to_delete_from_root = []
        if ls_result.stdout:
            for line in ls_result.stdout.splitlines():
                line_content = line.strip()
                if not line_content or line_content.lower().startswith("ls ") or line_content == ":":
                    continue
                parts = line_content.split(maxsplit=1)
                item_name = ""
                if len(parts) == 2 and parts[0].isdigit(): 
                    item_name = parts[1]
                else: 
                    item_name = line_content 
                if item_name and item_name != '/': 
                    items_to_delete_from_root.append(item_name.rstrip('/')) 
        if not items_to_delete_from_root:
            print("Root directory is already empty or no valid items found by ls.")
            return
        print(f"Items to delete from root: {items_to_delete_from_root}")
        all_successful = True
        for item_name_to_delete in items_to_delete_from_root:
            item_target_for_mpremote = ":" + item_name_to_delete 
            print(f"Deleting '{item_target_for_mpremote}'...")
            del_result = run_mpremote_command(
                ["fs", "rm", "-r", item_target_for_mpremote], 
                suppress_output=True, 
                timeout=DELETE_OPERATION_TIMEOUT
            )
            time.sleep(FS_OPERATION_DELAY) # Delay after each rm operation
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
        if path_type is None:
            print(f"Error: Remote path '{mpremote_target_path}' does not exist on device.", file=sys.stderr)
            sys.exit(1) 
        print(f"Deleting '{mpremote_target_path}' (type detected: {path_type})...")
        del_args = ["fs", "rm", "-r", mpremote_target_path] 
        del_result = run_mpremote_command(del_args, suppress_output=True, timeout=DELETE_OPERATION_TIMEOUT)
        time.sleep(FS_OPERATION_DELAY) # Delay after rm operation
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
        {"desc": "Filesystem Usage (mpremote fs df)", "type": "mpremote_cmd", "args": ["fs", "df"], "timeout": MP_TIMEOUT_DF},
        {"desc": "Free GC Memory (gc.mem_free())", "type": "exec", "code": "import gc; gc.collect(); print(gc.mem_free())", "timeout": MP_TIMEOUT_EXEC},
        {"desc": "List Root (mpremote fs ls :/)", "type": "mpremote_cmd", "args": ["fs", "ls", ":/"], "timeout": MP_TIMEOUT_LS}
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
                print(f"Details: {result.stderr.strip()}", file=sys.stderr)
            elif result and result.stdout and step["type"] == "exec": 
                print(f"Output (may contain error): {result.stdout.strip()}", file=sys.stderr)
            elif result is None:
                 print("Failed to get a process result.", file=sys.stderr)
        elif result.stdout and step["type"] == "mpremote_cmd": 
            pass 
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
        print("If you have a specific firmware .bin URL or local file, please provide it as an argument to the flash command.")
    print("\nIMPORTANT: Ensure your ESP32-C3 is in bootloader mode.")
    print("To do this: Unplug USB, press and HOLD the BOOT button, plug in USB, wait 2-3 seconds, then RELEASE BOOT button.")
    try:
        run_esptool_command(["--version"], suppress_output=True, timeout=5) 
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
                    downloaded_size = 0
                    chunk_size = 8192 
                    progress_ticks = 0
                    sys.stdout.write("Downloading: [")
                    sys.stdout.flush()
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size:
                            current_progress_pct = (downloaded_size / total_size) * 100
                            if int(current_progress_pct / 5) > progress_ticks:
                                sys.stdout.write("#")
                                sys.stdout.flush()
                                progress_ticks = int(current_progress_pct / 5)
                        else: 
                            if downloaded_size // (chunk_size * 10) > progress_ticks : 
                                sys.stdout.write(".")
                                sys.stdout.flush()
                                progress_ticks +=1
                    sys.stdout.write("] Done.\n")
                    sys.stdout.flush()
                    actual_firmware_file_to_flash = tmp_file.name
                    downloaded_temp_file = actual_firmware_file_to_flash
                print(f"Firmware downloaded successfully to temporary file: {actual_firmware_file_to_flash}")
            except urllib.error.URLError as e:
                print(f"\nError downloading firmware: {e.reason}", file=sys.stderr)
                if hasattr(e, 'code'):
                    print(f"HTTP Error Code: {e.code}", file=sys.stderr)
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
        
        esptool_timeout = 180 # 3 minutes for erase or write
        print(f"\nStep 1: Erasing flash on {DEVICE_PORT}...")
        erase_args = ["--chip", "esp32c3", "--port", DEVICE_PORT, "erase_flash"]
        erase_result = run_esptool_command(erase_args, timeout=esptool_timeout) 
        if not erase_result or erase_result.returncode != 0:
            err_msg = erase_result.stderr.strip() if erase_result and erase_result.stderr else "Erase command failed."
            print(f"Error erasing flash. esptool said: {err_msg}", file=sys.stderr)
            if "A fatal error occurred: Could not connect to an Espressif device" in err_msg or \
               "Failed to connect to ESP32-C3" in err_msg :
                 print("This commonly indicates the device is not in bootloader mode or a connection issue.", file=sys.stderr)
                 print("Please ensure you have followed the BOOT button procedure correctly.", file=sys.stderr)
            sys.exit(1)
        print("Flash erase completed successfully.")
        
        print(f"\nStep 2: Writing firmware '{Path(actual_firmware_file_to_flash).name}' to {DEVICE_PORT} at baud {baud_rate_str}...")
        write_args = [
            "--chip", "esp32c3", "--port", DEVICE_PORT, "--baud", baud_rate_str,
            "write_flash", "-z", "0x0", actual_firmware_file_to_flash
        ]
        write_result = run_esptool_command(write_args, timeout=esptool_timeout)
        if not write_result or write_result.returncode != 0:
            err_msg = write_result.stderr.strip() if write_result and write_result.stderr else "Write flash command failed."
            print(f"Error writing firmware. esptool said: {err_msg}", file=sys.stderr)
            sys.exit(1)
        print("Firmware writing completed successfully.")
        
        print("\nStep 3: Verifying MicroPython installation...")
        print("Waiting a few seconds for device to reboot...")
        time.sleep(5) 
        verified, msg = test_micropython_presence(DEVICE_PORT, timeout=MP_TIMEOUT_EXEC + 5) # Slightly longer timeout after flash
        print(msg)
        if not verified:
            print("MicroPython verification failed. The board may not have rebooted correctly, or flashing was unsuccessful despite esptool's report.", file=sys.stderr)
            print("Try manually resetting the device (unplug/replug or reset button if available) and then 'esp32 device' to test communication.", file=sys.stderr)
            sys.exit(1)
        print("\nMicroPython flashed and verified successfully!")
        print("It's recommended to unplug and replug your device now to ensure it starts in normal MicroPython mode.")
    finally:
        if downloaded_temp_file:
            try:
                os.remove(downloaded_temp_file)
            except OSError as e:
                print(f"Warning: Could not delete temporary firmware file {downloaded_temp_file}: {e}", file=sys.stderr)

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
    flash_parser.add_argument("firmware_source", default=DEFAULT_FIRMWARE_URL, nargs='?', help=f"URL to download the MicroPython .bin firmware, or a path to a local .bin file. (Default: official ESP32_GENERIC_C3)")
    flash_parser.add_argument("--baud", default="230400", help="Baud rate for flashing firmware (Default: 460800). Other common: 230400, 921600, 1152000.")
    
    up_parser = subparsers.add_parser("upload", help="Upload file/directory to ESP32. Iterative with delays.")
    up_parser.add_argument("local_source", help="Local file/dir. Trailing '/' on dir (e.g. 'mydir/') uploads its contents. No trailing slash (e.g. 'mydir') uploads the directory itself.")
    up_parser.add_argument("remote_destination", nargs='?', default=None, help="Remote parent directory path (e.g. '/lib'). If omitted, uploads to device root. Trailing slash is normalized.")
    
    dl_parser = subparsers.add_parser("download", help="Download file/directory from ESP32. Iterative with delays.")
    dl_parser.add_argument("remote_source_path", metavar="REMOTE_PATH", help="Remote file/dir path. Trailing '/' on dir (e.g., '/logs/', '//' for root contents) downloads its contents. No trailing slash (e.g. '/logs') downloads the directory itself.")
    dl_parser.add_argument("local_target_path", nargs='?', default=None, metavar="LOCAL_PATH", help="Local directory to download into, or local filename for a single remote file. If omitted, uses current directory. Trailing slash on path indicates a target directory.")
    
    run_parser = subparsers.add_parser("run", help="Run Python script on ESP32.")
    run_parser.add_argument("script_name", nargs='?', default="main.py", metavar="SCRIPT", help="Script to run (default: main.py). Path is relative to device root.")
    
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
        # Special handling for 'flash' and 'device' (test current) which might not have DEVICE_PORT set yet by user
        if args.cmd == "device" and not args.port_name: # 'esp32 device' to test current (or lack thereof)
            pass 
        elif args.cmd == "flash" and not DEVICE_PORT: 
             # Flash command will prompt for port if not set and continue, or use it if provided by --port
             pass # cmd_flash will handle missing DEVICE_PORT by erroring out if --port not also given
        else:
            print("Error: No COM port selected or configured.", file=sys.stderr)
            print("Use 'esp32 devices' to list available ports, then 'esp32 device <PORT_NAME>' to set one.", file=sys.stderr)
            sys.exit(1)

    if args.cmd == "help":
        parser.print_help()
        if not DEVICE_PORT: print(f"\nWarning: No port selected. Use 'esp32 devices' and 'esp32 device <PORT_NAME>'.")
    elif args.cmd == "devices": cmd_devices()
    elif args.cmd == "device":
        if args.port_name: cmd_device(args.port_name, args.force)
        elif DEVICE_PORT: 
            print(f"Current selected COM port is {DEVICE_PORT}. Testing...")
            ok, msg = test_device(DEVICE_PORT); print(msg)
        else: 
            print("No COM port currently selected or configured.")
            cmd_devices() 
            print(f"\nUse 'esp32 device <PORT_NAME>' to set one.")
    elif args.cmd == "flash":
        # If DEVICE_PORT is not set (e.g. first run, no config), and --port not given, cmd_flash will exit.
        cmd_flash(args.firmware_source, args.baud)
    elif args.cmd == "upload": cmd_upload(args.local_source, args.remote_destination)
    elif args.cmd == "run": run_script(args.script_name)
    elif args.cmd == "list": list_remote(args.remote_directory)
    elif args.cmd == "tree": tree_remote(args.remote_directory)
    elif args.cmd == "download": cmd_download(args.remote_source_path, args.local_target_path)
    elif args.cmd == "delete": delete_remote(args.remote_path_to_delete)
    elif args.cmd == "diagnostics": cmd_diagnostics() 

if __name__ == "__main__":
    main()