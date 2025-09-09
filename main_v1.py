import threading
import time
import numpy as np
import os
import RPi.GPIO as GPIO
from threading import Timer
from datetime import datetime
import tkinter as tk
from tkinter import simpledialog, messagebox, scrolledtext
from pathlib import Path
from typing import Any, Callable
import shutil
import h5py
import sys
import io
from collections import deque

from espstage import ESPStage
# A comment to notify that this version works with v3
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
    # The maximum delay in fs is MOVE_STEP_FS * MAX_MOVE_STEPS!
    # This acts as a safety limit and determines the size of the data array.
    MAX_MOVE_STEPS = 200
    
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
    def __init__(self, esp: ESPStage, local_h5_path: str, max_timestamps: int):
        """
        Initializes the TriggerHandler.
        
        :param esp: An instance of the ESPStage controller class, used to send commands to the motor.
        :param local_h5_path: Path to the local HDF5 file for data storage.
        :param max_timestamps: Maximum number of timestamps to store.
        """
        print(f"DEBUG: Initializing TriggerHandler with HDF5 path: {local_h5_path}")
        self.esp = esp
        self.local_h5_path = local_h5_path
        self.max_timestamps = max_timestamps
        self.trigger_count = 0
        self.move_count = 0
        self.last_trigger_time = None
        self.last_move_time = None
        self._external_callback: Callable[[], None] = lambda: None
        self.h5_lock = threading.Lock()  # Thread-safe access to HDF5
        
        # Initialize HDF5 file
        self._init_h5_file()
        print(f"DEBUG: TriggerHandler initialized. trigger_count={self.trigger_count}, move_count={self.move_count}")

    def _init_h5_file(self):
        """Initialize the local HDF5 file with datasets."""
        print(f"DEBUG: Initializing HDF5 file: {self.local_h5_path}")
        try:
            with h5py.File(self.local_h5_path, 'w') as f:
                # Create dataset for timestamps
                f.create_dataset('timestamps_us', (self.max_timestamps,), dtype='int64', 
                               maxshape=(None,), fillvalue=0)
                
                # Create metadata group
                meta = f.create_group('metadata')
                meta.attrs['trigger_count'] = 0
                meta.attrs['move_count'] = 0
                meta.attrs['created'] = datetime.now().isoformat()
                
                print(f"DEBUG: HDF5 file initialized successfully")
        except Exception as e:
            print(f"ERROR: Failed to initialize HDF5 file: {e}")
            raise

    def record_trigger(self):
        """Records a single timestamp when a hardware trigger is detected."""
        print(f"DEBUG: record_trigger() called. Current trigger_count: {self.trigger_count}")
        
        now = time.time()
        timestamp_us = int(time.time_ns() // 1000)
        print(f"DEBUG: Generated timestamp - now: {now:.6f}, timestamp_us: {timestamp_us}")

        if self.trigger_count < self.max_timestamps:
            print(f"DEBUG: Writing timestamp to HDF5 at index {self.trigger_count}")
            
            try:
                with self.h5_lock:
                    with h5py.File(self.local_h5_path, 'a') as f:
                        # Write timestamp
                        f['timestamps_us'][self.trigger_count] = timestamp_us
                        
                        # Update metadata
                        f['metadata'].attrs['trigger_count'] = self.trigger_count + 1
                        f['metadata'].attrs['last_update'] = datetime.now().isoformat()
                        
                        # Flush to disk
                        f.flush()
                        
                print(f"DEBUG: Successfully wrote timestamp {timestamp_us} to HDF5")
                
                delta_trigger = now - self.last_trigger_time if self.last_trigger_time else 0
                delta_move = now - self.last_move_time if self.last_move_time else 0
                print(f"Trigger {self.trigger_count + 1} received — Δt_trigger = {delta_trigger:.2f}s, Δt_move = {delta_move:.2f}s")
                
                self.last_trigger_time = now
                self.trigger_count += 1
                print(f"DEBUG: Incremented trigger_count to {self.trigger_count}")
                
            except Exception as e:
                print(f"ERROR: Failed to write timestamp to HDF5: {e}")
                return
        else:
            print(f"ERROR: Max timestamps reached. trigger_count={self.trigger_count}, max={self.max_timestamps}. Ignoring trigger.")

    def perform_move(self):
        """Executes a relative move of the delay stage."""
        print(f"DEBUG: perform_move() called. Current move_count: {self.move_count}")
        
        print(f"DEBUG: Removing GPIO event detect on pin {Constants.TRIGGER_PIN}")
        GPIO.remove_event_detect(Constants.TRIGGER_PIN)
        
        try:
            print(f"DEBUG: Sending move command: {float(Settings.MOVE_STEP_MM)} mm")
            self.esp.move_relative(float(Settings.MOVE_STEP_MM))
            self.last_move_time = time.time()
            self.move_count += 1
            
            # Update move count in HDF5
            with self.h5_lock:
                with h5py.File(self.local_h5_path, 'a') as f:
                    f['metadata'].attrs['move_count'] = self.move_count
                    
            print(f"Move {self.move_count} executed successfully")
        except Exception as e:
            print(f"ERROR: Failed to execute move: {e}")
        
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
        

# HDF5 SYNC MANAGER #####################################
class HDF5SyncManager:
    def __init__(self, local_h5_path: str, server_h5_path: str):
        self.local_h5_path = local_h5_path
        self.server_h5_path = server_h5_path
        self.sync_thread = None
        self.stop_event = threading.Event()
        self.sync_interval = 10.0  # seconds
        
    def start_sync(self):
        """Start the background sync thread."""
        print(f"DEBUG: Starting HDF5 sync thread")
        self.stop_event.clear()
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
        
    def stop_sync(self):
        """Stop the background sync thread."""
        print(f"DEBUG: Stopping HDF5 sync thread")
        self.stop_event.set()
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=5.0)
            
    def _sync_loop(self):
        """Background thread loop for syncing HDF5 files."""
        print(f"DEBUG: HDF5 sync loop started")
        while not self.stop_event.is_set():
            try:
                self._sync_to_server()
            except Exception as e:
                print(f"WARNING: Failed to sync HDF5 to server: {e}")
                
            # Wait for next sync interval or stop signal
            self.stop_event.wait(self.sync_interval)
            
        print(f"DEBUG: HDF5 sync loop stopped")
        
    def _sync_to_server(self):
        """Sync the local HDF5 file to the server."""
        if not os.path.exists(self.local_h5_path):
            return
            
        try:
            # Ensure server directory exists
            server_dir = Path(self.server_h5_path).parent
            server_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy the HDF5 file
            shutil.copy2(self.local_h5_path, self.server_h5_path)
            # Get relative path by removing the root directory
            relative_path = str(Path(self.server_h5_path).relative_to(PATHS.GROUP_ROOT))
            print(f"DEBUG: Synced HDF5 to server: .../{relative_path}")
            
        except Exception as e:
            print(f"ERROR: Failed to sync HDF5 to server: {e}")
            raise

# MAIN APPLICATION CLASS #########################################
class ExperimentController:
    def __init__(self, root):
        self.root = root
        self.root.title("Experiment Control")
        self.root.geometry("1280x720")  # Increased height for terminal output
        self.root.minsize(1280, 600)    # Set minimum window size
        self.experiment_thread = None
        self.stop_event = threading.Event()
        
        # Terminal output buffer (keep last 50 lines)
        self.terminal_buffer = deque(maxlen=50)
        
        # Create main container
        container = tk.Frame(root)
        container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        
        # Top frame for controls and settings
        top_frame = tk.Frame(container)
        top_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 10))
        
        # Left side - Control buttons
        control_frame = tk.Frame(top_frame, relief=tk.RAISED, borderwidth=1, bg="#f0f0f0")
        control_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        
        tk.Label(control_frame, text="Experiment Control", font=("Arial", 18, "bold"), bg="#f0f0f0").pack(pady=(20, 30))
        
        self.start_button = tk.Button(control_frame, text="Start Experiment", command=self.start_experiment, 
                                    font=("Arial", 18, "bold"), bg="#4CAF50", fg="white", width=18, height=3)
        self.start_button.pack(pady=15)

        self.stop_button = tk.Button(control_frame, text="Stop Experiment", command=self.stop_experiment, 
                                   state=tk.DISABLED, font=("Arial", 18, "bold"), bg="#f44336", fg="white", width=18, height=3)
        self.stop_button.pack(pady=15)
        
        # Right side - Settings
        settings_frame = tk.Frame(top_frame, relief=tk.RAISED, borderwidth=1, bg="#fafafa")
        settings_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        
        # Warning comment
        warning_label = tk.Label(settings_frame, text="⚠️ Changes should not be made during an experiment", 
                               font=("Arial", 18, "italic"), fg="red", bg="#fafafa")
        warning_label.pack(pady=(15, 10))
        
        tk.Label(settings_frame, text="Experiment Settings", font=("Arial", 18, "bold"), bg="#fafafa").pack(pady=(0, 25))
        
        # Create settings entries
        self.entries = {}
        params_config = {
            "ppas": {"label": "Pulses per Acquisition State", "comment": "~exp. time", "default": "30"},
            "spss": {"label": "Shots per Shutter State", "comment": "", "default": "10"},
            "spds": {"label": "Shots per Delay State", "comment": "", "default": "80"},
            "move_step_fs": {"label": "Move Step (fs)", "comment": "Temporal resolution", "default": Settings.MOVE_STEP_FS},
            "MAX_MOVE_STEPS": {"label": "Max delay steps", "comment": "NOT the max. delay in fs", "default": Settings.MAX_MOVE_STEPS},
        }
        
        for i, (key, data) in enumerate(params_config.items()):
            param_frame = tk.Frame(settings_frame, bg="#fafafa")
            param_frame.pack(fill=tk.X, padx=20, pady=8)
            
            tk.Label(param_frame, text=data["label"], width=25, anchor="w", 
                    font=("Arial", 18), bg="#fafafa").pack(side=tk.LEFT)
            entry = tk.Entry(param_frame, width=12, font=("Arial", 18))
            entry.pack(side=tk.LEFT, padx=(10, 5))
            entry.insert(0, str(data["default"]))
            self.entries[key] = entry
            
            if data["comment"]:
                tk.Label(param_frame, text=f"({data['comment']})", fg="grey", 
                        font=("Arial", 18), bg="#fafafa").pack(side=tk.LEFT)

        # Configure top frame grid weights
        top_frame.grid_columnconfigure(0, weight=1)
        top_frame.grid_columnconfigure(1, weight=2)
        top_frame.grid_rowconfigure(0, weight=1)
        
        # Bottom frame - Terminal output
        terminal_frame = tk.Frame(container, relief=tk.RAISED, borderwidth=1, bg="#1e1e1e")
        terminal_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        
        # Terminal header with title and clear button
        terminal_header = tk.Frame(terminal_frame, bg="#1e1e1e")
        terminal_header.pack(fill=tk.X, padx=10, pady=(10, 5))
        
        tk.Label(terminal_header, text="Terminal Output (Last 15 lines)", font=("Arial", 14, "bold"), 
                bg="#1e1e1e", fg="white").pack(side=tk.LEFT)
        
        clear_button = tk.Button(terminal_header, text="Clear", font=("Arial", 12), 
                               bg="#555555", fg="white", command=self.clear_terminal)
        clear_button.pack(side=tk.RIGHT)
        
        # Create scrolled text widget for terminal output
        self.terminal_output = scrolledtext.ScrolledText(
            terminal_frame, 
            height=30, 
            bg="#2d2d2d", 
            fg="#00ff00",  # Green text like terminal
            font=("Consolas", 12),
            insertbackground="white",
            state=tk.DISABLED,
            wrap=tk.WORD
        )
        self.terminal_output.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        # Configure color tags for different message types
        self.terminal_output.tag_config("error", foreground="#ff4444")
        self.terminal_output.tag_config("warning", foreground="#ffaa00")
        self.terminal_output.tag_config("debug", foreground="#888888")
        self.terminal_output.tag_config("important", foreground="#00aaff")
        self.terminal_output.tag_config("normal", foreground="#00ff00")
        
        # Setup print redirection
        self.setup_print_capture()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def setup_print_capture(self):
        """Setup print statement capture to display in GUI terminal"""
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        
        # Create a custom output stream that captures print statements
        class TerminalCapture:
            def __init__(self, gui_callback):
                self.gui_callback = gui_callback
                
            def write(self, text):
                if text.strip():  # Only process non-empty lines
                    self.gui_callback(text)
                # Also write to original stdout so it still appears in console
                sys.__stdout__.write(text)
                
            def flush(self):
                sys.__stdout__.flush()
        
        # Redirect stdout to our capture
        sys.stdout = TerminalCapture(self.add_terminal_line)
    
    def add_terminal_line(self, text):
        """Add a line to the terminal output display"""
        def update_gui():
            # Add to buffer with color coding
            lines = text.strip().split('\n')
            for line in lines:
                if line.strip():
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    
                    # Determine color based on content
                    if line.startswith("ERROR") or line.startswith("CRITICAL"):
                        color_tag = "error"
                    elif line.startswith("WARNING"):
                        color_tag = "warning"
                    elif line.startswith("DEBUG"):
                        color_tag = "debug"
                    elif "Experiment started" in line or "finished" in line:
                        color_tag = "important"
                    else:
                        color_tag = "normal"
                    
                    formatted_line = f"[{timestamp}] {line}\n"
                    self.terminal_buffer.append((formatted_line, color_tag))
            
            # Update the display
            self.update_terminal_display()
        
        # Schedule GUI update in main thread
        self.root.after(0, update_gui)
    
    def clear_terminal(self):
        """Clear the terminal output display"""
        self.terminal_buffer.clear()
        self.terminal_output.config(state=tk.NORMAL)
        self.terminal_output.delete(1.0, tk.END)
        self.terminal_output.config(state=tk.DISABLED)
        
    def update_terminal_display(self):
        """Update the terminal display widget"""
        self.terminal_output.config(state=tk.NORMAL)
        self.terminal_output.delete(1.0, tk.END)
        
        # Show last 15 lines from buffer with color coding
        recent_lines = list(self.terminal_buffer)[-15:]
        for line_data in recent_lines:
            if isinstance(line_data, tuple):
                line, color_tag = line_data
                self.terminal_output.insert(tk.END, line, color_tag)
            else:
                # Fallback for old format
                self.terminal_output.insert(tk.END, line_data, "normal")
        
        # Auto-scroll to bottom
        self.terminal_output.see(tk.END)
        self.terminal_output.config(state=tk.DISABLED)

    def start_experiment(self):
        # Get parameters from the settings entries
        try:
            params = {
                "ppas": int(self.entries["ppas"].get()),
                "spss": int(self.entries["spss"].get()),
                "spds": int(self.entries["spds"].get()),
                "move_step_fs": float(self.entries["move_step_fs"].get()),
                "MAX_MOVE_STEPS": int(self.entries["MAX_MOVE_STEPS"].get()),
            }
            
            # Validate parameters
            if any(params[k] <= 0 for k in ["ppas", "spss", "spds", "MAX_MOVE_STEPS"]):
                tk.messagebox.showerror("Error", "ppas, spss, spds, and MAX_MOVE_STEPS must be positive values.")
                return
                
        except ValueError:
            tk.messagebox.showerror("Error", "Invalid input. Please ensure all values are correct numeric types.")
            return

        # Disable settings entries during experiment
        for entry in self.entries.values():
            entry.config(state=tk.DISABLED)

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
        # Re-enable settings entries
        for entry in self.entries.values():
            entry.config(state=tk.NORMAL)
            
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.experiment_thread = None
        print("Experiment finished and GUI updated.")

    def on_closing(self):
        # Restore original stdout
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        
        if self.experiment_thread and self.experiment_thread.is_alive():
            print("Please stop the experiment before closing.")
        else:
            self.root.destroy()

def run_experiment(params, stop_event, on_finish_callback):
    print(f"DEBUG: run_experiment() started with params: {params}")
    esp = None
    handler = None
    sync_manager = None
    local_session_dir = None
    
    try:
        # --- Step 1: Apply parameters ---
        ppas, spss, spds = params["ppas"], params["spss"], params["spds"]
        Settings.MOVE_STEP_FS = params["move_step_fs"]
        Settings.MAX_MOVE_STEPS = params["MAX_MOVE_STEPS"]
        print(f"DEBUG: Applied parameters - ppas:{ppas}, spss:{spss}, spds:{spds}, move_step_fs:{Settings.MOVE_STEP_FS}, MAX_MOVE_STEPS:{Settings.MAX_MOVE_STEPS}")

        # --- Step 2: Calculate physical constants ---
        move_step_mm = delay_fs_to_mm(Settings.MOVE_STEP_FS)
        Settings.MOVE_STEP_MM = move_step_mm
        print(f"Using MOVE_STEP_FS = {Settings.MOVE_STEP_FS:.2f} fs → MOVE_STEP_MM = {move_step_mm:.4f} mm")

        # --- Step 3: Prepare storage paths ---
        print(f"DEBUG: Preparing session paths...")
        local_session_dir, local_h5_path, server_h5_path = prepare_session_paths()
        print(f"Saving data locally to: {local_session_dir}")
        print(f"DEBUG: Local HDF5 path: {local_h5_path}")
        print(f"DEBUG: Server HDF5 path: {server_h5_path}")
        
        # --- Step 4: Initialize hardware controllers ---
        print(f"DEBUG: Initializing ESP stage on /dev/ttyUSB0...")
        try:
            esp = ESPStage("/dev/ttyUSB0", baud=19200, axis=1)
            print(f"DEBUG: ESP stage initialized successfully")
        except Exception as e:
            print(f"ERROR: Failed to initialize ESP stage: {e}")
            raise
            
        print(f"DEBUG: Creating TriggerHandler...")
        max_timestamps = Settings.MAX_MOVE_STEPS * 2
        handler = TriggerHandler(esp, local_h5_path, max_timestamps)

        # --- Step 5: Start HDF5 sync manager ---
        print(f"DEBUG: Starting HDF5 sync manager...")
        sync_manager = HDF5SyncManager(local_h5_path, server_h5_path)
        sync_manager.start_sync()

        # --- Step 6: Configure GPIO for hardware triggers ---
        print(f"DEBUG: Configuring GPIO...")
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(Constants.TRIGGER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            print(f"DEBUG: GPIO configured - pin {Constants.TRIGGER_PIN} set as input with pull-down")
            
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

        # --- Step 8: Save experimental parameters to HDF5 ---
        print(f"DEBUG: Saving experimental parameters to HDF5...")
        save_experiment_metadata(local_h5_path, ppas, spss, spds, np.abs(move_step_mm), Settings.MAX_MOVE_STEPS)

        # --- Step 9: Run the main loop ---
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
        print(f"DEBUG: Starting cleanup...")
        
        if sync_manager:
            print(f"DEBUG: Stopping sync manager and performing final sync...")
            sync_manager.stop_sync()
            # Perform one final sync
            try:
                sync_manager._sync_to_server()
            except Exception as e:
                print(f"Warning: Final sync failed: {e}")

        print(f"DEBUG: Cleaning up GPIO...")
        GPIO.cleanup()
        if esp:
            print(f"DEBUG: Closing ESP stage...")
            esp.close()
        print("GPIO cleaned up, stage closed.")
        
        print(f"DEBUG: Calling finish callback...")
        on_finish_callback()

# MAIN FUNCTION #########################################
# This is the main entry point of the script.
def main():
    root = tk.Tk()
    app = ExperimentController(root)
    root.mainloop()
        

# MISC FUNCTIONS ########################################
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
    Determines the next available session index and creates session directory paths.
    Returns paths for local and server HDF5 files.
    """
    print(f"DEBUG: prepare_session_paths() called")
    now = datetime.now()
    yyyy = now.strftime("%Y")
    yymmdd = now.strftime("%y%m%d")
    print(f"DEBUG: Date strings - yyyy: {yyyy}, yymmdd: {yymmdd}")

    # --- Step 1: Determine the next session index by checking the SERVER. ---
    server_day_dir = Path(PATHS.GROUP_ROOT) / yyyy / "DelayStage" / yymmdd
    print(f"DEBUG: Server day dir: {server_day_dir}")
    print(f"DEBUG: Server day dir exists: {server_day_dir.exists()}")
    
    next_idx = 1
    for idx in range(1, 1000):
        idx_str = f"{idx:03d}"
        server_session_dir = server_day_dir / f"{yymmdd}_{idx_str}"
        if not server_session_dir.exists():
            next_idx = idx
            print(f"DEBUG: Found available session index: {next_idx}")
            break
    else:
        raise RuntimeError("No available session indices (001-999) left for today on the server.")

    # --- Step 2: Create the session directory LOCALLY using the determined index. ---
    idx_str = f"{next_idx:03d}"
    local_day_dir = Path(PATHS.LOCAL_ROOT) / yyyy / "DelayStage" / yymmdd
    local_session_dir = local_day_dir / f"{yymmdd}_{idx_str}"
    print(f"DEBUG: Creating local session dir: {local_session_dir}")
    
    local_session_dir.mkdir(parents=True, exist_ok=True)
    print(f"DEBUG: Local session dir created. Exists: {local_session_dir.exists()}")

    # --- Step 3: Define HDF5 file paths ---
    h5_filename = f"delay_{yymmdd}_S{idx_str}.hdf5"
    local_h5_path = local_session_dir / h5_filename
    
    server_session_dir = Path(PATHS.GROUP_ROOT) / yyyy / "DelayStage" / yymmdd / f"{yymmdd}_{idx_str}"
    server_h5_path = server_session_dir / h5_filename
    
    print(f"DEBUG: Local HDF5 path: {local_h5_path}")
    print(f"DEBUG: Server HDF5 path: {server_h5_path}")
    
    return str(local_session_dir), str(local_h5_path), str(server_h5_path)


def save_experiment_metadata(h5_path: str, ppas: int, spss: int, spds: int, step_mm: float, MAX_MOVE_STEPS_fs: float):
    """
    Save experimental parameters to the HDF5 file metadata.
    """
    print(f"DEBUG: save_experiment_metadata() called")
    try:
        with h5py.File(h5_path, 'a') as f:
            meta = f['metadata']
            meta.attrs['ppas'] = ppas
            meta.attrs['spss'] = spss
            meta.attrs['spds'] = spds
            meta.attrs['step_mm'] = step_mm
            meta.attrs['max_move_step_fs'] = MAX_MOVE_STEPS_fs
            meta.attrs['experiment_started'] = datetime.now().isoformat()
            f.flush()
        print(f"DEBUG: Experimental metadata saved to HDF5")
    except Exception as e:
        print(f"ERROR: Failed to save experimental metadata: {e}")
        raise

# This ensures that the `main()` function is called only when the script is executed directly.

    
# This ensures that the `main()` function is called only when the script is executed directly.
if __name__ == "__main__":
    main()
