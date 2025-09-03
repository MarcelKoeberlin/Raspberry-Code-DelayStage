import serial


def open_esp301_serial(port: str = "/dev/ttyUSB0", baud: int = 19200) -> serial.Serial:
    """
    Opens and returns a configured serial connection to the ESP301.

    :param port: Serial port path (e.g. "/dev/ttyUSB0").
    :param baud: Baud rate (default 19200).
    :return: Configured serial.Serial object.
    """
    return serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2
    )


def send_command(ser: serial.Serial, cmd: str) -> str:
    """
    Sends a command to the ESP301 and reads the response.

    :param ser: Serial connection to the ESP301.
    :param cmd: Command string to send (without carriage return).
    :return: Response string from ESP301.
    """
    ser.write((cmd + "\r").encode())
    return ser.readline().decode().strip()


def move_relative(ser: serial.Serial, axis: int, offset_mm: float):
    """
    Moves the stage by a relative distance in mm and waits for completion.

    :param ser: Serial connection to the ESP301.
    :param axis: Axis number to move (e.g., 1).
    :param offset_mm: Distance to move relative to current position (in mm).
    """
    current_pos = float(send_command(ser, f"{axis}TP"))
    print(f"Current position: {current_pos:.4f} mm")

    print(f"Moving by {offset_mm:.4f} mm ...", end="", flush=True)
    send_command(ser, f"{axis}PR{offset_mm:.4f}")
    send_command(ser, f"{axis}WS1")
    print(" done.")

    new_pos = float(send_command(ser, f"{axis}TP"))
    print(f"New position: {new_pos:.4f} mm")


def move_absolute(ser: serial.Serial, axis: int, target_pos_mm: float):
    """
    Moves the stage to an absolute position and waits for completion.

    :param ser: Serial connection to the ESP301.
    :param axis: Axis number to move (e.g., 1).
    :param target_pos_mm: Absolute position to move to (in mm).
    """
    current_pos = float(send_command(ser, f"{axis}TP"))
    print(f"Current position: {current_pos:.4f} mm")

    print(f"Moving to {target_pos_mm:.4f} mm ...", end="", flush=True)
    send_command(ser, f"{axis}PA{target_pos_mm:.4f}")
    send_command(ser, f"{axis}WS1")
    print(" done.")

    new_pos = float(send_command(ser, f"{axis}TP"))
    print(f"New position: {new_pos:.4f} mm")


def main():
    ser = open_esp301_serial()
    try:
        move_relative(ser, axis=1, offset_mm=0.5)
        move_absolute(ser, axis=1, target_pos_mm=6.0)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
