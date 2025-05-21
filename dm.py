# File: esp32_deploy_manager/dm.py

#!/usr/bin/env python3
"""
esp32_deploy_manager (dm.py)

Manage deployment of MicroPython files to an ESP32-C3 via mpremote.

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

CONFIG_FILE = Path(__file__).parent / ".esp32_deploy_config.json"
DEVICE_PORT = None # Will be set by main after parsing args or loading config

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
        # Return a dummy CompletedProcess with a specific error code or message
        return subprocess.CompletedProcess(mpremote_args_list, -99, stdout="", stderr="Device port not set")


    base_cmd = ["mpremote", "connect", port_to_use]
    full_cmd = base_cmd + mpremote_args_list

    try:
        if suppress_output:
            process = subprocess.run(full_cmd, capture_output=True, text=True, check=False, timeout=timeout, cwd=working_dir)
        else:
            # Stream output, mpremote handles its own printing.
            process = subprocess.run(full_cmd, text=True, check=False, timeout=timeout, cwd=working_dir)
        return process
    except FileNotFoundError:
        print("Error: mpremote command not found. Is it installed and in PATH?", file=sys.stderr)
        sys.exit(1) # Critical error
    except subprocess.TimeoutExpired:
        # print(f"Timeout executing mpremote command: {' '.join(full_cmd)}", file=sys.stderr)
        return subprocess.CompletedProcess(full_cmd, -1, stdout="", stderr="TimeoutExpired executing mpremote")
    except Exception as e:
        # print(f"An unexpected error occurred running mpremote command {' '.join(full_cmd)}: {e}", file=sys.stderr)
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
            else: return None, None # Unexpected output
        except Exception: return None, None # Eval or format error
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
    print("Use 'esp32 device <PORT_NAME>' to change or set it.")


def test_device(port, timeout=5):
    # Uses run_mpremote_command now for consistency, though original was fine
    result = run_mpremote_command(["fs", "ls", ":"], connect_port=port, suppress_output=True, timeout=timeout)
    if result and result.returncode == 0:
        return True, f"Device on {port} responded."
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "No response or mpremote error."
        if result and result.returncode == -99: err_msg = result.stderr # Port not set from run_mpremote
        return False, f"No response or error on {port}. Details: {err_msg}"


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


def run_cmd_output(mpremote_args_list): # Kept for specific uses like list_remote_capture
    result = run_mpremote_command(mpremote_args_list, suppress_output=True)
    if result and result.returncode == 0:
        return result.stdout.splitlines()
    # else:
    #     if result and (result.stderr or result.stdout):
    #         # print(f"Command failed: mpremote ... {mpremote_args_list[-1]}\nError: {result.stderr or result.stdout}", file=sys.stderr)
    #         pass # Error message would be handled by caller or earlier print
    return []


def ensure_remote_dir(remote_dir_to_create):
    """
    Recursively ensures that the directory remote_dir_to_create exists on the device.
    remote_dir_to_create: e.g., 'networking' or 'foo/bar' (no leading/trailing slashes expected here for input).
    """
    global DEVICE_PORT
    if not DEVICE_PORT: # Should be checked by caller
        print("Error: Device port not set. Cannot ensure remote directory.", file=sys.stderr)
        return False

    # Normalize path: remove leading/trailing slashes and split
    # Path("foo/bar").parts -> ("foo", "bar")
    # Path("foo").parts -> ("foo",)
    # Path("").parts -> () - this case means root, handled.
    normalized_path = remote_dir_to_create.strip("/")
    if not normalized_path: # Root or empty string implies root, which always exists
        return True

    parts = Path(normalized_path).parts
    current_remote_path_str = ""

    for part in parts:
        if not current_remote_path_str:
            current_remote_path_str = part
        else:
            current_remote_path_str = f"{current_remote_path_str}/{part}"
        
        # Check if this part of the path already exists and is a directory
        # path_type, _ = get_remote_path_stat(current_remote_path_str)
        # if path_type == 'dir':
        #     continue # Already exists and is a dir, move to next part
        # if path_type == 'file':
        #     print(f"Error: Remote path ':{current_remote_path_str}' exists and is a file, cannot create directory component.", file=sys.stderr)
        #     return False
        
        # Attempt to create the current segment
        # mpremote fs mkdir :path_segment
        # print(f"DEBUG: Ensuring segment ':{current_remote_path_str}'") # For debugging
        result = run_mpremote_command(["fs", "mkdir", f":{current_remote_path_str}"], suppress_output=True)

        if result and result.returncode == 0:
            # Successfully created
            continue
        elif result and result.stderr and ("EEXIST" in result.stderr or "File exists" in result.stderr):
            # Directory already exists, which is fine.
            # We could add a stat here to confirm it's actually a directory if mpremote's EEXIST
            # isn't perfectly reliable for this, but usually it is.
            # For now, assume EEXIST means it's a usable directory.
            continue
        else:
            # An actual error occurred during mkdir
            err_msg = result.stderr.strip() if result and result.stderr else f"Unknown error creating ':{current_remote_path_str}'"
            if result and not result.stderr and result.stdout: # Sometimes mpremote puts error in stdout
                 err_msg = result.stdout.strip()
            print(f"Error creating remote directory component ':{current_remote_path_str}': {err_msg}", file=sys.stderr)
            return False
            
    return True # All parts created successfully

def cmd_upload(local_src_arg, remote_dest_arg=None):
    global DEVICE_PORT # Port check is done in main

    # 1. Determine local path characteristics
    local_src_original_for_mpremote = local_src_arg # Use original relative path for mpremote source
    abs_local_path_for_check = Path(os.path.abspath(local_src_arg))
    
    is_local_dir = abs_local_path_for_check.is_dir()
    local_is_contents_only = local_src_original_for_mpremote.endswith(("/", os.sep))

    if not abs_local_path_for_check.exists():
        print(f"Error: Local path '{local_src_original_for_mpremote}' (abs: '{abs_local_path_for_check}') does not exist.", file=sys.stderr)
        sys.exit(1)
    if local_is_contents_only and not is_local_dir:
        print(f"Error: Local source '{local_src_original_for_mpremote}' ends with '/' but is not a directory.", file=sys.stderr)
        sys.exit(1)

    # 2. Determine remote target directory to ensure and mpremote's copy target spec string
    remote_target_dir_to_ensure_str = ""  # Path on remote to ensure exists (parent of target item/contents)
    mpremote_cp_target_spec_str = ""      # The full :path string for mpremote's copy destination

    if local_is_contents_only:
        # --- Uploading CONTENTS of a local directory ---
        # Source for mpremote will be individual items *inside* local_src_original_for_mpremote
        # Target for mpremote is always a directory path ending with /
        if remote_dest_arg is None:
            remote_target_dir_to_ensure_str = ""  # Root
            mpremote_cp_target_spec_str = ":/"
        else:
            # User specified a remote directory for the contents
            remote_dest_norm = remote_dest_arg.replace(os.sep, "/").strip("/") # Normalize, e.g. "foo" or "foo/bar"
            remote_target_dir_to_ensure_str = remote_dest_norm
            mpremote_cp_target_spec_str = f":{remote_dest_norm}/" if remote_dest_norm else ":/"
    else:
        # --- Uploading a SINGLE ITEM (a file or a directory as a whole) ---
        # Source for mpremote will be local_src_original_for_mpremote itself
        local_item_basename = abs_local_path_for_check.name # e.g. "main.py", "flash"
        
        if remote_dest_arg is None: # No explicit remote destination
            if is_local_dir:
                # e.g. `upload my_dir` or `upload path/to/my_dir`
                # Mirror local relative path structure on remote.
                target_remote_item_path = Path(local_src_original_for_mpremote.rstrip("/")).as_posix()
                mpremote_cp_target_spec_str = f":{target_remote_item_path}" # Target is the item itself
                
                parent_path_obj = Path(target_remote_item_path).parent
                remote_target_dir_to_ensure_str = parent_path_obj.as_posix() if parent_path_obj.as_posix() != "." else ""
            else: # Is a file
                # e.g. `upload my_file.py` or `upload path/to/my_file.py`
                # Places file in remote root, named as original file's basename.
                mpremote_cp_target_spec_str = f":{local_item_basename}" # Target is the file in root
                remote_target_dir_to_ensure_str = "" # Root
        
        else: # Explicit remote_dest_arg IS provided
            remote_dest_norm = remote_dest_arg.replace(os.sep, "/").strip() 
            
            if remote_dest_norm.endswith("/"): # Target is explicitly a directory, e.g. "target_dir/" or "/"
                # This is the directory *into which* the local item should be placed.
                remote_parent_dir_for_item_placement_str = remote_dest_norm.rstrip("/") # "target_dir" or "" for root
                remote_target_dir_to_ensure_str = remote_parent_dir_for_item_placement_str
                
                # The mpremote target path will be parent_dir/local_item_basename
                if remote_parent_dir_for_item_placement_str: # Not root
                    mpremote_cp_target_spec_str = f":{remote_parent_dir_for_item_placement_str}/{local_item_basename}"
                else: # Root
                    mpremote_cp_target_spec_str = f":{local_item_basename}"
            
            else: # Target does NOT end with "/", e.g. "target_file_name.py" or "base_dir_for_local_structure"
                if is_local_dir:
                    # `upload local_dir remote_base_dir` or `upload path/to/local_dir remote_base_dir`
                    # The local directory (and its preceding path if any) is placed under remote_base_dir.
                    # Example: `esp32 upload flash/networking project_root`
                    # Results in: remote `:/project_root/flash/networking/`
                    
                    local_src_relative_path_obj = Path(local_src_original_for_mpremote.rstrip("/")) 
                    target_base_dir_on_remote_obj = Path(remote_dest_norm.strip("/")) # Should not have slash here

                    final_remote_item_path_obj = target_base_dir_on_remote_obj / local_src_relative_path_obj
                    mpremote_cp_target_spec_str = f":{final_remote_item_path_obj.as_posix()}"
                    
                    parent_dir_for_item_obj = final_remote_item_path_obj.parent
                    remote_target_dir_to_ensure_str = parent_dir_for_item_obj.as_posix() if parent_dir_for_item_obj.as_posix() != "." else ""
                else: # Is a file
                    # `upload local_file.py remote_file_name.py`
                    # Example: `esp32 upload flash/main.py main.py` -> target `:main.py`
                    # Here, remote_dest_norm *is* the exact target file path on remote.
                    mpremote_cp_target_spec_str = f":{remote_dest_norm}"
                    
                    parent_dir_obj = Path(remote_dest_norm).parent
                    remote_target_dir_to_ensure_str = parent_dir_obj.as_posix() if parent_dir_obj.as_posix() != "." else ""

    # 3. Ensure the target directory structure exists on remote (if any)
    # remote_target_dir_to_ensure_str can be "" (root), "foo", or "foo/bar"
    if remote_target_dir_to_ensure_str: # Only run if not root
        print(f"Ensuring remote directory ':{remote_target_dir_to_ensure_str}' exists...")
        if not ensure_remote_dir(remote_target_dir_to_ensure_str): # ensure_remote_dir handles stripping its input
            sys.exit(1)

    # 4. Perform the upload
    mpremote_actual_source_for_cp = local_src_original_for_mpremote # Use the original relative path

    if local_is_contents_only:
        print(f"Uploading contents of '{local_src_original_for_mpremote}' to '{mpremote_cp_target_spec_str}' on device...")
        
        items_in_local_dir = list(abs_local_path_for_check.iterdir())
        if not items_in_local_dir:
            print(f"Warning: Local directory '{local_src_original_for_mpremote}' is empty. Nothing to upload.")
            return # Success, nothing to do.

        success_count = 0; fail_count = 0
        for item in items_in_local_dir:
            mpremote_item_src_str = item.resolve().as_posix() # Absolute path for individual items
            cp_args = ["fs", "cp"]
            if item.is_dir(): cp_args.append("-r")
            cp_args.extend([mpremote_item_src_str, mpremote_cp_target_spec_str]) # Target is the remote dir
            
            print(f"  Uploading '{item.name}'...")
            result = run_mpremote_command(cp_args, suppress_output=True)
            if result and result.returncode == 0: success_count += 1
            else:
                fail_count += 1
                err_msg = result.stderr.strip() if result and result.stderr else "Upload failed"
                if result and not err_msg and result.stdout: err_msg = result.stdout.strip()
                print(f"    Error uploading '{item.name}': {err_msg}", file=sys.stderr)
        
        print(f"Contents upload complete. {success_count} items succeeded, {fail_count} items failed.")
        if fail_count > 0: sys.exit(1)

    else: # Uploading a single item (file or directory as a whole)
        print(f"Uploading '{local_src_original_for_mpremote}' to '{mpremote_cp_target_spec_str}' on device...")
        cp_args = ["fs", "cp"]
        if is_local_dir: cp_args.append("-r")
        
        cp_args.extend([mpremote_actual_source_for_cp, mpremote_cp_target_spec_str])
        
        result = run_mpremote_command(cp_args, suppress_output=True)
        if result and result.returncode == 0:
            print("Upload complete.")
        else:
            err_msg = result.stderr.strip() if result and result.stderr else "Upload failed"
            if result and not err_msg and result.stdout: err_msg = result.stdout.strip() # Check stdout for mpremote messages
            print(f"Error uploading '{local_src_original_for_mpremote}': {err_msg}", file=sys.stderr)
            sys.exit(1)

def upload_all(): # Uses run_mpremote_command now
    global DEVICE_PORT
    # Port check in main

    me = os.path.basename(__file__)
    items_to_upload = []
    for item_name in os.listdir("."): 
        if item_name == me or item_name.startswith(".") or \
           item_name.endswith(".egg-info") or item_name == "__pycache__" or \
           item_name == ".esp32_deploy_config.json": # Exclude config
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
        abs_item_path_str = str(item_path_obj.resolve()) # Absolute path for source
        
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
    # Port check in main

    script_on_device_norm = script.lstrip('/') 
    
    print(f"Checking for '{script_on_device_norm}' on device...")
    path_type, _ = get_remote_path_stat(script_on_device_norm)

    if path_type is None:
        print(f"Error: Script ':{script_on_device_norm}' not found on device.", file=sys.stderr)
        sys.exit(1)
    if path_type == 'dir':
        print(f"Error: Path ':{script_on_device_norm}' on device is a directory, not a runnable script.", file=sys.stderr)
        sys.exit(1)
    if path_type != 'file': # Catch 'unknown' or other types
        print(f"Error: Path ':{script_on_device_norm}' on device is not a file.", file=sys.stderr)
        sys.exit(1)

    # MicroPython exec(open(...)) generally expects absolute path
    abs_script_path_on_device = f"/{script_on_device_norm}"
    python_code = f"exec(open('{abs_script_path_on_device}').read())"
    
    print(f"Running '{script_on_device_norm}' on {DEVICE_PORT}...")
    # For run, we want to see the script's output, so suppress_output=False
    result = run_mpremote_command(["exec", python_code], suppress_output=False) 
    
    if result and result.returncode != 0:
        # If suppress_output=False, mpremote will have printed Python's Traceback to stderr already.
        # We just need to signal that the command wrapper (esp32) also failed.
        # print(f"Script '{script_on_device_norm}' execution failed.", file=sys.stderr) # mpremote output is usually sufficient
        sys.exit(1)


def download_file(remote_path, local_path=None):
    global DEVICE_PORT
    # Port check in main
    
    rp_norm = remote_path.lstrip("/")
    lp_str = local_path or rp_norm # Default local path is same name in CWD
    
    abs_local_target_path = Path(os.path.abspath(lp_str))
    
    # Check remote file existence
    print(f"Checking remote file ':{rp_norm}'...")
    path_type, _ = get_remote_path_stat(rp_norm)
    if path_type is None:
        print(f"Error: Remote file ':{rp_norm}' not found.", file=sys.stderr)
        sys.exit(1)
    if path_type != 'file':
        print(f"Error: Remote path ':{rp_norm}' is not a file.", file=sys.stderr)
        sys.exit(1)

    if abs_local_target_path.parent:
        abs_local_target_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading ':{rp_norm}' to '{abs_local_target_path}'...")
    result = run_mpremote_command(["fs", "cp", f":{rp_norm}", str(abs_local_target_path)], suppress_output=True)

    if result and result.returncode == 0:
        print("Download complete.")
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "Download failed"
        print(f"Error downloading ':{rp_norm}': {err_msg}", file=sys.stderr)
        sys.exit(1)


def download_all(remote_dir, local_dir_str=None):
    global DEVICE_PORT
    # Port check in main

    rd_norm = remote_dir.strip("/")
    if not rd_norm: # Cannot download root itself like this with mpremote
        print("Error: Remote directory for download_all cannot be root. Specify a directory name.", file=sys.stderr)
        sys.exit(1)

    # Check remote dir existence
    print(f"Checking remote directory ':{rd_norm}'...")
    path_type, _ = get_remote_path_stat(rd_norm)
    if path_type is None:
        print(f"Error: Remote directory ':{rd_norm}' not found.", file=sys.stderr)
        sys.exit(1)
    if path_type != 'dir':
        print(f"Error: Remote path ':{rd_norm}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Default local dir is a new dir with same name as remote_dir in CWD
    ld_final_str = local_dir_str or rd_norm 
    abs_local_dest_path = Path(os.path.abspath(ld_final_str))
    
    # mpremote cp -r :remote_src_dir local_dest_dir
    # If local_dest_dir does not exist, mpremote creates it and copies contents into it.
    # If local_dest_dir exists, mpremote copies remote_src_dir *inside* local_dest_dir.
    # To match user expectation of "download contents of remote_dir into specified/new local_dir":
    abs_local_dest_path.mkdir(parents=True, exist_ok=True) # Ensure local target base directory exists
    
    print(f"Downloading contents of ':{rd_norm}/' to '{abs_local_dest_path}/'...")
    # The mpremote command `cp -r :src_dir dest_dir` copies the *contents* of src_dir into dest_dir
    # if dest_dir exists. If dest_dir does not exist, it creates dest_dir and puts contents in it.
    # This is slightly different from `rsync` or POSIX `cp -r src_dir/ dest_dir`.
    # To ensure contents of :rd_norm go into abs_local_dest_path:
    
    # The command `mpremote fs cp -r :remote_folder local_folder` should result in `local_folder/remote_folder_contents...`
    # if `local_folder` exists, or `local_folder/contents...` if `local_folder` is created by mpremote.
    # Let's assume `mpremote` behavior: `cp -r :SRC new_LOCAL_DIR` will create `new_LOCAL_DIR` and put SRC contents in it.
    # `cp -r :SRC existing_LOCAL_DIR` will create `existing_LOCAL_DIR/SRC` and put contents in there.
    # To achieve "contents of :rd_norm into abs_local_dest_path", abs_local_dest_path must exist.
    
    result = run_mpremote_command([
        "fs", "cp", "-r",
        f":{rd_norm}", str(abs_local_dest_path) 
    ], suppress_output=True)

    if result and result.returncode == 0:
        print("Recursive download complete.")
    else:
        err_msg = result.stderr.strip() if result and result.stderr else "Recursive download failed"
        print(f"Error downloading ':{rd_norm}': {err_msg}", file=sys.stderr)
        sys.exit(1)


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
        # print(f"Error listing '{{p}}': {{e}}", file=sys.stderr) # Python print
        return
    for name, typ, *_ in items:
        base_path = p.rstrip('/')
        item_full_path = base_path + '/' + name if base_path != '' else '/' + name
        print(item_full_path) 
        if typ == 0x4000: # STAT_DIR
            _walk(item_full_path)
_walk('{path_for_walk}')
"""
    # run_cmd_output now uses run_mpremote_command with suppress_output=True
    lines = run_cmd_output(["exec", code])
    # Filter out potential error messages printed by the MicroPython script if uos.ilistdir fails mid-walk
    # However, the current script's _walk silences internal OSError.
    return [line for line in lines if line.startswith('/')]


def list_remote(remote_dir=None):
    global DEVICE_PORT
    # Port check in main

    normalized_remote_dir = (remote_dir or "").strip("/") 
    display_dir_name = f":{normalized_remote_dir or '/'}"
    
    if normalized_remote_dir: # Not root, check if it exists and is a directory
        # print(f"Checking remote path '{display_dir_name}'...") # Can be verbose
        path_type, _ = get_remote_path_stat(normalized_remote_dir)
        if path_type is None:
            print(f"Error: Remote path '{display_dir_name}' not found.", file=sys.stderr)
            return
        if path_type == 'file':
            print(f"Error: '{display_dir_name}' is a file, not a directory.", file=sys.stderr)
            return
    
    print(f"Listing contents of '{display_dir_name}'...")
    all_paths_abs = list_remote_capture(normalized_remote_dir) # Returns absolute paths

    if not all_paths_abs:
        # This means the exec script returned nothing, or the dir is genuinely empty
        # If normalized_remote_dir was checked and is a dir, then it's empty.
        # If it's root, it's empty.
        print(f"Directory '{display_dir_name}' is empty.")
        return
    
    # The user log `esp32 list networking` showed `networking/__init__.py`
    # This implies paths relative to the listed directory, prefixed with the dir name.
    # list_remote_capture returns absolute paths like `/networking/__init__.py`.
    
    displayed_any = False
    if not normalized_remote_dir: # Listing root
        for path_str in sorted(all_paths_abs):
            print(path_str) 
            displayed_any = True
    else: # Listing a subdirectory
        # Expected prefix for items inside the directory, e.g., "/networking/"
        # Path strings from capture are absolute, e.g., "/networking/file.py"
        # We want to display "networking/file.py"
        # The `list_remote_capture` script should list items *within* `normalized_remote_dir`
        # and they will all start with `/{normalized_remote_dir}`.

        for path_str in sorted(all_paths_abs):
            # path_str is like /dir/file.py. We want to show dir/file.py
            # The path_str from list_remote_capture should be absolute.
            # We want to print it relative to root, but only if it's within the requested dir.
            # The current list_remote_capture is already getting items for that specific dir path.
            # So, just print them if they match the prefix.
            
            # If normalized_remote_dir = "foo", path_str = "/foo/bar.py"
            # We want to print "foo/bar.py"
            if path_str.startswith(f"/{normalized_remote_dir}/") or path_str == f"/{normalized_remote_dir}":
                 # Strip leading slash for desired output format "dir/file.py"
                print(path_str.lstrip('/'))
                displayed_any = True
    
    if not displayed_any : # Should be caught by the 'if not all_paths_abs:' earlier
         print(f"Directory '{display_dir_name}' is empty.") # Fallback


def tree_remote(remote_dir=None):
    global DEVICE_PORT
    # Port check in main

    base_path_str_norm = (remote_dir or "").strip("/")
    display_root_name = f":{base_path_str_norm or '/'}"

    if base_path_str_norm: 
        # print(f"Checking remote path '{display_root_name}'...") # Can be verbose
        path_type, _ = get_remote_path_stat(base_path_str_norm)
        if path_type is None:
            print(f"Error: Remote directory '{display_root_name}' not found.", file=sys.stderr)
            return
        if path_type == 'file':
            print(f"Error: '{display_root_name}' is a file. Cannot display as tree.", file=sys.stderr)
            return
    
    print(f"Tree for '{display_root_name}' on device:")
    
    # list_remote_capture returns absolute paths from device root: /lib/foo.py
    lines_abs = list_remote_capture(base_path_str_norm) 
    
    if not lines_abs:
        print(f"Directory '{display_root_name}' is empty.")
        return

    paths_for_tree_build = [] # Paths relative to the base_path_str_norm for tree building
    if not base_path_str_norm: # Root listing
        # lines_abs are like "/file.py", "/dir/sfile.py"
        # Path objects should be "file.py", "dir/sfile.2py"
        paths_for_tree_build = [Path(p.lstrip('/')) for p in lines_abs if p != "/"]
    else:
        # Listing a subdirectory, e.g. base_path_str_norm = "lib"
        # lines_abs are like "/lib/foo.py", "/lib/subdir/bar.py"
        # We want Path("foo.py"), Path("subdir/bar.py")
        prefix_to_remove = f"/{base_path_str_norm}/"
        for line_abs in lines_abs:
            if line_abs.startswith(prefix_to_remove):
                relative_part = line_abs[len(prefix_to_remove):]
                if relative_part: # Avoid empty paths if line_abs was just prefix_to_remove itself (e.g. /lib/)
                    paths_for_tree_build.append(Path(relative_part))
            # elif line_abs == f"/{base_path_str_norm}": # Should not be needed from walk
            #     paths_for_tree_build.append(Path(Path(line_abs).name))


    if not paths_for_tree_build and (not base_path_str_norm or path_type == 'dir'):
        # This could happen if the directory exists but list_remote_capture (or filtering) yielded nothing.
        # The primary `if not lines_abs:` should catch most empty cases.
        print(f"Directory '{display_root_name}' is empty.") # Or contains no listable items for tree
        return

    structure = {}
    for p_obj in sorted(list(set(paths_for_tree_build))): # Unique paths
        current_level = structure
        parts = [part for part in p_obj.parts if part and part != '/']
        if not parts: continue # Skip if path was effectively empty or just "/"
        for part_idx, part in enumerate(parts):
            if part_idx == len(parts) - 1: # Last part
                current_level = current_level.setdefault(part, {})
            else:
                current_level = current_level.setdefault(part, {})
            
    def print_tree_nodes(node, prefix=""):
        children = sorted(node.keys())
        for i, child_name in enumerate(children):
            connector = "└── " if i == len(children) - 1 else "├── "
            print(f"{prefix}{connector}{child_name}")
            if node[child_name]: # If it has children (is a dict representing a dir)
                new_prefix = prefix + ("    " if i == len(children) - 1 else "│   ")
                print_tree_nodes(node[child_name], new_prefix)

    if not base_path_str_norm:
        print(".") 
        print_tree_nodes(structure, "")
    else:
        print(f". ({base_path_str_norm})")
        print_tree_nodes(structure, "")


def delete_remote(remote_path_arg):
    global DEVICE_PORT
    # Port check in main

    is_root_delete = remote_path_arg is None or remote_path_arg.strip() in ["", "/"]

    if is_root_delete:
        print("WARNING: You are about to delete all files and directories from the root of the device.")
        confirm = input("Are you sure? Type 'yes' to proceed: ")
        if confirm.lower() != 'yes':
            print("Operation cancelled.")
            return

        print("Fetching root directory contents for deletion...")
        ls_result = run_mpremote_command(["fs", "ls", ":"], suppress_output=True)
        
        if not ls_result or ls_result.returncode != 0:
            err = ls_result.stderr.strip() if ls_result and ls_result.stderr else "Failed to connect or list."
            print(f"Error listing root directory for deletion: {err}", file=sys.stderr)
            sys.exit(1)

        raw_item_entries = ls_result.stdout.splitlines()
        filtered_raw_entries = [line.strip() for line in raw_item_entries if line.strip() and not line.lower().startswith("ls ")]
        
        if not filtered_raw_entries:
            print("Root directory is already empty.")
            return

        actual_items_to_delete = []
        for raw_entry in filtered_raw_entries:
            parts = raw_entry.split(maxsplit=1)
            item_name = parts[1] if len(parts) == 2 and parts[0].isdigit() else raw_entry
            if item_name: actual_items_to_delete.append(item_name)

        if not actual_items_to_delete:
            print("No valid items to delete after parsing root listing.")
            return

        print(f"Items to delete from root: {actual_items_to_delete}")
        all_successful = True
        for item_name_to_delete in actual_items_to_delete:
            item_target_on_device = item_name_to_delete.lstrip("/")
            item_target_for_mpremote = ":" + item_target_on_device
            
            print(f"Deleting '{item_target_for_mpremote}'...")
            # Assuming mpremote rm -r works for both files and dirs
            del_result = run_mpremote_command(["fs", "rm", "-r", item_target_for_mpremote], suppress_output=True)
            
            if del_result and del_result.returncode == 0:
                print(f"  Deleted '{item_target_for_mpremote}'.")
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
        normalized_path = remote_path_arg.strip('/')
        mpremote_target_path = ":" + normalized_path

        # print(f"Checking path '{mpremote_target_path}' on device...") # Verbose
        path_type, _ = get_remote_path_stat(normalized_path)

        if path_type is None:
            print(f"File/Directory '{mpremote_target_path}' does not exist.") # Changed message slightly
            return # Don't exit, just inform as per request "don't need to print out anything else"

        print(f"Deleting '{mpremote_target_path}'...")
        del_result = run_mpremote_command(["fs", "rm", "-r", mpremote_target_path], suppress_output=True)

        if del_result and del_result.returncode == 0:
            print(f"Deleted '{mpremote_target_path}'.")
        else:
            err_msg = del_result.stderr.strip() if del_result and del_result.stderr else "Deletion failed"
            if del_result and not err_msg and del_result.stdout: err_msg = del_result.stdout.strip()
            print(f"Error deleting '{mpremote_target_path}': {err_msg}", file=sys.stderr)
            sys.exit(1)


def main():
    global DEVICE_PORT
    cfg = load_config()
    
    parser = argparse.ArgumentParser(
        prog="esp32",
        description="Manage deployment of MicroPython files to an ESP32 device via mpremote.",
        epilog="Use 'esp32 <command> --help' for more information on a specific command."
    )
    parser.add_argument("--port", "-p", help="Override default/configured COM port for this command instance.")
    subparsers = parser.add_subparsers(dest="cmd", required=True, title="Available commands", metavar="<command>")

    # Standard subparsers (help, devices, device, upload, run, ls, list, tree, download, download_all, delete, upload_all_cwd)
    help_parser = subparsers.add_parser("help", help="Show this help message and exit.")
    devices_parser = subparsers.add_parser("devices",help="List available COM ports and show the selected COM port.")
    dev_parser = subparsers.add_parser("device", help="Set or test the selected COM port for operations.")
    dev_parser.add_argument("port_name", nargs='?', metavar="PORT", help="The COM port to set. If omitted, tests current.")
    dev_parser.add_argument("--force", "-f", action="store_true", help="Force set port even if test fails.")
    up_parser = subparsers.add_parser("upload", help="Upload file/directory to ESP32.")
    up_parser.add_argument("local_source", help="Local file/dir. Trailing '/' on dir uploads contents.")
    up_parser.add_argument("remote_destination", nargs='?', default=None, help="Remote path/dir. Trailing '/' for dir target.")
    run_parser = subparsers.add_parser("run", help="Run Python script on ESP32.")
    run_parser.add_argument("script_name", nargs='?', default="main.py", metavar="SCRIPT", help="Script to run (default: main.py).")
    ls_parser = subparsers.add_parser("ls", help=argparse.SUPPRESS) # Hidden alias
    ls_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    list_parser = subparsers.add_parser("list", help="List files/dirs on ESP32.")
    list_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    tree_parser = subparsers.add_parser("tree", help="Display remote file tree.")
    tree_parser.add_argument("remote_directory", nargs='?', default=None, metavar="REMOTE_DIR", help="Remote directory (default: root).")
    dl_parser = subparsers.add_parser("download", help="Download file from ESP32.")
    dl_parser.add_argument("remote_file_path", metavar="REMOTE_PATH", help="Remote file to download.")
    dl_parser.add_argument("local_target_path", nargs='?', default=None, metavar="LOCAL_PATH", help="Local path to save (default: same name).")
    dl_all_parser = subparsers.add_parser("download_all", help="Recursively download directory from ESP32.")
    dl_all_parser.add_argument("remote_source_dir", metavar="REMOTE_DIR", help="Remote directory to download.")
    dl_all_parser.add_argument("local_target_dir", nargs='?', default=None, metavar="LOCAL_DIR", help="Local directory to save into.")
    del_parser = subparsers.add_parser("delete", help="Delete file/directory on ESP32.")
    del_parser.add_argument("remote_path_to_delete", metavar="REMOTE_PATH", nargs='?', default=None, help="Remote path. Omitting deletes root contents (confirm).")
    up_all_parser = subparsers.add_parser("upload_all_cwd", help="[Basic] Upload CWD items to ESP32 root.")

    args = parser.parse_args()

    if args.port: DEVICE_PORT = args.port
    elif "port" in cfg: DEVICE_PORT = cfg["port"]
    
    commands_needing_port = ["device", "upload", "run", "ls", "list", "tree", "download", "download_all", "delete", "upload_all_cwd"]
    is_device_command_setting_port = args.cmd == "device" and args.port_name
    is_device_command_testing_port = args.cmd == "device" and not args.port_name and DEVICE_PORT

    if args.cmd in commands_needing_port and not DEVICE_PORT and not is_device_command_setting_port:
        if args.cmd == "device" and not args.port_name : # esp32 device (no port given, no port configured)
            pass # Let cmd_device handle showing "No COM port currently selected"
        else:
            print("Error: No device port selected. Use 'esp32 device <PORT_NAME>' or --port.", file=sys.stderr)
            if args.cmd != "help" and args.cmd != "devices": cmd_devices()
            sys.exit(1)

    if args.cmd == "help":
        parser.print_help()
        if not DEVICE_PORT: print(f"\nWarning: No port selected. Use 'esp32 devices' and 'esp32 device <PORT_NAME>'.")
    elif args.cmd == "devices": cmd_devices()
    elif args.cmd == "device":
        if args.port_name: cmd_device(args.port_name, args.force)
        elif DEVICE_PORT: # esp32 device (no port_name, but one is configured/set) -> test it
            print(f"Current selected COM port is {DEVICE_PORT}. Testing...")
            ok, msg = test_device(DEVICE_PORT); print(msg)
            if not ok: print(f"Consider re-setting with 'esp32 device {DEVICE_PORT}'.")
        else: # esp32 device (no port_name, no port configured)
            print("No COM port currently selected or configured.")
            cmd_devices()
            print(f"\nUse 'esp32 device <PORT_NAME>' to set one.")
    elif args.cmd == "upload": cmd_upload(args.local_source, args.remote_destination)
    elif args.cmd == "run": run_script(args.script_name)
    elif args.cmd == "ls" or args.cmd == "list": list_remote(args.remote_directory)
    elif args.cmd == "tree": tree_remote(args.remote_directory)
    elif args.cmd == "download": download_file(args.remote_file_path, args.local_target_path)
    elif args.cmd == "download_all": download_all(args.remote_source_dir, args.local_target_dir)
    elif args.cmd == "delete": delete_remote(args.remote_path_to_delete)
    elif args.cmd == "upload_all_cwd": upload_all()

if __name__ == "__main__":
    main()