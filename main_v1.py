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
import h5py

from espstage import ESPStage

# PATHS #################################################
# This class holds the directory paths for data storage.
# It's designed to separate temporary local storage from the final network storage
# to ensure data integrity even if the network connection is lost.
class PATHS:
    # GROUP_ROOT is the final destination for the data on the network share.
    # All session data will be copied here upon successful completion of a run.
    # Example for a real setup: "/home/moritz/groupshares/attophys/Attoline"
    GROUP_ROOT = '/home/moritz/groupshares/attophys/Attoline/2025/DelayStage'
    
    # LOCAL_ROOT is the temporary storage location on the Raspberry Pi's local disk.
    # Data is saved here first to prevent data loss if the network is unavailable.
    LOCAL_ROOT = '/home/moritz/Desktop/DelayStage_Data'

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
    def __init__(self, esp: ESPStage, h5_file: h5py.File):
        """
        Initializes the TriggerHandler.
        
        :param esp: An instance of the ESPStage controller class, used to send commands to the motor.
        :param h5_file: An open h5py.File object for real-time data recording.
        """
        self.esp = esp
        self.h5_file = h5_file
        self.timestamp_dataset = h5_file['timestamps_us']
        self.trigger_count = 0  # Counter for the number of triggers received.
        self.move_count = 0     # Counter for the number of moves performed.
        self.last_trigger_time = None  # Timestamp of the last received trigger.
        self.last_move_time = None     # Timestamp of the last move command.
        self._external_callback: Callable[[], None] = lambda: None  # Placeholder for the main logic callback.

    def record_trigger(self):
        """
        Records a single timestamp to the HDF5 file when a hardware trigger is detected.
        """
        # Record the current time with high precision.
        now = time.time()
        timestamp_us = int(time.time_ns() // 1000)  # Convert nanoseconds to microseconds.

        # Resize the HDF5 dataset to accommodate the new timestamp.
        self.timestamp_dataset.resize((self.trigger_count + 1,))
        # Write the new timestamp to the end of the dataset.
        self.timestamp_dataset[self.trigger_count] = timestamp_us
        # .flush() ensures the data is written from memory to the disk immediately.
        self.h5_file.flush()
        
        # Print diagnostic information about the timing.
        delta_trigger = now - self.last_trigger_time if self.last_trigger_time else 0
        delta_move = now - self.last_move_time if self.last_move_time else 0
        print(f"Trigger {self.trigger_count + 1} received — Δt_trigger = {delta_trigger:.2f}s, Δt_move = {delta_move:.2f}s")
        
        self.last_trigger_time = now
        self.trigger_count += 1

    def perform_move(self):
        """
        Executes a relative move of the delay stage.
        """
        # Temporarily disable the GPIO trigger to prevent accidental firing during the move.
        GPIO.remove_event_detect(Constants.TRIGGER_PIN)
        # Send the move command to the stage controller.
        self.esp.move_relative(float(Settings.MOVE_STEP_MM))
        self.last_move_time = time.time()
        self.move_count += 1
        print(f"Move {self.move_count} executed")
        # Start a timer to re-enable the trigger after the cooldown period.
        Timer(Settings.TRIGGER_COOLDOWN_S, self.rearm_trigger).start()

    def rearm_trigger(self):
        """
        Re-enables the GPIO event detection after a move is complete.
        """
        # Add the event detector back to the GPIO pin.
        # GPIO.RISING means it triggers on a voltage change from low to high.
        # `bouncetime` helps to debounce the signal, preventing multiple triggers from a single event.
        GPIO.add_event_detect(Constants.TRIGGER_PIN, GPIO.RISING, callback=self._raw_callback, bouncetime=50)
        print("Trigger rearmed. Waiting for hardware triggers...\n")

    def _raw_callback(self, channel: int):
        """
        A lightweight, raw callback function that is directly called by the GPIO library.
        Its only job is to call the main external callback.
        """
        self._external_callback()

    def set_external_callback(self, callback: Callable[[], None]):
        """
        Assigns the main experimental logic to be executed when a trigger occurs.
        """
        self._external_callback = callback
        
    def handle_trigger(self, should_move: bool):
        """
        Handles the logic for a trigger event, deciding whether to move the stage or not.
        
        :param should_move: A boolean indicating if the conditions for a move are met.
        """
        # Always disarm the trigger immediately to prevent multiple detections.
        GPIO.remove_event_detect(Constants.TRIGGER_PIN)
        if should_move:
            # If a move is warranted, command the stage to move.
            self.esp.move_relative(float(Settings.MOVE_STEP_MM))
            self.last_move_time = time.time()
            self.move_count += 1
            print(f"Move {self.move_count} executed")
        else:
            # If not enough time has passed since the last move, ignore this trigger for movement purposes.
            delta = time.time() - self.last_move_time if self.last_move_time is not None else 0.0
            print(f"Ignored move (only {delta:.1f}s since last move)")
        
        # Always re-arm the trigger after the standard cooldown period.
        Timer(Settings.TRIGGER_COOLDOWN_S, self.rearm_trigger).start()

# BACKGROUND COPIER #####################################
def background_copier(local_path: str, remote_path: str, stop_event: threading.Event):
    """
    A target function for a background thread that periodically copies a file.

    :param local_path: The source file path to copy from.
    :param remote_path: The destination file path to copy to.
    :param stop_event: A threading.Event to signal when the thread should stop.
    """
    print("Background copy thread started.")
    while not stop_event.wait(5.0):  # Wait for 5 seconds, or until the event is set
        try:
            if not os.path.exists(local_path):
                print(f"Warning: Source file {local_path} not found for copying.")
                continue

            # Ensure the remote directory exists before copying.
            remote_dir = os.path.dirname(remote_path)
            os.makedirs(remote_dir, exist_ok=True)
            
            # Copy the file, overwriting the destination.
            shutil.copy(local_path, remote_path)
            print(f"Successfully copied data to network: {remote_path}")
        except Exception as e:
            print(f"Warning: Failed to copy file to network share: {e}")
    print("Background copy thread stopped.")
        

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
    esp = None
    copy_thread = None
    copy_stop_event = threading.Event()
    local_hdf5_path = None
    
    try:
        # --- Step 1: Apply parameters ---
        Settings.MOVE_STEP_FS = params["move_step_fs"]
        Settings.MAX_MOVE_STEPS = params["max_move_steps"]

        # --- Step 2: Calculate physical constants ---
        move_step_mm = delay_fs_to_mm(Settings.MOVE_STEP_FS)
        Settings.MOVE_STEP_MM = move_step_mm
        print(f"Using MOVE_STEP_FS = {Settings.MOVE_STEP_FS:.2f} fs → MOVE_STEP_MM = {move_step_mm:.4f} mm")

        # --- Step 3: Prepare storage paths for HDF5 ---
        local_session_dir, local_hdf5_path, remote_hdf5_path = prepare_session_paths_hdf5()
        print(f"Saving data locally to: {local_hdf5_path}")
        print(f"Network destination: {remote_hdf5_path}")

        # --- Step 4: Set up HDF5 file and background copier ---
        with h5py.File(local_hdf5_path, 'w') as hf:
            # Create a resizable dataset for timestamps
            hf.create_dataset('timestamps_us', (0,), maxshape=(None,), dtype='int64', chunks=True)
            
            # Store metadata as attributes in the HDF5 file
            hf.attrs['ppas'] = params['ppas']
            hf.attrs['spss'] = params['spss']
            hf.attrs['spds'] = params['spds']
            hf.attrs['move_step_fs'] = Settings.MOVE_STEP_FS
            hf.attrs['move_step_mm'] = np.abs(move_step_mm)
            hf.attrs['start_time_utc'] = datetime.utcnow().isoformat()

            # --- Step 5: Start the background copy thread ---
            copy_thread = threading.Thread(target=background_copier, args=(local_hdf5_path, remote_hdf5_path, copy_stop_event))
            copy_thread.start()

            # --- Step 6: Initialize hardware controllers ---
            esp = ESPStage("/dev/ttyUSB0", baud=19200, axis=1)
            handler = TriggerHandler(esp, hf)

            # --- Step 7: Configure GPIO for hardware triggers ---
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(Constants.TRIGGER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

            # --- Step 8: Define the main trigger logic ---
            def on_trigger_filtered():
                now = time.time()
                handler.record_trigger()
                should_move = handler.last_move_time is None or (now - handler.last_move_time >= Settings.MOVE_COOLDOWN_S)
                handler.handle_trigger(should_move)

            handler.set_external_callback(on_trigger_filtered)
            handler.rearm_trigger()

            # --- Step 9: Run the main loop ---
            print("\nExperiment started. Waiting for triggers or stop signal.")
            while not stop_event.is_set():
                time.sleep(0.1)
            print("\nStop signal received. Cleaning up.")
            hf.attrs['end_time_utc'] = datetime.utcnow().isoformat()

    except Exception as e:
        print(f"CRITICAL ERROR during experiment: {e}")
    finally:
        # --- Final Cleanup ---
        if copy_thread:
            copy_stop_event.set()
            copy_thread.join() # Wait for the copy thread to finish its last cycle
        
        # Perform one final copy to ensure the very last data points are synced
        if local_hdf5_path and os.path.exists(local_hdf5_path):
            try:
                shutil.copy(local_hdf5_path, remote_hdf5_path)
                print(f"Final data sync successful to: {remote_hdf5_path}")
            except Exception as e:
                print(f"Warning: final data sync to network share failed: {e}")
                print(f"Data remains available locally at: {local_hdf5_path}")

        GPIO.cleanup()
        if esp:
            esp.close()
        print("GPIO cleaned up, stage closed.")
        
        # Notify the main thread that the experiment is finished
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
def prepare_session_paths_hdf5():
    """
    Determines the next available session index by checking the network share (GROUP_ROOT),
    and then creates the corresponding session directory and paths for HDF5 files.

    :return: A tuple containing (local_session_dir, local_hdf5_path, remote_hdf5_path).
    """
    now = datetime.now()
    yyyy = now.strftime("%Y")
    yymmdd = now.strftime("%y%m%d")

    # --- Step 1: Determine the next session index by checking the SERVER. ---
    server_day_dir = Path(PATHS.GROUP_ROOT) / yyyy / "DelayStage" / yymmdd
    next_idx = 1
    if server_day_dir.exists():
        existing_sessions = [d.name for d in server_day_dir.iterdir() if d.is_dir()]
        existing_indices = set()
        for session in existing_sessions:
            try:
                # Assuming format yymmdd_xxx
                idx = int(session.split('_')[-1])
                existing_indices.add(idx)
            except (ValueError, IndexError):
                continue
        
        while next_idx in existing_indices:
            next_idx += 1

    # --- Step 2: Create the session directory LOCALLY using the determined index. ---
    idx_str = f"{next_idx:03d}"
    session_name = f"{yymmdd}_{idx_str}"
    
    local_session_dir = Path(PATHS.LOCAL_ROOT) / yyyy / "DelayStage" / yymmdd / session_name
    local_session_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 3: Define and return the full paths for local and remote HDF5 files. ---
    file_name = f"delay_{yymmdd}_S{idx_str}.h5"
    local_path = local_session_dir / file_name
    
    remote_session_dir = server_day_dir / session_name
    remote_path = remote_session_dir / file_name
    
    return str(local_session_dir), str(local_path), str(remote_path)
    
# This ensures that the `main()` function is called only when the script is executed directly.
if __name__ == "__main__":
    main()
