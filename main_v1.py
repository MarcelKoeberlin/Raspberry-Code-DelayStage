import threading
import time
import numpy as np
import os
import RPi.GPIO as GPIO
from threading import Timer
from datetime import datetime
import tkinter as tk
from tkinter import simpledialog
from pathlib import Path
from typing import Any, Callable
import shutil

from espstage import ESPStage

# PATHS #################################################
# This class holds the directory paths for data storage.
# It's designed to separate temporary local storage from the final network storage
# to ensure data integrity even if the network connection is lost.
class PATHS:
    # GROUP_ROOT is the final destination for the data on the network share.
    # All session data will be copied here upon successful completion of a run.
    # Example for a real setup: "/home/moritz/groupshares/attophys/Attoline"
    GROUP_ROOT = '/home/moritz/groupshares/attophys/Attoline'
    # LOCAL_ROOT is the temporary storage location on the Raspberry Pi's local disk.
    # Data is saved here first to prevent data loss if the network is unavailable.
    LOCAL_ROOT = '/home/moritz/Desktop/DelayStage Data'

# SETTINGS ##############################################
# This class holds the core experimental parameters that can be adjusted.
# Some of these values are set by the user through the GUI at startup.
class Settings:
    # The physical distance the delay stage moves in a single step, defined in femtoseconds.
    # This value is used to calculate the equivalent distance in millimeters.
    MOVE_STEP_FS = 20
    
    # The maximum number of steps the delay stage will perform in a single run.
    # This acts as a safety limit and determines the size of the data array.
    MAX_MOVE_STEPS = 300
    
    # The move step converted to millimeters. This is calculated at runtime and should not be set manually.
    MOVE_STEP_MM: float = 0.0
    
    # A cooldown period (in seconds) after a trigger to prevent the GPIO from firing multiple times for a single event.
    # This is a hardware-related setting and generally should not be changed.
    TRIGGER_COOLDOWN_S = 2.0
    
    # A cooldown period (in seconds) between movements of the delay stage.
    # This ensures the stage has time to settle and the system is ready for the next acquisition.
    MOVE_COOLDOWN_S = 15.0

# CONSTANTS #############################################
# This class holds fixed hardware-related constants.
class Constants:
    # The GPIO pin number on the Raspberry Pi that is connected to the external hardware trigger.
    TRIGGER_PIN = 17

# TRIGGER HANDLER #######################################
# This class is the heart of the script's logic. It manages the state of the experiment,
# handles hardware triggers, records data, and controls the movement of the delay stage.
class TriggerHandler:
    def __init__(self, esp: ESPStage, timestamp_memmap: Any):
        """
        Initializes the TriggerHandler.
        
        :param esp: An instance of the ESPStage controller class, used to send commands to the motor.
        :param timestamp_memmap: A memory-mapped NumPy array for high-performance, real-time data recording.
        """
        print(f"DEBUG: Initializing TriggerHandler with memmap shape: {timestamp_memmap.shape}, dtype: {timestamp_memmap.dtype}")
        self.esp = esp
        self.timestamp_memmap = timestamp_memmap
        self.trigger_count = 0  # Counter for the number of triggers received.
        self.move_count = 0     # Counter for the number of moves performed.
        self.last_trigger_time = None  # Timestamp of the last received trigger.
        self.last_move_time = None     # Timestamp of the last move command.
        self._external_callback: Callable[[], None] = lambda: None  # Placeholder for the main logic callback.
        print(f"DEBUG: TriggerHandler initialized. trigger_count={self.trigger_count}, move_count={self.move_count}")

    def record_trigger(self):
        """
        Records a single timestamp when a hardware trigger is detected.
        This function is designed to be as fast as possible to not miss any triggers.
        """
        print(f"DEBUG: record_trigger() called. Current trigger_count: {self.trigger_count}")
        print(f"DEBUG: Memmap array length: {len(self.timestamp_memmap)}")
        
        # Record the current time with high precision.
        now = time.time()
        timestamp_us = int(time.time_ns() // 1000)  # Convert nanoseconds to microseconds.
        print(f"DEBUG: Generated timestamp - now: {now:.6f}, timestamp_us: {timestamp_us}")

        # Check if there is still space in the pre-allocated array.
        if self.trigger_count < len(self.timestamp_memmap):
            print(f"DEBUG: Space available in memmap. Writing timestamp at index {self.trigger_count}")
            
            # Write the timestamp to the memory-mapped file.
            try:
                self.timestamp_memmap[self.trigger_count] = timestamp_us
                print(f"DEBUG: Successfully wrote timestamp {timestamp_us} to index {self.trigger_count}")
                
                # .flush() ensures the data is written from memory to the disk immediately.
                # This is crucial for data integrity in case of a power failure.
                self.timestamp_memmap.flush()
                print(f"DEBUG: Memmap flushed to disk")
                
                # Verify the write was successful
                read_back = self.timestamp_memmap[self.trigger_count]
                print(f"DEBUG: Read-back verification: {read_back} (expected: {timestamp_us})")
                if read_back != timestamp_us:
                    print(f"ERROR: Write verification failed! Expected {timestamp_us}, got {read_back}")
                
            except Exception as e:
                print(f"ERROR: Failed to write timestamp to memmap: {e}")
                print(f"ERROR: trigger_count={self.trigger_count}, memmap_len={len(self.timestamp_memmap)}")
                return
            
            # Print diagnostic information about the timing.
            delta_trigger = now - self.last_trigger_time if self.last_trigger_time else 0
            delta_move = now - self.last_move_time if self.last_move_time else 0
            print(f"Trigger {self.trigger_count + 1} received — Δt_trigger = {delta_trigger:.2f}s, Δt_move = {delta_move:.2f}s")
            
            self.last_trigger_time = now
            self.trigger_count += 1
            print(f"DEBUG: Incremented trigger_count to {self.trigger_count}")
        else:
            print(f"ERROR: Max timestamps reached. trigger_count={self.trigger_count}, max_len={len(self.timestamp_memmap)}. Ignoring trigger.")

    def perform_move(self):
        """
        Executes a relative move of the delay stage.
        """
        print(f"DEBUG: perform_move() called. Current move_count: {self.move_count}")
        
        # Temporarily disable the GPIO trigger to prevent accidental firing during the move.
        print(f"DEBUG: Removing GPIO event detect on pin {Constants.TRIGGER_PIN}")
        GPIO.remove_event_detect(Constants.TRIGGER_PIN)
        
        # Send the move command to the stage controller.
        try:
            print(f"DEBUG: Sending move command: {float(Settings.MOVE_STEP_MM)} mm")
            self.esp.move_relative(float(Settings.MOVE_STEP_MM))
            self.last_move_time = time.time()
            self.move_count += 1
            print(f"Move {self.move_count} executed successfully")
        except Exception as e:
            print(f"ERROR: Failed to execute move: {e}")
        
        # Start a timer to re-enable the trigger after the cooldown period.
        print(f"DEBUG: Starting timer for trigger rearm in {Settings.TRIGGER_COOLDOWN_S}s")
        Timer(Settings.TRIGGER_COOLDOWN_S, self.rearm_trigger).start()

    def rearm_trigger(self):
        """
        Re-enables the GPIO event detection after a move is complete.
        """
        print(f"DEBUG: rearm_trigger() called")
        
        # Add the event detector back to the GPIO pin.
        # GPIO.RISING means it triggers on a voltage change from low to high.
        # `bouncetime` helps to debounce the signal, preventing multiple triggers from a single event.
        try:
            print(f"DEBUG: Adding GPIO event detect on pin {Constants.TRIGGER_PIN}")
            GPIO.add_event_detect(Constants.TRIGGER_PIN, GPIO.RISING, callback=self._raw_callback, bouncetime=50)
            print("Trigger rearmed. Waiting for hardware triggers...\n")
        except Exception as e:
            print(f"ERROR: Failed to rearm trigger: {e}")

    def _raw_callback(self, channel: int):
        """
        A lightweight, raw callback function that is directly called by the GPIO library.
        Its only job is to call the main external callback.
        """
        print(f"DEBUG: _raw_callback triggered on channel {channel}")
        try:
            self._external_callback()
            print(f"DEBUG: External callback executed successfully")
        except Exception as e:
            print(f"ERROR: Exception in external callback: {e}")

    def set_external_callback(self, callback: Callable[[], None]):
        """
        Assigns the main experimental logic to be executed when a trigger occurs.
        """
        print(f"DEBUG: Setting external callback: {callback}")
        self._external_callback = callback
        
    def handle_trigger(self, should_move: bool):
        """
        Handles the logic for a trigger event, deciding whether to move the stage or not.
        
        :param should_move: A boolean indicating if the conditions for a move are met.
        """
        print(f"DEBUG: handle_trigger() called with should_move={should_move}")
        
        # Always disarm the trigger immediately to prevent multiple detections.
        print(f"DEBUG: Disarming trigger on pin {Constants.TRIGGER_PIN}")
        GPIO.remove_event_detect(Constants.TRIGGER_PIN)
        
        if should_move:
            # If a move is warranted, command the stage to move.
            try:
                print(f"DEBUG: Executing move: {float(Settings.MOVE_STEP_MM)} mm")
                self.esp.move_relative(float(Settings.MOVE_STEP_MM))
                self.last_move_time = time.time()
                self.move_count += 1
                print(f"Move {self.move_count} executed")
            except Exception as e:
                print(f"ERROR: Failed to execute move in handle_trigger: {e}")
        else:
            # If not enough time has passed since the last move, ignore this trigger for movement purposes.
            delta = time.time() - self.last_move_time if self.last_move_time is not None else 0.0
            print(f"Ignored move (only {delta:.1f}s since last move)")
        
        # Always re-arm the trigger after the standard cooldown period.
        print(f"DEBUG: Starting rearm timer for {Settings.TRIGGER_COOLDOWN_S}s")
        Timer(Settings.TRIGGER_COOLDOWN_S, self.rearm_trigger).start()
        

# MAIN APPLICATION CLASS #########################################
class ExperimentController:
    def __init__(self, root):
        self.root = root
        self.root.title("Experiment Control")
        self.experiment_thread = None
        self.stop_event = threading.Event()

        self.start_button = tk.Button(root, text="Settings", command=self.start_experiment)
        self.start_button.pack(pady=10)

        self.stop_button = tk.Button(root, text="Stop Experiment", command=self.stop_experiment, state=tk.DISABLED)
        self.stop_button.pack(pady=10)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def start_experiment(self):
        params = ask_for_all_parameters(self.root)
        if not params:
            return

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.stop_event.clear()

        self.experiment_thread = threading.Thread(target=run_experiment, args=(params, self.stop_event, self.on_experiment_finish))
        self.experiment_thread.start()

    def stop_experiment(self):
        print("Stop button pressed. Signalling experiment to stop...")
        self.stop_event.set()
        self.stop_button.config(state=tk.DISABLED)

    def on_experiment_finish(self):
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.experiment_thread = None
        print("Experiment finished and GUI updated.")

    def on_closing(self):
        if self.experiment_thread and self.experiment_thread.is_alive():
            print("Please stop the experiment before closing.")
        else:
            self.root.destroy()

def run_experiment(params, stop_event, on_finish_callback):
    print(f"DEBUG: run_experiment() started with params: {params}")
    esp = None
    handler = None
    local_tmp_mmap_path = None
    local_session_dir = None
    
    try:
        # --- Step 1: Apply parameters ---
        ppas, spss, spds = params["ppas"], params["spss"], params["spds"]
        Settings.MOVE_STEP_FS = params["move_step_fs"]
        Settings.MAX_MOVE_STEPS = params["max_move_steps"]
        print(f"DEBUG: Applied parameters - ppas:{ppas}, spss:{spss}, spds:{spds}, move_step_fs:{Settings.MOVE_STEP_FS}, max_move_steps:{Settings.MAX_MOVE_STEPS}")

        # --- Step 2: Calculate physical constants ---
        move_step_mm = delay_fs_to_mm(Settings.MOVE_STEP_FS)
        Settings.MOVE_STEP_MM = move_step_mm
        print(f"Using MOVE_STEP_FS = {Settings.MOVE_STEP_FS:.2f} fs → MOVE_STEP_MM = {move_step_mm:.4f} mm")

        # --- Step 3: Prepare storage paths ---
        print(f"DEBUG: Preparing session paths...")
        local_session_dir, local_final_npz_path, local_tmp_mmap_path = prepare_session_paths()
        print(f"Saving data locally to: {local_session_dir}")
        print(f"DEBUG: NPZ path: {local_final_npz_path}")
        print(f"DEBUG: Memmap path: {local_tmp_mmap_path}")
        
        # --- Step 4: Set up the memory-mapped file for live data ---
        print(f"DEBUG: Creating memmap with shape ({Settings.MAX_MOVE_STEPS * 2},) at {local_tmp_mmap_path}")
        try:
            mmap_array = np.memmap(
                local_tmp_mmap_path,
                dtype="int64",
                mode="w+",
                shape=(Settings.MAX_MOVE_STEPS * 2,)
            )
            print(f"DEBUG: Memmap created successfully. Shape: {mmap_array.shape}, dtype: {mmap_array.dtype}")
            print(f"DEBUG: Memmap file exists: {os.path.exists(local_tmp_mmap_path)}")
            print(f"DEBUG: Memmap file size: {os.path.getsize(local_tmp_mmap_path) if os.path.exists(local_tmp_mmap_path) else 'N/A'} bytes")
            
            # Test write to memmap
            print(f"DEBUG: Testing memmap write...")
            test_value = 12345
            mmap_array[0] = test_value
            mmap_array.flush()
            read_value = mmap_array[0]
            print(f"DEBUG: Memmap test - wrote {test_value}, read {read_value}, success: {test_value == read_value}")
            
        except Exception as e:
            print(f"ERROR: Failed to create memmap: {e}")
            raise

        # --- Step 5: Initialize hardware controllers ---
        print(f"DEBUG: Initializing ESP stage on /dev/ttyUSB0...")
        try:
            esp = ESPStage("/dev/ttyUSB0", baud=19200, axis=1)
            print(f"DEBUG: ESP stage initialized successfully")
        except Exception as e:
            print(f"ERROR: Failed to initialize ESP stage: {e}")
            raise
            
        print(f"DEBUG: Creating TriggerHandler...")
        handler = TriggerHandler(esp, mmap_array)

        # --- Step 6: Configure GPIO for hardware triggers ---
        print(f"DEBUG: Configuring GPIO...")
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(Constants.TRIGGER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            print(f"DEBUG: GPIO configured - pin {Constants.TRIGGER_PIN} set as input with pull-down")
            
            # Read initial GPIO state
            initial_state = GPIO.input(Constants.TRIGGER_PIN)
            print(f"DEBUG: Initial GPIO state on pin {Constants.TRIGGER_PIN}: {initial_state}")
        except Exception as e:
            print(f"ERROR: Failed to configure GPIO: {e}")
            raise

        # --- Step 7: Define the main trigger logic ---
        def on_trigger_filtered():
            print(f"DEBUG: on_trigger_filtered() called")
            now = time.time()
            print(f"DEBUG: About to call handler.record_trigger()")
            handler.record_trigger()
            should_move = handler.last_move_time is None or (now - handler.last_move_time >= Settings.MOVE_COOLDOWN_S)
            print(f"DEBUG: should_move = {should_move} (last_move_time: {handler.last_move_time}, cooldown: {Settings.MOVE_COOLDOWN_S})")
            handler.handle_trigger(should_move)

        print(f"DEBUG: Setting external callback...")
        handler.set_external_callback(on_trigger_filtered)
        print(f"DEBUG: Rearming trigger...")
        handler.rearm_trigger()

        # --- Step 8: Run the main loop ---
        print("\nExperiment started. Waiting for triggers or stop signal.")
        print(f"DEBUG: Entering main loop. Stop event set: {stop_event.is_set()}")
        
        loop_count = 0
        while not stop_event.is_set():
            time.sleep(0.1)
            loop_count += 1
            if loop_count % 100 == 0:  # Print every 10 seconds
                print(f"DEBUG: Main loop running... trigger_count={handler.trigger_count if handler else 'N/A'}")
                
        print("\nStop signal received. Cleaning up.")

    except Exception as e:
        print(f"CRITICAL ERROR during experiment: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # --- Final Cleanup and Save ---
        print(f"DEBUG: Starting cleanup and save...")
        if handler:
            try:
                print(f"DEBUG: Saving final NPZ with {handler.trigger_count} timestamps...")
                save_final_npz(local_final_npz_path, mmap_array, handler.trigger_count, ppas, spss, spds, np.abs(move_step_mm))
            except Exception as e:
                print(f"CRITICAL: failed to save local NPZ file: {e}")
                import traceback
                traceback.print_exc()
        
        if local_session_dir:
            try:
                print(f"DEBUG: Copying session data to network share...")
                dest_dir = Path(PATHS.GROUP_ROOT) / Path(local_session_dir).relative_to(PATHS.LOCAL_ROOT)
                dest_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(local_session_dir, dest_dir, dirs_exist_ok=True)
                print(f"Successfully copied session data to: {dest_dir}")
            except Exception as e:
                print(f"Warning: could not copy data to network share: {e}")
                print(f"Data remains available locally at: {local_session_dir}")

        if local_tmp_mmap_path and os.path.exists(local_tmp_mmap_path):
            try:
                print(f"DEBUG: Removing temporary memmap file: {local_tmp_mmap_path}")
                os.remove(local_tmp_mmap_path)
            except Exception as e:
                print(f"Warning: could not remove temporary file: {e}")

        print(f"DEBUG: Cleaning up GPIO...")
        GPIO.cleanup()
        if esp:
            print(f"DEBUG: Closing ESP stage...")
            esp.close()
        print("GPIO cleaned up, stage closed.")
        
        # Notify the main thread that the experiment is finished
        print(f"DEBUG: Calling finish callback...")
        on_finish_callback()

# MAIN FUNCTION #########################################
# This is the main entry point of the script.
def main():
    root = tk.Tk()
    app = ExperimentController(root)
    root.mainloop()
        

# MISC FUNCTIONS ########################################
def ask_for_all_parameters(parent):
    """
    Opens a modal dialog to ask for experimental parameters.
    Returns a dictionary with parameters, or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Experiment Parameters")
    dialog.transient(parent)
    dialog.grab_set()

    params_config = {
        "ppas": {"label": "ppas", "comment": "Pulses per Acquisition State (~exp. time)", "default": "4"},
        "spss": {"label": "spss", "comment": "Shots per Shutter State", "default": "8"},
        "spds": {"label": "spds", "comment": "Shots per Delay State", "default": "100"},
        "move_step_fs": {"label": "Move Step (fs)", "comment": "Temporal resolution (fs)", "default": Settings.MOVE_STEP_FS},
        "max_move_steps": {"label": "Max Move Steps", "comment": "Max. number of steps", "default": Settings.MAX_MOVE_STEPS},
    }

    entries = {}
    for i, (key, data) in enumerate(params_config.items()):
        tk.Label(dialog, text=data["label"]).grid(row=i, column=0, sticky="w", padx=10, pady=5)
        entry = tk.Entry(dialog, width=15)
        entry.grid(row=i, column=1, padx=10, pady=5)
        entry.insert(0, str(data["default"]))
        entries[key] = entry
        tk.Label(dialog, text=data["comment"], fg="grey").grid(row=i, column=2, sticky="w", padx=10, pady=5)

    result = {}
    
    def on_submit():
        nonlocal result
        try:
            result = {
                "ppas": int(entries["ppas"].get()),
                "spss": int(entries["spss"].get()),
                "spds": int(entries["spds"].get()),
                "move_step_fs": float(entries["move_step_fs"].get()),
                "max_move_steps": int(entries["max_move_steps"].get()),
            }
            if any(result[k] <= 0 for k in ["ppas", "spss", "spds", "max_move_steps"]):
                print("Error: ppas, spss, spds, and max_move_steps must be positive values.")
                result = {} # Invalidate result
                return
            dialog.destroy()
        except ValueError:
            print("Error: Invalid input. Please ensure all values are correct numeric types.")
            result = {} # Invalidate result

    def on_closing():
        nonlocal result
        result = None
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", on_closing)
    submit_button = tk.Button(dialog, text="Start Experiment", command=on_submit)
    submit_button.grid(row=len(params_config), column=0, columnspan=3, pady=20)

    parent.wait_window(dialog)
    return result if result else None


def delay_fs_to_mm(delay_fs: float) -> float:
    """
    Converts a delay time in femtoseconds (fs) to a physical distance in millimeters (mm).
    This is based on the speed of light and accounts for the fact that the light
    travels the distance twice (round trip) in a pump-probe experiment.
    
    :param delay_fs: The desired time delay in femtoseconds.
    :return: The corresponding distance the stage needs to move, in millimeters.
    """
    # Speed of light (c) in mm/fs. (299,792,458 m/s) -> (299,792,458 * 1000 mm/s) / (1e15 fs/s)
    c_mm_per_fs = 299_792_458 / 1e15 * 1e3
    # The formula is: distance = (time * speed_of_light) / 2
    # The division by 2 is because the path length difference is twice the stage movement.
    # The movement is typically negative to increase the delay.
    return -1 * round(np.abs(delay_fs * c_mm_per_fs / 2), 4)




# ############### HELPERS FOR PATHS AND SAVING #################
def prepare_session_paths():
    """
    Determines the next available session index by checking the network share (GROUP_ROOT),
    and then creates the corresponding session directory on the local disk (LOCAL_ROOT).
    This is a critical safety feature to prevent overwriting existing data on the server,
    especially if local data has been cleared.

    :return: A tuple containing the paths for the local session directory, the final NPZ file,
             and the temporary binary file.
    """
    print(f"DEBUG: prepare_session_paths() called")
    now = datetime.now()
    yyyy = now.strftime("%Y")
    yymmdd = now.strftime("%y%m%d")
    print(f"DEBUG: Date strings - yyyy: {yyyy}, yymmdd: {yymmdd}")

    # --- Step 1: Determine the next session index by checking the SERVER. ---
    # This ensures that the session number is always unique on the central storage.
    server_day_dir = Path(PATHS.GROUP_ROOT) / yyyy / "DelayStage" / yymmdd
    print(f"DEBUG: Server day dir: {server_day_dir}")
    print(f"DEBUG: Server day dir exists: {server_day_dir.exists()}")
    
    next_idx = 1
    for idx in range(1, 1000):
        idx_str = f"{idx:03d}"
        # Check if a folder for this session index already exists on the server.
        server_session_dir = server_day_dir / f"{yymmdd}_{idx_str}"
        if not server_session_dir.exists():
            # If it doesn't exist, we've found our index.
            next_idx = idx
            print(f"DEBUG: Found available session index: {next_idx}")
            break
    else:
        # This `else` belongs to the `for` loop and executes if the loop completes without a `break`.
        # This would mean all numbers from 001 to 999 are taken.
        raise RuntimeError("No available session indices (001-999) left for today on the server.")

    # --- Step 2: Create the session directory LOCALLY using the determined index. ---
    idx_str = f"{next_idx:03d}"
    local_day_dir = Path(PATHS.LOCAL_ROOT) / yyyy / "DelayStage" / yymmdd
    local_session_dir = local_day_dir / f"{yymmdd}_{idx_str}"
    print(f"DEBUG: Creating local session dir: {local_session_dir}")
    
    # `parents=True` creates any missing parent directories. `exist_ok=True` prevents errors if the folder already exists.
    local_session_dir.mkdir(parents=True, exist_ok=True)
    print(f"DEBUG: Local session dir created. Exists: {local_session_dir.exists()}")

    # --- Step 3: Define and return the full paths for the local files. ---
    npz_path = local_session_dir / f"delay_{yymmdd}_S{idx_str}.npz"
    tmp_path = local_session_dir / f"timestamps_S{idx_str}.bin"
    
    print(f"DEBUG: NPZ path: {npz_path}")
    print(f"DEBUG: TMP path: {tmp_path}")
    
    return str(local_session_dir), str(npz_path), str(tmp_path)


def save_final_npz(npz_path: str, mmap_array: Any, count: int, ppas: int, spss: int, spds: int, step_mm: float):
    """
    Saves the collected timestamp data and metadata into a compressed NumPy file (.npz).
    This function includes a final safety check to prevent accidentally overwriting a file
    if it was created by another process in the meantime.
    
    :param npz_path: The target path for the .npz file.
    :param mmap_array: The memory-mapped array containing the raw timestamp data.
    :param count: The total number of valid timestamps recorded.
    :param ppas, spss, spds: Experimental parameters to be saved as metadata.
    :param step_mm: The move step in millimeters, also saved as metadata.
    """
    print(f"DEBUG: save_final_npz() called with count={count}, npz_path={npz_path}")
    
    # Final safety check: if the target file already exists, find a new name.
    # This is a fallback and should ideally not be triggered if `prepare_session_paths` works correctly.
    if os.path.exists(npz_path):
        print(f"WARNING: NPZ file already exists: {npz_path}")
        session_dir = str(Path(npz_path).parent)
        parent = Path(session_dir).name
        try:
            yymmdd, cur_idx = parent.split("_")
        except ValueError:
            raise FileExistsError(f"Refusing to overwrite existing file: {npz_path}")
        
        base_day_dir = str(Path(session_dir).parent)
        for idx in range(int(cur_idx) + 1, 1000):
            idx_str = f"{idx:03d}"
            new_session_dir = os.path.join(base_day_dir, f"{yymmdd}_{idx_str}")
            new_npz_path = os.path.join(new_session_dir, f"delay_{yymmdd}_S{idx_str}.npz")
            if not os.path.exists(new_npz_path):
                os.makedirs(new_session_dir, exist_ok=True)
                npz_path = new_npz_path
                print(f"DEBUG: Using alternative NPZ path: {npz_path}")
                break

    # Extract the valid data from the memory-mapped array.
    # `mmap_array` was pre-allocated, so we only take the first `count` entries.
    print(f"DEBUG: Extracting {count} timestamps from memmap array")
    try:
        data = np.asarray(mmap_array[:count], dtype=np.int64)
        print(f"DEBUG: Extracted data shape: {data.shape}, dtype: {data.dtype}")
        print(f"DEBUG: First few timestamps: {data[:min(5, len(data))].tolist()}")
        print(f"DEBUG: Last few timestamps: {data[max(0, len(data)-5):].tolist()}")
    except Exception as e:
        print(f"ERROR: Failed to extract data from memmap: {e}")
        raise
    
    # Save the data and metadata into a single, compressed .npz file.
    # This is efficient and keeps all relevant information for a run together.
    try:
        print(f"DEBUG: Saving NPZ file with metadata - ppas:{ppas}, spss:{spss}, spds:{spds}, step_mm:{step_mm}")
        np.savez_compressed(
            npz_path,
            timestamps_us=data,
            ppas=int(ppas),
            spss=int(spss),
            spds=int(spds),
            step_mm=float(step_mm),
        )
        print(f"Saved NPZ: {npz_path} (timestamps: {count})")
        print(f"DEBUG: NPZ file exists after save: {os.path.exists(npz_path)}")
        if os.path.exists(npz_path):
            print(f"DEBUG: NPZ file size: {os.path.getsize(npz_path)} bytes")
    except Exception as e:
        print(f"ERROR: Failed to save NPZ file: {e}")
        raise
    
# This ensures that the `main()` function is called only when the script is executed directly.
if __name__ == "__main__":
    main()
