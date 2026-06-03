import enum
import time


class Direction(enum.Enum):
    NORTH = 0
    EAST  = 90
    SOUTH = 180
    WEST  = 270

    def turn_right(self):
        return Direction((self.value + 90) % 360)

    def turn_left(self):
        return Direction((self.value - 90) % 360)

    def delta(self):
        """Return (dx, dy) for one step in this direction."""
        return {
            Direction.NORTH: ( 0, -1),
            Direction.EAST:  ( 1,  0),
            Direction.SOUTH: ( 0,  1),
            Direction.WEST:  (-1,  0),
        }[self]

    def __str__(self):
        return self.name.lower()


class Player:
    MAX_PLAYERS = 8

    # One distinct color per slot — indices 0-7 match slot numbers
    COLORS = [
        (220,  50,  50),   # 0  red
        ( 50, 100, 220),   # 1  blue
        ( 50, 180,  50),   # 2  green
        ( 20,  20,  20),   # 3  black
        (230, 230, 230),   # 4  white
        (230, 140,  30),   # 5  orange
        (140,  50, 180),   # 6  purple
        ( 30, 210, 210),   # 7  cyan
    ]
    COLOR_NAMES = ["red", "blue", "green", "black",
                   "white", "orange", "purple", "cyan"]

    def __init__(self, name, slot, address, x=0, y=0, direction=None,
                 health_starting=3, health_max=3):
        """
        name            : display name chosen by the player
        slot            : integer 0-7, determines color and starting position
        address         : (ip, port) tuple used to send UDP replies
        x, y            : starting tile coordinates on the grid
        direction       : starting Direction (defaults to NORTH)
        health_starting : HP the player begins the game with
        health_max      : HP ceiling (repairs cannot exceed this)
        """
        self.name       = name
        self.slot       = slot
        self.color      = self.COLORS[slot]
        self.color_name = self.COLOR_NAMES[slot]
        self.address    = address

        self.x         = x
        self.y         = y
        self.direction = direction if direction is not None else Direction.NORTH

        self.health_max = max(1, health_max)
        self.hit_points = max(1, min(health_starting, self.health_max))
        self.alive      = True
        self.connected  = True

        # Wait-based repair tracking
        self.wait_count = 0    # accumulated waits since last repair (or game start)

        # Action cooldown — monotonic timestamp when the player may act again
        self._cooldown_end = 0.0

    # ------------------------------------------------------------------
    # Cooldown
    # ------------------------------------------------------------------

    def is_on_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_end

    def set_cooldown(self, seconds: float = 1.0):
        self._cooldown_end = time.monotonic() + seconds

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def turn_right(self):
        self.direction = self.direction.turn_right()

    def turn_left(self):
        self.direction = self.direction.turn_left()

    def take_hit(self) -> int:
        """Reduce HP by 1. Returns remaining HP."""
        if self.hit_points > 0:
            self.hit_points -= 1
        if self.hit_points == 0:
            self.alive = False
        return self.hit_points

    def record_wait(self, repair_waits: int) -> bool:
        """
        Increment the wait counter. Every repair_waits accumulated waits,
        restore 1 HP (up to health_max). Returns True if a repair occurred.
        """
        if repair_waits <= 0:
            return False
        self.wait_count += 1
        if self.wait_count % repair_waits == 0 and self.hit_points < self.health_max:
            self.hit_points += 1
            return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __str__(self):
        return (f"Player({self.name!r}, {self.color_name}, "
                f"pos=({self.x},{self.y}), dir={self.direction}, "
                f"hp={self.hit_points}/{self.health_max})")

    def __repr__(self):
        return self.__str__()
