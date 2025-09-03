import numpy as np
import matplotlib.pyplot as plt
from tkinter import Tk, filedialog
from typing import Tuple
import os
import re


def main():
    # File selection dialog
    root = Tk()
    root.withdraw()
    root.update()
    file_path = filedialog.askopenfilename(
        title="Select delayStage timestamp file",
        initialdir="/home/moritz/Desktop/delayStage_recorded_data",
        filetypes=[("Raw Memmap Files", "*.npz")]
    )
    root.destroy()

    if not file_path or not os.path.exists(file_path):
        print("No file selected or file not found.")
        return

    try:
        timestamps_s, spss, spds = get_delay_timestamps_spss_spds(file_path)
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    print(f"\nspss = {spss}, spds = {spds}\n")
    print("Timestamps (seconds since epoch):")
    for i, ts in enumerate(timestamps_s):
        if i == 0:
            print(f"{i:3d}: {ts:.6f} s  (Δt = ---)")
        else:
            dt = ts - timestamps_s[i - 1]
            print(f"{i:3d}: {ts:.6f} s  (Δt = {dt:.6f} s)")

    # Plot time evolution
    times_relative = timestamps_s - timestamps_s[0]
    plt.figure(figsize=(10, 5))
    plt.plot(times_relative, marker='o', linestyle='-')
    plt.xlabel("Trigger index")
    plt.ylabel("Time since first trigger [s]")
    plt.title(f"Trigger Timestamps (spss={spss}, spds={spds})")
    plt.grid(True)
    plt.tight_layout()
    plt.show()
    

def get_delay_timestamps_spss_spds(filename: str) -> Tuple[np.ndarray, int, int]:
    """
    Reads the timestamp file and extracts timestamps, spss, and spds.

    :param filename: Path to the .npz file (memmap format)
    :return: Tuple containing:
        - timestamps_s (np.ndarray): Trigger timestamps in seconds.
        - spss (int): Seconds per step.
        - spds (int): Steps per delay stage.
    """
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File not found: {filename}")

    # Read memmap and filter valid entries
    mmap_data = np.memmap(filename, dtype='int64', mode='r')
    timestamps_us = np.array(mmap_data[mmap_data > 0])
    timestamps_s = timestamps_us / 1e6

    # Extract spss and spds from filename using regex
    match = re.search(r"spss_(\d+)_spds_(\d+)", filename)
    if not match:
        raise ValueError("spss and spds not found in filename. Expected format: *_spss_<num>_spds_<num>.npz")

    spss = int(match.group(1))
    spds = int(match.group(2))

    return timestamps_s, spss, spds


if __name__ == "__main__":
    main()