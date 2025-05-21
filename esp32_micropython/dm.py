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
import urllib.request # Added for firmware download
import tempfile     # Added for temporary firmware file
import shutil       # Added for file operations (like copyfileobj)

CONFIG_FILE = Path(__file__).parent / ".esp32_deploy_config.json"
DEVICE_PORT = None # Will be set by main after parsing args or loading config
DEFAULT_FIRMWARE_URL = "https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20250415-v1.25.0.bin"

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
    """
    Runs an mpremote command.
    mpremote_args_list: list of arguments for mpremote AFTER 'connect <port>'.
    connect_port: The port to use. If None, uses global DEVICE_PORT.
    suppress_output: If True, stdout/stderr of mpremote are captured. Otherwise, streams to console.
    timeout: Optional timeout for the command.
    working_dir: Optional working directory for the subprocess.

    Returns: A subprocess.CompletedProcess object or None if port is not set.
    Exits script if mpremote is not found.
    """
    global DEVICE_PORT
    port_to_use = connect_port or DEVICE_PORT
    if not port_to_use:
        print("Error: Device port not set for mpremote command.", file=sys.stderr)
        return subprocess.CompletedProcess(mpremote_args_list, -99, stdout="", stderr="Device port not set")

    base_cmd = ["mpremote", "connect", port_to_use]
    full_cmd = base_cmd + mpremote_args_list

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
        return subprocess.CompletedProcess(full_cmd, -1, stdout="", stderr="TimeoutExpired executing mpremote")
    except Exception as e:
        return subprocess.CompletedProcess(full_cmd, -2, stdout="", stderr=f"Unexpected error: {e}")

# --- NEW FUNCTION: run_esptool_command ---
def run_esptool_command(esptool_args_list, suppress_output=False, timeout=None, working_dir=None):
    """
    Runs an esptool command.
    esptool_args_list: list of arguments for esptool.
    suppress_output: If True, stdout/stderr of esptool are captured. Otherwise, streams to console.
    timeout: Optional timeout for the command.
    working_dir: Optional working directory for the subprocess.

    Returns: A subprocess.CompletedProcess object.
    Exits script if esptool is not found.
    """
    base_cmd = ["esptool"]
    full_cmd = base_cmd + esptool_args_list
    # print(f"Executing: {' '.join(full_cmd)}") # For debugging

    try:
        if suppress_output:
            process = subprocess.run(full_cmd, capture_output=True, text=True, check=False, timeout=timeout, cwd=working_dir)
        else:
            # esptool usually provides useful progress output, so don't suppress by default
            process = subprocess.run(full_cmd, text=True, check=False, timeout=timeout, cwd=working_dir)
        return process
    except FileNotFoundError:
        print("Error: esptool command not found. Is it installed and in PATH? (esptool is required for flashing).", file=sys.stderr)
        sys.exit(1) # Critical error
    except subprocess.TimeoutExpired:
        # print(f"Timeout executing esptool command: {' '.join(full_cmd)}", file=sys.stderr)
        return subprocess.CompletedProcess(full_cmd, -1, stdout="", stderr="TimeoutExpired executing esptool")
    except Exception as e:
        # print(f"An unexpected error occurred running esptool command {' '.join(full_cmd)}: {e}", file=sys.stderr)
        return subprocess.CompletedProcess(full_cmd, -2, stdout="", stderr=f"Unexpected error: {e}")

def get_remote_path_stat(target_path_on_device):
    global DEVICE_PORT
    if not DEVICE_PORT:
        return None, None

    path_for_uos = f"/{target_path_on_device.lstrip('/')}" if target_path_on_device and target_path_on_device != "/" else "/"
    code = f"import uos; print(uos.stat('{path_for_uos}'))"
    
    result = run_mpremote_command(["exec", code], suppress_output=True, timeout=5)

    if result and result.returncode == 0 and result.stdout:
        stat_tuple_str = result.stdout.strip()
        try:
            if stat_tuple_str.startswith("(") and stat_tuple_str.endswith(")"):
                stat_tuple = eval(stat_tuple_str) 
                mode = stat_tuple[0]
                S_IFDIR = 0x4000
                S_IFREG = 0x8000
                if mode & S_IFDIR: return "dir", stat_tuple
                elif mode & S_IFREG: return "file", stat_tuple
                else: return "unknown", stat_tuple
            else: return None, None
        except Exception: return None, None
    return None, None


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

# --- MODIFIED FUNCTION: test_device ---
def test_device(port, timeout=5):
    result = run_mpremote_command(["fs", "ls", ":"], connect_port=port, suppress_output=True, timeout=timeout)
    if result and result.returncode == 0:
        return True, f"Device on {port} responded (mpremote fs ls successful)."
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "No response or mpremote error."
        if result and result.returncode == -99: err_msg = result.stderr # Port not set from run_mpremote
        # --- Appended suggestion ---
        suggestion = (
            "Ensure the device is properly connected (try holding BOOT while plugging in, then release BOOT after a few seconds) "
            "and flashed with MicroPython. You can use the 'esp32 flash <firmware_file_or_url>' command to flash it."
        )
        return False, f"No response or error on {port}. Details: {err_msg}\n{suggestion}"

# --- NEW FUNCTION: test_micropython_presence ---
def test_micropython_presence(port, timeout=10):
    """
    Tests if a MicroPython REPL is responsive and identifies as MicroPython.
    """
    global DEVICE_PORT # Though port is passed, run_mpremote_command might use global if port is None
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
    elif result and result.returncode == -99: # Port not set error from run_mpremote_command
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
    
    ok, result_msg = test_device(port_arg) # test_device now includes flashing advice on failure
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


def run_cmd_output(mpremote_args_list):
    result = run_mpremote_command(mpremote_args_list, suppress_output=True)
    if result and result.returncode == 0:
        return result.stdout.splitlines()
    return []


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
        
        result = run_mpremote_command(["fs", "mkdir", f":{current_remote_path_str}"], suppress_output=True)

        if result and result.returncode == 0:
            continue
        elif result and result.stderr and ("EEXIST" in result.stderr or "File exists" in result.stderr):
            continue
        else:
            err_msg = result.stderr.strip() if result and result.stderr else f"Unknown error creating ':{current_remote_path_str}'"
            if result and not result.stderr and result.stdout:
                 err_msg = result.stdout.strip()
            print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
            return False
            
    return True

def cmd_upload(local_src_arg, remote_dest_arg=None):
    global DEVICE_PORT # Port check is assumed to be done in main

    # 1. Validate and characterize local source path
    # Use the original local_src_arg for mpremote to respect relative paths.
    # For local filesystem checks (exists, is_dir), use an absolute path.
    # Strip trailing slash for these checks, but remember if it was there.
    
    had_trailing_slash = local_src_arg.endswith(("/", os.sep))
    local_src_for_checks_str = local_src_arg
    if had_trailing_slash:
        local_src_for_checks_str = local_src_arg.rstrip("/" + os.sep)
        # If local_src_arg was just "/" or "C:/", rstrip might make it empty or "C:", handle carefully
        if not local_src_for_checks_str and Path(local_src_arg).is_absolute(): # e.g. user typed "/"
             local_src_for_checks_str = local_src_arg # Keep it as is for Path object if it's root

    abs_local_path = Path(os.path.abspath(local_src_for_checks_str))

    if not abs_local_path.exists():
        print(f"Error: Local path '{local_src_arg}' (resolved to '{abs_local_path}') does not exist.", file=sys.stderr)
        sys.exit(1)

    is_local_file = abs_local_path.is_file()
    is_local_dir = abs_local_path.is_dir()

    if not is_local_file and not is_local_dir: # Should be caught by exists(), but as a safeguard
        print(f"Error: Local path '{local_src_arg}' is neither a file nor a directory.", file=sys.stderr)
        sys.exit(1)

    if is_local_file and had_trailing_slash:
        print(f"Warning: Trailing slash on a local_file path '{local_src_arg}' is ignored. Treating as file '{abs_local_path.name}'.")
        # For mpremote source, use the path without the slash.
        mpremote_local_source_arg = local_src_for_checks_str
    elif is_local_dir and not had_trailing_slash:
        # Uploading the directory itself
        mpremote_local_source_arg = local_src_for_checks_str
    else: # is_local_dir and had_trailing_slash (contents) OR is_local_file (no slash originally)
        mpremote_local_source_arg = local_src_arg # Use original (e.g. with slash for contents logic trigger)


    # 2. Determine and prepare remote target directory
    # This is the directory ON THE DEVICE into which items will be placed, or where the new dir will be created.
    effective_remote_parent_dir_str = ""
    if remote_dest_arg:
        effective_remote_parent_dir_str = remote_dest_arg.replace(os.sep, "/").strip("/")
        
    if effective_remote_parent_dir_str: # Not root
        print(f"Ensuring remote target directory ':{effective_remote_parent_dir_str}' exists...")
        if not ensure_remote_dir(effective_remote_parent_dir_str):
            # ensure_remote_dir already prints error
            sys.exit(1)

    # 3. Perform upload based on determined scenario

    if is_local_file:
        local_file_basename = abs_local_path.name
        # Destination for the file on the device
        mpremote_target_file_spec_str = f":{effective_remote_parent_dir_str}/{local_file_basename}" if effective_remote_parent_dir_str else f":{local_file_basename}"
        
        print(f"Uploading file '{mpremote_local_source_arg}' to '{mpremote_target_file_spec_str}' on device...")
        cp_args = ["fs", "cp", mpremote_local_source_arg, mpremote_target_file_spec_str]
        result = run_mpremote_command(cp_args, suppress_output=True)
        
        if result and result.returncode == 0:
            print("File upload complete.")
        else:
            err_msg = result.stderr.strip() if result and result.stderr else "File upload failed"
            if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
            print(f"Error uploading file '{mpremote_local_source_arg}': {err_msg}", file=sys.stderr)
            sys.exit(1)

    elif is_local_dir:
        if had_trailing_slash:
            # Scenario: Upload contents of local_dir_path/ to :effective_remote_parent_dir_str/
            mpremote_target_dir_for_contents_spec_str = f":{effective_remote_parent_dir_str}/" if effective_remote_parent_dir_str else ":/"
            
            print(f"Uploading contents of local directory '{local_src_arg}' to '{mpremote_target_dir_for_contents_spec_str}' on device...")
            
            # abs_local_path is the directory whose contents we iterate
            items_in_local_dir = list(abs_local_path.iterdir())
            if not items_in_local_dir:
                print(f"Warning: Local directory '{local_src_arg}' (resolved to '{abs_local_path}') is empty. Nothing to upload.")
                return

            success_count = 0
            fail_count = 0
            for item in items_in_local_dir:
                # For individual items, use their resolved absolute paths as source for mpremote
                # to avoid issues if the script's CWD is different from where 'local_src_arg' was relative to.
                item_src_for_mpremote = str(item.resolve())
                
                cp_args = ["fs", "cp"]
                if item.is_dir():
                    cp_args.append("-r")
                cp_args.extend([item_src_for_mpremote, mpremote_target_dir_for_contents_spec_str])
                
                print(f"  Uploading '{item.name}'...")
                result = run_mpremote_command(cp_args, suppress_output=True)
                if result and result.returncode == 0:
                    success_count += 1
                else:
                    fail_count += 1
                    err_msg = result.stderr.strip() if result and result.stderr else "Upload failed"
                    if result and not err_msg and result.stdout: err_msg = result.stdout.strip() # Check stdout too
                    print(f"    Error uploading '{item.name}': {err_msg}", file=sys.stderr)
            
            print(f"Contents upload complete. {success_count} items succeeded, {fail_count} items failed.")
            if fail_count > 0:
                sys.exit(1)
        else:
            # Scenario: Upload local_dir itself into :effective_remote_parent_dir_str/
            # mpremote cp -r local_dir_name :/remote_parent/  => results in :/remote_parent/local_dir_name/...
            # So, the mpremote target is the PARENT directory on the remote.
            mpremote_target_parent_dir_spec_str = f":{effective_remote_parent_dir_str}/" if effective_remote_parent_dir_str else ":/"
            local_dir_basename = abs_local_path.name # This will be the name of the dir on the remote, inside the target parent.

            print(f"Uploading directory '{mpremote_local_source_arg}' to '{mpremote_target_parent_dir_spec_str}{local_dir_basename}' on device...")
            # mpremote_local_source_arg is already stripped of any trailing slash here.
            cp_args = ["fs", "cp", "-r", mpremote_local_source_arg, mpremote_target_parent_dir_spec_str]
            result = run_mpremote_command(cp_args, suppress_output=True)
            
            if result and result.returncode == 0:
                print("Directory upload complete.")
            else:
                err_msg = result.stderr.strip() if result and result.stderr else "Directory upload failed"
                if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
                print(f"Error uploading directory '{mpremote_local_source_arg}': {err_msg}", file=sys.stderr)
                sys.exit(1)
    else:
        # This case should not be reached due to prior checks
        print(f"Error: Unhandled local source type for '{local_src_arg}'.", file=sys.stderr)
        sys.exit(1)

def upload_all():
    global DEVICE_PORT
    me = os.path.basename(__file__)
    items_to_upload = []
    for item_name in os.listdir("."): 
        if item_name == me or item_name.startswith(".") or \
           item_name.endswith(".egg-info") or item_name == "__pycache__" or \
           item_name == ".esp32_deploy_config.json":
            continue
        items_to_upload.append(item_name)

    if not items_to_upload:
        print("No items to upload in current directory (after filtering).")
        return

    print(f"Starting to upload all eligible items from current directory to device root...")
    success_count = 0
    fail_count = 0

    for item_name in items_to_upload:
        item_path_obj = Path(item_name)
        abs_item_path_str = str(item_path_obj.resolve())
        
        print(f"  Uploading '{item_name}' to ':{item_name}'...")
        cp_args = ["fs", "cp"]
        if item_path_obj.is_dir():
            cp_args.append("-r")
        cp_args.extend([abs_item_path_str, f":{item_path_obj.name}"])
        
        result = run_mpremote_command(cp_args, suppress_output=True)
        if result and result.returncode == 0:
            success_count +=1
        else:
            fail_count += 1
            err_msg = result.stderr.strip() if result and result.stderr else "Upload failed"
            if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
            print(f"    Failed to upload '{item_name}': {err_msg}", file=sys.stderr)
            
    print(f"Upload all CWD complete. {success_count} items succeeded, {fail_count} items failed.")
    if fail_count > 0: sys.exit(1)


def run_script(script="main.py"):
    global DEVICE_PORT
    script_on_device_norm = script.lstrip('/')
    
    print(f"Checking for '{script_on_device_norm}' on device...")
    path_type, _ = get_remote_path_stat(script_on_device_norm)

    if path_type is None:
        print(f"Error: Script ':{script_on_device_norm}' not found on device.", file=sys.stderr)
        sys.exit(1)
    if path_type == 'dir':
        print(f"Error: Path ':{script_on_device_norm}' on device is a directory, not a runnable script.", file=sys.stderr)
        sys.exit(1)
    if path_type != 'file':
        print(f"Error: Path ':{script_on_device_norm}' on device is not a file.", file=sys.stderr)
        sys.exit(1)

    abs_script_path_on_device = f"/{script_on_device_norm}"
    python_code = f"exec(open('{abs_script_path_on_device}').read())"
    
    print(f"Running '{script_on_device_norm}' on {DEVICE_PORT}...")
    result = run_mpremote_command(["exec", python_code], suppress_output=False)
    
    if result and result.returncode != 0:
        sys.exit(1)


def cmd_download(remote_source_arg, local_destination_arg=None):
    global DEVICE_PORT

    remote_src_clean = remote_source_arg.strip()
    if not remote_src_clean or remote_src_clean == "/":
        print("Error: Remote source cannot be root for this download operation. Specify a file or directory.", file=sys.stderr)
        sys.exit(1)

    had_remote_trailing_slash = remote_src_clean.endswith("/")
    # remote_path_for_stat should not have a trailing slash for get_remote_path_stat
    # but keep original for mpremote if it had a slash (for contents copying)
    remote_path_on_device_for_stat = remote_src_clean.rstrip("/")

    print(f"Checking remote path ':{remote_path_on_device_for_stat}'...")
    remote_type, _ = get_remote_path_stat(remote_path_on_device_for_stat)

    if remote_type is None:
        print(f"Error: Remote path ':{remote_path_on_device_for_stat}' not found on device.", file=sys.stderr)
        sys.exit(1)
    if remote_type == "unknown":
        print(f"Error: Remote path ':{remote_path_on_device_for_stat}' is of an unknown type.", file=sys.stderr)
        sys.exit(1)

    # Determine local destination path
    local_base_destination_str = local_destination_arg or "."
    abs_local_base_dest_path = Path(local_base_destination_str).resolve()

    cp_args = ["fs", "cp"]
    success_msg = ""

    if remote_type == "file":
        if had_remote_trailing_slash: # e.g. "file.txt/" which is unusual but handle defensively
            print(f"Warning: Trailing slash on a remote file path ':{remote_src_clean}' is ignored. Treating as file ':{remote_path_on_device_for_stat}'.")
        
        mpremote_remote_spec = f":{remote_path_on_device_for_stat}"
        remote_file_basename = Path(remote_path_on_device_for_stat).name

        if abs_local_base_dest_path.is_dir() or \
           (not abs_local_base_dest_path.exists() and local_destination_arg and local_destination_arg.endswith(("/", os.sep))):
            # Target is an existing directory, or a path ending with slash (new dir)
            abs_local_base_dest_path.mkdir(parents=True, exist_ok=True) # Ensure dir exists
            final_local_target_path = abs_local_base_dest_path / remote_file_basename
        else:
            # Target is a file name, or a path not ending with slash (new file, possibly in new dir)
            final_local_target_path = abs_local_base_dest_path
            final_local_target_path.parent.mkdir(parents=True, exist_ok=True)

        cp_args.extend([mpremote_remote_spec, str(final_local_target_path)])
        print(f"Downloading remote file ':{remote_path_on_device_for_stat}' to '{final_local_target_path}'...")
        success_msg = "File download complete."

    elif remote_type == "dir":
        cp_args.append("-r") # Recursive for directories

        if had_remote_trailing_slash:
            # Download contents of remote_dir/ into local_dest/
            mpremote_remote_spec = f":{remote_path_on_device_for_stat}/" # mpremote needs trailing slash for contents
            final_local_target_dir = abs_local_base_dest_path
            final_local_target_dir.mkdir(parents=True, exist_ok=True)
            
            cp_args.extend([mpremote_remote_spec, str(final_local_target_dir)])
            print(f"Downloading contents of remote directory ':{remote_path_on_device_for_stat}/' to '{final_local_target_dir}/'...")
            success_msg = "Directory contents download complete."
        else:
            # Download remote_dir itself into local_dest/
            # mpremote cp -r :remote_dir local_dest/  => results in local_dest/remote_dir
            mpremote_remote_spec = f":{remote_path_on_device_for_stat}"
            final_local_target_parent_dir = abs_local_base_dest_path
            final_local_target_parent_dir.mkdir(parents=True, exist_ok=True)

            cp_args.extend([mpremote_remote_spec, str(final_local_target_parent_dir)])
            remote_dir_basename = Path(remote_path_on_device_for_stat).name
            print(f"Downloading remote directory ':{remote_path_on_device_for_stat}' into '{final_local_target_parent_dir / remote_dir_basename}'...")
            success_msg = "Directory download complete."

    result = run_mpremote_command(cp_args, suppress_output=True)

    if result and result.returncode == 0:
        print(success_msg)
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "Download operation failed"
        if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
        print(f"Error during download: {err_msg}", file=sys.stderr)
        sys.exit(1)

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

    help_parser = subparsers.add_parser("help", help="Show this help message and exit.")
    devices_parser = subparsers.add_parser("devices",help="List available COM ports and show the selected COM port.")
    dev_parser = subparsers.add_parser("device", help="Set or test the selected COM port for operations.")
    dev_parser.add_argument("port_name", nargs='?', metavar="PORT", help="The COM port to set. If omitted, tests current.")
    dev_parser.add_argument("--force", "-f", action="store_true", help="Force set port even if test fails.")
    
    # --- NEW: flash_parser ---
    flash_parser = subparsers.add_parser("flash", help="Download and flash MicroPython firmware to the ESP32-C3.")
    flash_parser.add_argument(
        "firmware_source",
        default=DEFAULT_FIRMWARE_URL, # Uses the global default
        nargs='?', # Makes it optional, will use default if not provided.
        help=f"URL to download the MicroPython .bin firmware, or a path to a local .bin file. (Default: {DEFAULT_FIRMWARE_URL})"
    )
    flash_parser.add_argument(
        "--baud",
        default="460800",
        help="Baud rate for flashing firmware (Default: 460800)."
    )

    up_parser = subparsers.add_parser("upload", help="Upload file/directory to ESP32.")
    up_parser.add_argument("local_source", help="Local file/dir. Trailing '/' on dir uploads contents.")
    up_parser.add_argument("remote_destination", nargs='?', default=None, help="Remote path/dir. Trailing '/' for dir target.")
    run_parser = subparsers.add_parser("run", help="Run Python script on ESP32.")
    run_parser.add_argument("script_name", nargs='?', default="main.py", metavar="SCRIPT", help="Script to run (default: main.py).")
    ls_parser = subparsers.add_parser("ls", help=argparse.SUPPRESS)
    ls_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    list_parser = subparsers.add_parser("list", help="List files/dirs on ESP32.")
    list_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    tree_parser = subparsers.add_parser("tree", help="Display remote file tree.")
    tree_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    
    dl_parser = subparsers.add_parser("download", help="Download file/directory from ESP32. Mirrors 'upload' logic.")
    dl_parser.add_argument("remote_source", metavar="REMOTE_SRC", help="Remote file/dir. Trailing '/' on dir downloads contents.")
    dl_parser.add_argument("local_destination", nargs='?', default=None, metavar="LOCAL_DEST", help="Local path/dir. If omitted, CWD is used.")
    
    del_parser = subparsers.add_parser("delete", help="Delete file/directory on ESP32.")
    del_parser.add_argument("remote_path_to_delete", metavar="REMOTE_PATH", nargs='?', default=None, help="Remote path. Omitting deletes root contents (confirm).")
    up_all_parser = subparsers.add_parser("upload_all_cwd", help="[Basic] Upload CWD items to ESP32 root.")

    args = parser.parse_args()

    if args.port: DEVICE_PORT = args.port
    elif "port" in cfg: DEVICE_PORT = cfg["port"]
    
    # --- ADDED "flash" ---
    commands_needing_port = ["device", "upload", "run", "ls", "list", "tree", "download", "delete", "upload_all_cwd", "flash"]
    is_device_command_setting_port = args.cmd == "device" and args.port_name
    is_device_command_testing_port = args.cmd == "device" and not args.port_name and DEVICE_PORT

    if args.cmd in commands_needing_port and not DEVICE_PORT and \
       not is_device_command_setting_port and args.cmd != "flash": # Flash handles its own port check message
        if args.cmd == "device" and not args.port_name :
            pass
        else:
            print("Error: No port selected. Use 'esp32 devices' and 'esp32 device <PORT_NAME>'.", file=sys.stderr)
            if args.cmd != "help" and args.cmd != "devices": cmd_devices()
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
            # Suggestion now part of test_device output
            # if not ok: print(f"Consider re-setting with 'esp32 device {DEVICE_PORT}'.")
        else: 
            print("No COM port currently selected or configured.")
            cmd_devices()
            print(f"\nUse 'esp32 device <PORT_NAME>' to set one.")
    # --- NEW: flash command handling ---
    elif args.cmd == "flash":
        cmd_flash(args.firmware_source, args.baud)
    elif args.cmd == "upload": cmd_upload(args.local_source, args.remote_destination)
    elif args.cmd == "run": run_script(args.script_name)
    elif args.cmd == "ls" or args.cmd == "list": list_remote(args.remote_directory)
    elif args.cmd == "tree": tree_remote(args.remote_directory)
    elif args.cmd == "download": cmd_download(args.remote_source, args.local_destination)
    elif args.cmd == "delete": delete_remote(args.remote_path_to_delete)
    elif args.cmd == "upload_all_cwd": upload_all()

if __name__ == "__main__":
    main()