"""Two MAX7219 matrices on Firmata D6/D13/D10; no servo."""


class LedControl:
    def __init__(self, board):
        self.din = board.get_pin("d:6:o")
        self.clk = board.get_pin("d:13:o")
        self.cs = board.get_pin("d:10:o")
        self.rows = {}
        self.din_state = None
        self.clk.write(0)
        self.cs.write(1)
        for device in (0, 1):
            self.transfer(device, 15, 0)  # Display-test mode off
            self.shutdown(device, True)
            self.transfer(device, 11, 7)  # Scan eight rows
            self.transfer(device, 9, 0)   # No numeric decoding
            for row in range(8):
                self.set_row(device, row, 0)

    def transfer(self, device, opcode, value):
        self.cs.write(0)
        for target in (1, 0):
            for byte in ((opcode, value) if target == device else (0, 0)):
                for bit in range(7, -1, -1):
                    level = (byte >> bit) & 1
                    if level != self.din_state:
                        self.din.write(level)
                        self.din_state = level
                    self.clk.write(1)
                    self.clk.write(0)
        self.cs.write(1)

    def set_row(self, device, row, value):
        if self.rows.get((device, row)) != value:
            self.transfer(device, row + 1, value)
            self.rows[device, row] = value

    def shutdown(self, device, off):
        self.transfer(device, 12, 0 if off else 1)

    def set_intensity(self, device, value):
        self.transfer(device, 10, max(0, min(15, value)))

    def draw_pair(self, left, right=None):
        right = left if right is None else right
        for row in range(8):
            # Current mounting from user's sketch: left=1, right=0, no flips.
            self.set_row(1, row, left[row])
            self.set_row(0, row, right[row])
