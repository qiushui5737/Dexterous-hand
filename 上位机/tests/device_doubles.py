"""In-memory actuator double for tests; never imported by the application."""


class FakeBus:
    def __init__(self, motors):
        self.positions = {sid: motor.zero_tick for sid, motor in motors.items()}

    def prepare(self, motors, model_number, *, stop_event=None):
        return self.positions.copy()

    def move(self, targets, *, stop_event=None):
        if stop_event is None or not stop_event.is_set():
            self.positions.update(targets)

    def status(self, sid):
        return {"id": sid, "position_tick": self.positions[sid]}
