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

    # target_path_on_device is expected to be relative to root, e.g., "file.txt", "somedir/file.txt", or "" for root.
    path_for_uos = f"/{target_path_on_device.lstrip('/')}" if target_path_on_device and target_path_on_device != "/" else "/"
    code = f"import uos; print(uos.stat('{path_for_uos}'))"
    
    result = run_mpremote_command(["exec", code], suppress_output=True, timeout=10) # Increased timeout slightly

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
            else: return None, None # Could be an error message from uos.stat
        except Exception: return None, None
    # Handle cases where uos.stat itself fails (e.g., file not found)
    if result and result.stderr and "ENOENT" in result.stderr: # No such file or directory
        return None, None
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
    if not normalized_path: # Root directory always exists
        return True

    parts = Path(normalized_path).parts
    current_remote_path_str = ""

    for part in parts:
        if not current_remote_path_str:
            current_remote_path_str = part
        else:
            current_remote_path_str = f"{current_remote_path_str}/{part}"
        
        # Use get_remote_path_stat to check if it exists and is a dir
        # path_type, _ = get_remote_path_stat(current_remote_path_str)
        # if path_type == "dir":
        #     continue
        # if path_type == "file":
        #     print(f"Error: Remote path ':{current_remote_path_str}' exists and is a file, cannot create directory.", file=sys.stderr)
        #     return False
            
        # If not a dir (None or file), try to create it
        result = run_mpremote_command(["fs", "mkdir", f":{current_remote_path_str}"], suppress_output=True)

        if result and result.returncode == 0:
            continue # Successfully created
        elif result and result.stderr and ("EEXIST" in result.stderr or "File exists" in result.stderr):
            # Check if the existing path is indeed a directory
            # This is important because mpremote mkdir EEXIST doesn't distinguish file/dir conflict
            path_type_check, _ = get_remote_path_stat(current_remote_path_str)
            if path_type_check == "dir":
                continue # It exists and is a directory, which is fine
            else:
                err_msg = f"Path ':{current_remote_path_str}' exists but is not a directory."
                print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
                return False
        else:
            err_msg = result.stderr.strip() if result and result.stderr else f"Unknown error creating ':{current_remote_path_str}'"
            if result and not result.stderr and result.stdout: # Sometimes mpremote puts mkdir errors to stdout
                 err_msg = result.stdout.strip()
            print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
            return False
            
    return True

def cmd_upload(local_src_arg, remote_dest_arg=None):
    global DEVICE_PORT 

    had_trailing_slash_local = local_src_arg.endswith(("/", os.sep))
    local_src_for_checks_str = local_src_arg
    if had_trailing_slash_local:
        local_src_for_checks_str = local_src_arg.rstrip("/" + os.sep)
        if not local_src_for_checks_str and Path(local_src_arg).is_absolute():
             local_src_for_checks_str = local_src_arg

    abs_local_path = Path(os.path.abspath(local_src_for_checks_str))

    if not abs_local_path.exists():
        print(f"Error: Local path '{local_src_arg}' (resolved to '{abs_local_path}') does not exist.", file=sys.stderr)
        sys.exit(1)

    is_local_file = abs_local_path.is_file()
    is_local_dir = abs_local_path.is_dir()

    if not is_local_file and not is_local_dir:
        print(f"Error: Local path '{local_src_arg}' is neither a file nor a directory.", file=sys.stderr)
        sys.exit(1)

    # mpremote_local_source_arg is what mpremote will use for the source.
    # It should retain trailing slash if user meant to copy contents of a dir.
    if is_local_file and had_trailing_slash_local:
        print(f"Warning: Trailing slash on a local file path '{local_src_arg}' is ignored. Treating as file '{abs_local_path.name}'.")
        mpremote_local_source_arg = str(abs_local_path) # Use resolved absolute path for files
    elif is_local_dir and not had_trailing_slash_local: # Uploading the directory itself
        mpremote_local_source_arg = str(abs_local_path) # Use resolved absolute path
    elif is_local_dir and had_trailing_slash_local: # Uploading contents of the directory
        # mpremote needs the trailing slash for its 'cp dir/' logic
        mpremote_local_source_arg = str(abs_local_path) + os.sep
    else: # is_local_file (no slash originally)
        mpremote_local_source_arg = str(abs_local_path)


    effective_remote_parent_dir_str = ""
    if remote_dest_arg:
        effective_remote_parent_dir_str = remote_dest_arg.replace(os.sep, "/").strip("/")
        
    if effective_remote_parent_dir_str:
        print(f"Ensuring remote target directory ':{effective_remote_parent_dir_str}' exists...")
        if not ensure_remote_dir(effective_remote_parent_dir_str):
            sys.exit(1)

    if is_local_file:
        local_file_basename = abs_local_path.name
        mpremote_target_path_on_device = f":{effective_remote_parent_dir_str}/{local_file_basename}" if effective_remote_parent_dir_str else f":{local_file_basename}"
        
        print(f"Uploading file '{local_src_arg}' to '{mpremote_target_path_on_device}' on device...")
        # For file upload, mpremote_local_source_arg should be the specific file path.
        # Using abs_local_path ensures mpremote can find it regardless of CWD.
        cp_args = ["fs", "cp", str(abs_local_path), mpremote_target_path_on_device]
        result = run_mpremote_command(cp_args, suppress_output=True)
        
        if result and result.returncode == 0:
            print("File upload complete.")
        else:
            err_msg = result.stderr.strip() if result and result.stderr else "File upload failed"
            if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
            print(f"Error uploading file '{local_src_arg}': {err_msg}", file=sys.stderr)
            sys.exit(1)

    elif is_local_dir:
        if had_trailing_slash_local:
            # Upload contents of local_dir_path/ to :effective_remote_parent_dir_str/
            # mpremote cp -r local_dir/ :/remote_target_dir/
            mpremote_target_dir_for_contents_spec_str = f":{effective_remote_parent_dir_str}/" if effective_remote_parent_dir_str else ":/"
            
            print(f"Uploading contents of local directory '{local_src_arg}' to '{mpremote_target_dir_for_contents_spec_str}' on device...")
            
            # mpremote_local_source_arg is already abs_local_path + os.sep
            cp_args = ["fs", "cp", "-r", mpremote_local_source_arg, mpremote_target_dir_for_contents_spec_str]
            result = run_mpremote_command(cp_args, suppress_output=True) # mpremote handles iteration

            if result and result.returncode == 0:
                print(f"Contents of '{local_src_arg}' uploaded successfully.")
            else:
                err_msg = result.stderr.strip() if result and result.stderr else "Directory contents upload failed"
                if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
                print(f"Error uploading contents of '{local_src_arg}': {err_msg}", file=sys.stderr)
                sys.exit(1)
        else:
            # Upload local_dir itself into :effective_remote_parent_dir_str/
            # mpremote cp -r local_dir :/remote_parent/  => results in :/remote_parent/local_dir_basename/...
            mpremote_target_parent_dir_spec_str = f":{effective_remote_parent_dir_str}/" if effective_remote_parent_dir_str else ":/"
            local_dir_basename = abs_local_path.name

            print(f"Uploading directory '{local_src_arg}' to '{mpremote_target_parent_dir_spec_str}{local_dir_basename}' on device...")
            # mpremote_local_source_arg is abs_local_path (no trailing slash)
            cp_args = ["fs", "cp", "-r", mpremote_local_source_arg, mpremote_target_parent_dir_spec_str]
            result = run_mpremote_command(cp_args, suppress_output=True)
            
            if result and result.returncode == 0:
                print("Directory upload complete.")
            else:
                err_msg = result.stderr.strip() if result and result.stderr else "Directory upload failed"
                if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
                print(f"Error uploading directory '{local_src_arg}': {err_msg}", file=sys.stderr)
                sys.exit(1)
    else:
        print(f"Error: Unhandled local source type for '{local_src_arg}'.", file=sys.stderr)
        sys.exit(1)


def cmd_download(remote_src_arg, local_dest_arg=None):
    global DEVICE_PORT

    # 1. Validate and characterize remote source path
    had_trailing_slash_remote = remote_src_arg.endswith("/")
    # remote_src_for_checks_str is the path string without trailing slash, used for logic.
    # Needs to handle if remote_src_arg is just "/" or "//"
    if remote_src_arg == "/" or remote_src_arg == "//":
        remote_src_for_checks_str = "/"
    else:
        remote_src_for_checks_str = remote_src_arg.rstrip("/")
    
    # path_for_stat is what get_remote_path_stat expects: relative to root, or "" for root.
    if remote_src_for_checks_str == "/":
        path_for_stat = "" # Represents the root directory for get_remote_path_stat
    else:
        path_for_stat = remote_src_for_checks_str.lstrip('/')

    # Handle "download /" case early
    if remote_src_arg == "/" and not had_trailing_slash_remote: # User typed `download /`
        print("Error: Ambiguous command 'download /'.", file=sys.stderr)
        print("  To download contents of the root directory, use 'download // [local_path]'.", file=sys.stderr)
        print("  To download a specific item from root, use 'download /item_name [local_path]'.", file=sys.stderr)
        sys.exit(1)

    print(f"Checking remote path ':{path_for_stat if path_for_stat else '/'}'...")
    if path_for_stat == "": # Root directory is always a dir
        remote_type = "dir"
    else:
        remote_type, _ = get_remote_path_stat(path_for_stat)

    if remote_type is None:
        print(f"Error: Remote path ':{path_for_stat if path_for_stat else '/'}' not found on device.", file=sys.stderr)
        sys.exit(1)

    # 2. Determine local destination path and ensure target directory exists.
    final_mpremote_local_dest_str: str
    dir_to_ensure_exists: Path

    if remote_type == "file":
        # For a remote file, its basename will be used for the local filename.
        # local_dest_arg (or CWD if None) is the directory where this file will be placed.
        remote_basename = Path(path_for_stat).name # path_for_stat is like "some/file.txt" or "file.txt"
        
        target_local_dir = Path(os.path.abspath(local_dest_arg or "."))
        final_mpremote_local_dest_str = str(target_local_dir / remote_basename)
        dir_to_ensure_exists = target_local_dir
    
    else: # remote_type == "dir"
        # local_dest_arg (or CWD if None) is the target base directory for mpremote.
        # mpremote cp -r :/remote_dir ./local_target -> creates ./local_target/remote_dir
        # mpremote cp -r :/remote_dir/ ./local_target -> copies contents into ./local_target/
        target_local_base_dir = Path(os.path.abspath(local_dest_arg or "."))
        final_mpremote_local_dest_str = str(target_local_base_dir)
        dir_to_ensure_exists = target_local_base_dir

    print(f"Ensuring local target directory '{dir_to_ensure_exists}' exists...")
    dir_to_ensure_exists.mkdir(parents=True, exist_ok=True)

    # 3. Construct mpremote remote source string for 'fs cp'
    # It always starts with ':', followed by the path.
    # For root, path_for_stat is "", mpremote needs ":/" for contents, or just ":" for the dir itself (less common for cp src)
    
    if path_for_stat == "": # Source is root
        mpremote_remote_source_str = ":/" # For 'cp -r :/', implies contents of root
        if not had_trailing_slash_remote:
            # This case should have been caught by (remote_src_arg == "/" and not had_trailing_slash_remote)
            # However, if we ever allowed "download /" to mean "download root dir itself", it would be ":"
            # but "cp -r : local_dest" is not standard for mpremote.
            # For safety, ensure it's for contents if path_for_stat is ""
             mpremote_remote_source_str = ":/"
    else:
        mpremote_remote_source_str = f":{path_for_stat}"
        if had_trailing_slash_remote and remote_type == "dir":
            mpremote_remote_source_str += "/"
    
    # 4. Construct mpremote command and run
    cp_args = ["fs", "cp"]
    if remote_type == "dir":
        cp_args.append("-r")
    
    cp_args.extend([mpremote_remote_source_str, final_mpremote_local_dest_str])

    # Print informative message
    if remote_type == "file":
        print(f"Downloading remote file '{mpremote_remote_source_str}' to local path '{final_mpremote_local_dest_str}'...")
    elif had_trailing_slash_remote: # Directory contents
        print(f"Downloading contents of remote directory '{mpremote_remote_source_str}' to local directory '{final_mpremote_local_dest_str}'...")
    else: # Directory itself
        # When downloading a dir "rdir" to "ldir", mpremote creates "ldir/rdir"
        # path_for_stat here is the "rdir" part.
        remote_dir_name = Path(path_for_stat).name if path_for_stat else "root_contents" # Should not be "root_contents" due to earlier checks
        if path_for_stat == "": remote_dir_name = "device_root_content" # placeholder if somehow reached
        
        # final_mpremote_local_dest_str is the PARENT local directory.
        expected_local_path = Path(final_mpremote_local_dest_str) / remote_dir_name
        print(f"Downloading remote directory '{mpremote_remote_source_str}' to local path '{expected_local_path}'...")

    result = run_mpremote_command(cp_args, suppress_output=True)

    if result and result.returncode == 0:
        print("Download complete.")
    else:
        err_parts = []
        if result and result.stdout: err_parts.append(result.stdout.strip())
        if result and result.stderr: err_parts.append(result.stderr.strip())
        
        err_msg = "; ".join(filter(None, err_parts))
        if not err_msg : err_msg = f"{remote_type.capitalize()} download failed with mpremote exit code {result.returncode if result else 'N/A'}"
        
        print(f"Error downloading from '{mpremote_remote_source_str}': {err_msg}", file=sys.stderr)
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
        abs_item_path_str = str(item_path_obj.resolve()) # Use absolute path for mpremote source
        
        print(f"  Uploading '{item_name}' to ':{item_name}'...")
        cp_args = ["fs", "cp"]
        if item_path_obj.is_dir():
            cp_args.append("-r")
        # Target is :/item_name for files, or :/ for dirs (mpremote creates dir_name inside)
        # For dirs, cp -r local_dir :/ creates :/local_dir
        # For files, cp local_file :/file_name creates :/file_name
        # The target for mpremote cp should be the container or the final name.
        # To place items at root using their names:
        if item_path_obj.is_dir():
            # cp -r abs_item_path_str :/  -> will create /abs_item_path_str on device. We want /item_name
            # So, source should be item_name (relative) if CWD is '.', or its full path.
            # Destination is :/ (mpremote then creates item_name in root)
             target_on_device = ":/" # mpremote will use basename of source
        else:
             target_on_device = f":{item_path_obj.name}"


        current_item_source_for_mpremote = abs_item_path_str

        final_cp_args = ["fs", "cp"]
        if item_path_obj.is_dir():
            final_cp_args.append("-r")
        # The source for mpremote is the local item.
        # The destination for mpremote is the parent directory on the device (e.g. ":/")
        # mpremote will then create the item with its name inside that parent.
        final_cp_args.extend([current_item_source_for_mpremote, ":/"])


        result = run_mpremote_command(final_cp_args, suppress_output=True)
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
        # No sys.exit(1) here, as the script itself might exit with non-zero code,
        # and mpremote would have already printed script's output/errors.
        # We only exit if mpremote command itself failed fundamentally before running the script's code.
        # However, mpremote exec "code" usually returns 0 even if "code" has runtime error.
        # For now, let's keep it as is; if script has unhandled exception, mpremote prints it.
        pass


def list_remote_capture(remote_dir_arg=None): 
    global DEVICE_PORT
    if not DEVICE_PORT: return []

    path_for_walk = f"/{remote_dir_arg.strip('/')}" if remote_dir_arg and remote_dir_arg.strip('/') else "/"
    
    code = f"""\\
import uos
def _walk(p):
    try:
        items = uos.ilistdir(p) 
    except OSError as e:
        if e.args[0] == 2: # ENOENT
            print("Error: Directory '" + p + "' not found for listing.", file=sys.stderr)
        else:
            print("Error listing directory '" + p + "': " + str(e), file=sys.stderr)
        return
    for name, typ, *_ in items:
        base_path = p.rstrip('/')
        item_full_path = base_path + '/' + name if base_path != '' and base_path != '/' else '/' + name
        if base_path == '/': item_full_path = '/' + name # handles root listing better
        print(item_full_path) 
        if typ == 0x4000: # STAT_DIR
            _walk(item_full_path)
_walk('{path_for_walk}')
"""
    # print(f"Executing for list_remote_capture: {code}") # Debug
    lines = run_cmd_output(["exec", code])
    # Filter out potential error messages printed to stdout by the script
    return [line for line in lines if line.startswith('/') and not line.startswith("Error:")]


def list_remote(remote_dir=None):
    global DEVICE_PORT
    normalized_remote_dir = (remote_dir or "").strip("/")
    # For display and get_remote_path_stat, if it's root, normalized_remote_dir will be ""
    # For list_remote_capture, "" means root.
    
    display_dir_name = f":{normalized_remote_dir or '/'}"
    path_for_stat_check = normalized_remote_dir # "" for root, "dir" for a dir

    if path_for_stat_check: # Not root, check if it exists and is a dir
        path_type, _ = get_remote_path_stat(path_for_stat_check)
        if path_type is None:
            print(f"Error: Remote path '{display_dir_name}' not found.", file=sys.stderr)
            return
        if path_type == 'file':
            print(f"Error: '{display_dir_name}' is a file, not a directory. Use 'download' for files.", file=sys.stderr)
            return
    
    print(f"Listing contents of '{display_dir_name}'...")
    # list_remote_capture expects "" for root, or "somedir"
    all_paths_abs = list_remote_capture(normalized_remote_dir if normalized_remote_dir else None)


    if not all_paths_abs:
        # Check if the directory itself actually exists, or if list_remote_capture had an issue
        # (e.g. permission error, though get_remote_path_stat should catch non-existence)
        # If path_for_stat_check is empty (root), it always exists.
        # if path_for_stat_check and get_remote_path_stat(path_for_stat_check)[0] is None:
        #     # This case should be caught by the initial check
        #     pass 
        # else:
        print(f"Directory '{display_dir_name}' is empty or no items found.")
        return
    
    # Prepare for potentially displaying a subset if remote_dir was specified
    # all_paths_abs contains absolute paths like /foo, /foo/bar
    # If normalized_remote_dir is "foo", we want to show "foo/bar" as "bar" relative to "foo" (or keep absolute)
    # The current implementation lists absolute paths that are AT or UNDER the target.
    
    # If listing root (normalized_remote_dir is ""), show all absolute paths.
    # If listing a subdir (e.g., "foo"), show paths like "/foo/file.txt", "/foo/bar/baz.txt"
    # The request seems to be to list them as they are (absolute).
    
    # The original list_remote logic was a bit complex with stripping prefixes.
    # Let's simplify: just print the absolute paths that are relevant.
    
    # If normalized_remote_dir is empty (listing root):
    # Print all items directly (e.g. /main.py, /lib/)
    # If normalized_remote_dir is "lib" (listing /lib):
    # Print only items starting with "/lib/" (e.g. /lib/utils.py)

    printed_any = False
    if not normalized_remote_dir: # Listing root
        for path_str in sorted(all_paths_abs):
             # Only print top-level items for root listing, not recursive items from sub-folders
            if Path(path_str).parent == Path('/'):
                print(path_str.lstrip('/')) # e.g. main.py, lib
                printed_any = True
    else: # Listing a subdirectory
        # We need to list items *directly within* normalized_remote_dir
        # And also recursively if that was the old behavior (list_remote_capture is recursive)
        # The prompt's "ls" is typically non-recursive for the top level.
        # However, the previous `list_remote` using `list_remote_capture` was effectively recursive.
        # Let's stick to showing all captured (recursive) paths that fall under the specified dir.
        prefix_to_match = f"/{normalized_remote_dir}/"
        direct_match = f"/{normalized_remote_dir}" # For the directory itself if it were listed somehow

        for path_str in sorted(all_paths_abs):
            if path_str.startswith(prefix_to_match) or path_str == direct_match:
                # Display path relative to the listed directory, or absolute?
                # User expectation for "ls /foo" is to see "bar.py", not "/foo/bar.py"
                # But list_remote_capture gives absolute.
                # The original code `print(path_str.lstrip('/'))` prints `foo/bar.py`
                print(path_str.lstrip('/')) 
                printed_any = True
            # If normalized_remote_dir is, say, "foo", and path_str is "/foo" (the dir itself)
            # this logic might not directly list it. list_remote_capture lists *contents*.
            # Let's assume list_remote_capture works as intended for now.

    if not printed_any:
         print(f"Directory '{display_dir_name}' is empty or no matching items found by the current filter.")


def tree_remote(remote_dir=None):
    global DEVICE_PORT
    base_path_str_norm = (remote_dir or "").strip("/") # "" for root, "foo" for /foo
    display_root_name = f":{base_path_str_norm or '/'}"

    if base_path_str_norm: # Not root, check existence
        path_type, _ = get_remote_path_stat(base_path_str_norm)
        if path_type is None:
            print(f"Error: Remote directory '{display_root_name}' not found.", file=sys.stderr)
            return
        if path_type == 'file':
            print(f"Error: '{display_root_name}' is a file. Cannot display as tree.", file=sys.stderr)
            return
    
    print(f"Tree for '{display_root_name}' on device:")
    # list_remote_capture expects None for root, or "foo"
    lines_abs = list_remote_capture(base_path_str_norm if base_path_str_norm else None) 
    
    if not lines_abs:
        print(f"Directory '{display_root_name}' is empty.")
        return

    # paths_for_tree_build should be relative to the base_path_str_norm
    paths_for_tree_build = []
    if not base_path_str_norm: # Tree from root
        # lines_abs are like "/foo", "/foo/bar.py". Path objects: "foo", "foo/bar.py"
        paths_for_tree_build = [Path(p.lstrip('/')) for p in lines_abs if p != "/"]
    else: # Tree from a subdirectory e.g. "lib"
        # lines_abs are like "/lib/one.py", "/lib/sub/two.py"
        # We want Path("one.py"), Path("sub/two.py")
        prefix_to_remove = f"/{base_path_str_norm}/"
        len_prefix = len(prefix_to_remove)
        for line_abs in lines_abs:
            if line_abs.startswith(prefix_to_remove):
                relative_part = line_abs[len_prefix:]
                if relative_part: # Ensure not empty if line_abs was just prefix_to_remove (unlikely)
                    paths_for_tree_build.append(Path(relative_part))
            # elif line_abs == f"/{base_path_str_norm}": # If the dir itself is in the list (not typical for ilistdir contents)
            #     pass # Don't add empty path

    if not paths_for_tree_build :
        print(f"Directory '{display_root_name}' contains no listable items for tree display.")
        return

    structure = {}
    # Sort paths by parts to ensure parents are processed first (though setdefault handles it)
    sorted_paths_for_tree = sorted(list(set(paths_for_tree_build)), key=lambda p: p.parts)

    for p_obj in sorted_paths_for_tree:
        current_level = structure
        parts = [part for part in p_obj.parts if part and part != '/']
        if not parts: continue

        for part_idx, part in enumerate(parts):
            is_last_part = (part_idx == len(parts) - 1)
            # If it's the last part, and the original path p_obj represents this part directly
            # (i.e., p_obj itself is not a directory leading to further parts in other paths),
            # mark it distinctly if needed, but setdefault to {} is fine.
            current_level = current_level.setdefault(part, {})
            
    def print_tree_nodes(node, prefix=""):
        children_names = sorted(node.keys())
        for i, child_name in enumerate(children_names):
            connector = "└── " if i == len(children_names) - 1 else "├── "
            print(f"{prefix}{connector}{child_name}")
            if node[child_name]: # If it has further children, recurse
                new_prefix = prefix + ("    " if i == len(children_names) - 1 else "│   ")
                print_tree_nodes(node[child_name], new_prefix)

    # Print root of the tree display
    if not base_path_str_norm:
        print(".") 
    else:
        print(f". ({base_path_str_norm})")
    
    print_tree_nodes(structure, "")


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
        # Using mpremote ls : as it's simpler than exec for just names at root
        ls_result = run_mpremote_command(["fs", "ls", ":"], suppress_output=True, timeout=10)
        
        if not ls_result or ls_result.returncode != 0:
            err = ls_result.stderr.strip() if ls_result and ls_result.stderr else "Failed to connect or list root."
            print(f"Error listing root directory for deletion: {err}", file=sys.stderr)
            sys.exit(1)

        # Process ls output: "  item1\r\n  item2\r\n" or "  123 item1\r\n" (from some versions)
        # We only want the names.
        items_to_delete_from_root = []
        if ls_result.stdout:
            for line in ls_result.stdout.splitlines():
                line_content = line.strip()
                if not line_content: continue
                # Remove potential size prefix if mpremote ls output includes it (e.g. "123 item_name")
                parts = line_content.split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    items_to_delete_from_root.append(parts[1])
                else:
                    items_to_delete_from_root.append(line_content) # Assume whole line is name
        
        if not items_to_delete_from_root:
            print("Root directory is already empty or no items found by ls.")
            return

        print(f"Items to delete from root: {items_to_delete_from_root}")
        all_successful = True
        for item_name_to_delete in items_to_delete_from_root:
            item_target_for_mpremote = ":" + item_name_to_delete # Already relative to root
            
            print(f"Deleting '{item_target_for_mpremote}'...")
            # Use -r for directories, but fs rm also works on files without -r.
            # To be safe and handle both files and dirs listed by `ls :`
            del_result = run_mpremote_command(["fs", "rm", "-r", item_target_for_mpremote], suppress_output=True)
            
            if del_result and del_result.returncode == 0:
                pass # Success
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
        # Delete specific file or directory
        normalized_path_for_stat = remote_path_arg.strip('/') # for get_remote_path_stat
        mpremote_target_path = ":" + normalized_path_for_stat   # for mpremote command

        path_type, _ = get_remote_path_stat(normalized_path_for_stat)

        if path_type is None:
            print(f"Error: Remote path '{mpremote_target_path}' does not exist on device.", file=sys.stderr)
            # Changed from return to sys.exit(1) for consistency, as delete implies it should exist
            sys.exit(1) 

        print(f"Deleting '{mpremote_target_path}' ({path_type})...")
        # fs rm works for files. fs rm -r for directories (and also files).
        # Using -r is safer if we don't strictly need to differentiate command.
        del_args = ["fs", "rm"]
        if path_type == "dir": # Only add -r if we know it's a directory
            del_args.append("-r")
        del_args.append(mpremote_target_path)
        
        del_result = run_mpremote_command(del_args, suppress_output=True)

        if del_result and del_result.returncode == 0:
            print(f"Deleted '{mpremote_target_path}'.")
        else:
            err_msg = del_result.stderr.strip() if del_result and del_result.stderr else "Deletion failed"
            if del_result and not err_msg and del_result.stdout: err_msg = del_result.stdout.strip()
            print(f"Error deleting '{mpremote_target_path}': {err_msg}", file=sys.stderr)
            sys.exit(1)



def cmd_flash(firmware_source, baud_rate_str="460800"):
    global DEVICE_PORT
    
    if not DEVICE_PORT:
        print("Error: Device port not set. Cannot proceed with flashing.", file=sys.stderr)
        print("Use 'esp32 devices` to see available devices then `esp32 device <PORT_NAME>' to set the port.")
        sys.exit(1)

    if firmware_source == DEFAULT_FIRMWARE_URL or "micropython.org/resources/firmware/" in firmware_source: # Check against actual source used
        print(f"Using official default firmware URL: {firmware_source}")
        print("If you have a specific firmware .bin URL or local file, please provide it as an argument to the flash command.")

    print("\nIMPORTANT: Ensure your ESP32-C3 is in bootloader mode.")
    print("To do this: Unplug USB, press and HOLD the BOOT button, plug in USB, wait 2-3 seconds, then RELEASE BOOT button.")
    
    try:
        # Check esptool presence before asking to proceed
        run_esptool_command(["--version"], suppress_output=True, timeout=5) 
    except SystemExit: # If run_esptool_command exits due to FileNotFoundError
        # Error message already printed by run_esptool_command
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
                    chunk_size = 8192 # 8KB
                    
                    # Simple progress tracking
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
                            # More responsive progress bar
                            current_progress_pct = (downloaded_size / total_size) * 100
                            # Update bar every 5% or so
                            if int(current_progress_pct / 5) > progress_ticks:
                                sys.stdout.write("#")
                                sys.stdout.flush()
                                progress_ticks = int(current_progress_pct / 5)
                        else: # No total size, just show dots
                            if downloaded_size // (chunk_size * 10) > progress_ticks : # roughly every 80KB
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
            actual_firmware_file_to_flash = str(local_firmware_path.resolve()) # Use resolved path
            print(f"Using local firmware file: {actual_firmware_file_to_flash}")

        print(f"\nStep 1: Erasing flash on {DEVICE_PORT}...")
        erase_args = ["--chip", "esp32c3", "--port", DEVICE_PORT, "erase_flash"]
        erase_result = run_esptool_command(erase_args) # Suppress output is False by default
        if not erase_result or erase_result.returncode != 0:
            err_msg = erase_result.stderr.strip() if erase_result and erase_result.stderr else "Erase command failed."
            # esptool already prints detailed errors, so our message can be simpler.
            print(f"Error erasing flash. esptool said: {err_msg}", file=sys.stderr)
            if "A fatal error occurred: Could not connect to an Espressif device" in err_msg or \
               "Failed to connect to ESP32-C3" in err_msg :
                 print("This commonly indicates the device is not in bootloader mode or a connection issue.", file=sys.stderr)
                 print("Please ensure you have followed the BOOT button procedure correctly.", file=sys.stderr)
            sys.exit(1)
        print("Flash erase completed successfully.")

        print(f"\nStep 2: Writing firmware '{Path(actual_firmware_file_to_flash).name}' to {DEVICE_PORT} at baud {baud_rate_str}...")
        write_args = [
            "--chip", "esp32c3",
            "--port", DEVICE_PORT,
            "--baud", baud_rate_str,
            "write_flash",
            #"-fm", "dio", "-fs", "detect", # Common options, but MicroPython bin usually self-contains
            "-z", "0x0", # Flash from offset 0
            actual_firmware_file_to_flash
        ]
        write_result = run_esptool_command(write_args)
        if not write_result or write_result.returncode != 0:
            err_msg = write_result.stderr.strip() if write_result and write_result.stderr else "Write flash command failed."
            print(f"Error writing firmware. esptool said: {err_msg}", file=sys.stderr)
            sys.exit(1)
        print("Firmware writing completed successfully.")

        print("\nStep 3: Verifying MicroPython installation...")
        print("Waiting a few seconds for device to reboot...")
        import time
        time.sleep(5) 

        verified, msg = test_micropython_presence(DEVICE_PORT)
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
                # print(f"Temporary firmware file {downloaded_temp_file} deleted.")
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

    help_parser = subparsers.add_parser("help", help="Show this help message and exit.")
    devices_parser = subparsers.add_parser("devices",help="List available COM ports and show the selected COM port.")
    dev_parser = subparsers.add_parser("device", help="Set or test the selected COM port for operations.")
    dev_parser.add_argument("port_name", nargs='?', metavar="PORT", help="The COM port to set. If omitted, tests current.")
    dev_parser.add_argument("--force", "-f", action="store_true", help="Force set port even if test fails.")
    
    flash_parser = subparsers.add_parser("flash", help="Download (if URL) and flash MicroPython firmware to the ESP32.")
    flash_parser.add_argument(
        "firmware_source",
        default=DEFAULT_FIRMWARE_URL, 
        nargs='?', 
        help=f"URL to download the MicroPython .bin firmware, or a path to a local .bin file. (Default: official ESP32_GENERIC_C3)"
    )
    flash_parser.add_argument(
        "--baud",
        default="460800",
        help="Baud rate for flashing firmware (Default: 460800)."
    )

    up_parser = subparsers.add_parser("upload", help="Upload file/directory to ESP32.")
    up_parser.add_argument("local_source", help="Local file/dir. Trailing '/' on dir uploads its contents.")
    up_parser.add_argument("remote_destination", nargs='?', default=None, help="Remote parent directory path. If omitted, uploads to root.")
    
    # New download parser
    dl_parser = subparsers.add_parser("download", help="Download file/directory from ESP32. Replaces download_file and download_all.")
    dl_parser.add_argument("remote_source_path", metavar="REMOTE_PATH", help="Remote file/dir path. Trailing '/' on dir downloads its contents (e.g., '/logs/', '//' for root contents).")
    dl_parser.add_argument("local_target_path", nargs='?', default=None, metavar="LOCAL_PATH", help="Local directory to download into, or local filename for a single remote file. If omitted, uses current directory.")

    run_parser = subparsers.add_parser("run", help="Run Python script on ESP32.")
    run_parser.add_argument("script_name", nargs='?', default="main.py", metavar="SCRIPT", help="Script to run (default: main.py). Path is relative to device root.")
    
    # ls_parser is deprecated by list_parser, but kept for argcomplete if used.
    # ls_parser = subparsers.add_parser("ls", help=argparse.SUPPRESS) 
    # ls_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    
    list_parser = subparsers.add_parser("list", help="List files/dirs on ESP32 (recursively from given path).") # Alias for ls
    list_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory path (e.g., '/lib', or omit for root).")
    
    tree_parser = subparsers.add_parser("tree", help="Display remote file tree.")
    tree_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory path (default: root).")
    
    del_parser = subparsers.add_parser("delete", help="Delete file/directory on ESP32.")
    del_parser.add_argument("remote_path_to_delete", metavar="REMOTE_PATH", nargs='?', default=None, help="Remote path (e.g. '/main.py', '/lib'). Omitting or '/' deletes root contents (requires confirmation).")
    
    up_all_parser = subparsers.add_parser("upload_all_cwd", help="[Basic] Upload eligible CWD items to ESP32 root.")

    args = parser.parse_args()

    if args.port: DEVICE_PORT = args.port
    elif "port" in cfg: DEVICE_PORT = cfg["port"]
    
    commands_needing_port = [
        "device", "upload", "run", "list", "tree", 
        "download", "delete", "upload_all_cwd", "flash"
    ]
    # "ls" can be added back if its parser is not suppressed.

    is_device_command_setting_port = args.cmd == "device" and args.port_name
    # is_device_command_testing_port = args.cmd == "device" and not args.port_name and DEVICE_PORT # Not used directly here

    if args.cmd in commands_needing_port and not DEVICE_PORT and not is_device_command_setting_port:
        # Special handling for 'device' command without port_name (it's a query/test, might not need port yet)
        if args.cmd == "device" and not args.port_name:
            pass # Will be handled by its own logic to show status or prompt.
        elif args.cmd == "flash" and not DEVICE_PORT: # flash command has its own detailed port check message
             cmd_flash(args.firmware_source, args.baud) # Will exit if port not set
             sys.exit(0) # Should not reach here if cmd_flash exits
        else:
            print("Error: No COM port selected or configured.", file=sys.stderr)
            print("Use 'esp32 devices' to list available ports, then 'esp32 device <PORT_NAME>' to set one.", file=sys.stderr)
            if args.cmd not in ["help", "devices"]: # Avoid double listing if 'devices' was implicitly called
                 # cmd_devices() # Optionally show devices again.
                 pass
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
            cmd_devices() # Show available ports
            print(f"\nUse 'esp32 device <PORT_NAME>' to set one.")
    elif args.cmd == "flash":
        cmd_flash(args.firmware_source, args.baud)
    elif args.cmd == "upload": cmd_upload(args.local_source, args.remote_destination)
    elif args.cmd == "run": run_script(args.script_name)
    # elif args.cmd == "ls": list_remote(args.remote_directory) # If ls parser is active
    elif args.cmd == "list": list_remote(args.remote_directory)
    elif args.cmd == "tree": tree_remote(args.remote_directory)
    elif args.cmd == "download": cmd_download(args.remote_source_path, args.local_target_path)
    elif args.cmd == "delete": delete_remote(args.remote_path_to_delete)
    elif args.cmd == "upload_all_cwd": upload_all()

if __name__ == "__main__":
    main()