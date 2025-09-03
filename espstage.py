import serial


class ESPStage:
    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 19200, axis: int = 1):
        self.axis = axis
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2
        )

    def send_command(self, cmd: str) -> str:
        self.ser.write((cmd + "\r").encode())
        return self.ser.readline().decode().strip()

    def get_position(self) -> float:
        return float(self.send_command(f"{self.axis}TP"))

    def move_relative(self, offset_mm: float):
        current_pos = self.get_position()
        print(f"Current position: {current_pos:.4f} mm")

        print(f"Moving by {offset_mm:.4f} mm ...", end="", flush=True)
        self.send_command(f"{self.axis}PR{offset_mm:.4f}")
        self.send_command(f"{self.axis}WS1")
        print(" done.")

        new_pos = self.get_position()
        print(f"New position: {new_pos:.4f} mm")

    def close(self):
        self.ser.close()