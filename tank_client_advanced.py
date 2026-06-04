#!/usr/bin/python3
"""
Advanced autonomous AI Tanks client — a map-building "hunter-killer" bot.

This is a more capable example than ``tank_client_ai.py``. Where the basic bot
only ever looks at the 3x3 tiles around itself and reacts, this one:

    * Builds an INTERNAL MAP of the whole battlefield as it explores, using
      absolute tile coordinates it gets from the server (``info coordinates``
      and ``info map``).
    * Uses ALL THREE scans — ``scan wide`` (3x3) for close awareness,
      ``scan extended`` (5x5) every few turns for the bigger picture, and
      ``scan far`` to confirm a clear line of fire before shooting.
    * REMEMBERS where it last saw enemies (with a timestamp) so it can keep
      hunting a target that has slipped out of view.
    * PATHFINDS with breadth-first search across its internal map — both to
      close in on an enemy and to steer toward unexplored ground — instead of
      wandering randomly.
    * HUNTS: it manoeuvres into a firing line with an enemy, then shoots.
    * REPAIRS: when no enemy has been seen for a while and it has taken damage,
      it parks and ``wait``s to heal (the game restores 1 HP every
      ``repair_waits`` waits), re-checking each turn that it is still safe.

All three scans report tiles in row-major (reading) order, north-up regardless
of facing, using these UPPERCASE symbols (matched against the SCAN_* constants
in communication.py):

    S = your own tank (the centre of a wide / extended scan)
    . = empty tile
    T = an alive (enemy) tank
    D = a burned-out, dead tank — a permanent obstacle
    X = a wall / the edge of the field

``scan wide`` is a 9-char 3x3 (self at index 4), ``scan extended`` a 25-char 5x5
(self at index 12), and ``scan far`` the 1-9 tiles directly ahead (the ray stops
at the first wall, space-padded to 9). The symbols are case-sensitive, so we
fold scans into the map as-is — never lowercase a scan result, or ``T``/``X``/
``D`` would stop matching SCAN_TANK / SCAN_WALL / SCAN_DEAD.

It is still written to be read by students: every section is commented, and the
decision logic in ``decide`` reads like a priority list. Compare it side by side
with ``tank_client_ai.py`` to see what each new capability buys you.

Usage:
    python tank_client_advanced.py NAME HOST [PORT]

Example:
    python tank_client_advanced.py Seeker 127.0.0.1 1234

Press Ctrl-C to disconnect and quit.
"""

import random
import socket
import sys
import time
from collections import deque

from communication import (
    build_message, parse_message, MAX_PACKET_SIZE,
    CMD_CONNECT, CMD_DISCONNECT, CMD_INFO, CMD_STATUS,
    CMD_SCAN, CMD_MOVE, CMD_TURN, CMD_SHOOT, CMD_WAIT,
    ARG_WIDE, ARG_FAR, ARG_EXTENDED, ARG_FACING, ARG_COORDINATES, ARG_MAP,
    ARG_LEFT, ARG_RIGHT, ARG_STARTED, ARG_ENDED,
    RESP_WELCOME, RESP_GAME, RESP_YOU, RESP_ERROR,
    RESP_WIDESCAN, RESP_FARSCAN, RESP_EXTENDEDSCAN,
    RESP_FACING, RESP_COORDINATES, RESP_MAP, RESP_PLAYER,
    YOU_DIED, YOU_GOT_SHOT, YOU_GOT_HIT, YOU_REPAIRED,
    SCAN_TANK, SCAN_WALL, SCAN_EMPTY, SCAN_DEAD, SCAN_SELF,
)
from tank_player import Direction

# Seconds to wait for a reply to a single command before giving up.
REPLY_TIMEOUT = 1.0
# Every gameplay command (scan, move, turn, shoot, wait) puts the tank on a
# cooldown. Pause a hair longer than the longest cooldown so the next command
# is never rejected with "on cooldown". (Raise this if you increase the
# server's move_cooldown / scan_cooldown / extended_cooldown.)
ACTION_PAUSE = 1.10

# How long (seconds) to keep chasing an enemy after the last time we saw it.
# Enemies move, so an old sighting is only a hint, not a certainty.
ENEMY_MEMORY = 8.0
# Do a 5x5 extended scan (instead of a 3x3 wide scan) every Nth turn, to
# periodically refresh the wider picture without paying its longer cooldown
# every single turn.
EXTENDED_EVERY = 4
# Re-check our own health (free, no cooldown) every Nth turn.
STATUS_EVERY = 3

# Map an (dx, dy) step back to the Direction that produces it.
_DELTA_TO_DIR = {d.delta(): d for d in Direction}


class AdvancedTank:
    def __init__(self, name, host, port):
        self.name = name
        self.server_addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)

        self.game_running = False
        self.game_over = False
        self.alive = True

        # --- What we know about the world -------------------------------
        self.width = 0          # battlefield size in tiles (from `info map`)
        self.height = 0
        self.x = 0              # our absolute tile position (from the server)
        self.y = 0
        self.facing = None      # our Direction (from `info facing`)

        # Our internal map. terrain[(x, y)] holds one of SCAN_EMPTY / SCAN_WALL
        # / SCAN_DEAD; a tile we have never seen is simply absent (UNKNOWN).
        self.terrain = {}
        # Last-seen enemy tiles → the time.monotonic() timestamp we saw them.
        self.enemies = {}

        # --- What we know about ourselves -------------------------------
        self.hp = None          # current health (from `status`)
        self.max_hp = None      # the most health we have ever observed

        self.turn_count = 0

    # ------------------------------------------------------------------
    # Low-level networking (same shape as the basic example)
    # ------------------------------------------------------------------

    def _send(self, *parts):
        self._sock.sendto(build_message(*parts), self.server_addr)

    def _handle_async(self, cmd, args):
        """Update bot state from any message the server sends us, any time."""
        if cmd == RESP_GAME and args:
            if args[0] == ARG_STARTED:
                self.game_running = True
                print("[server] game started")
            elif args[0] == ARG_ENDED:
                self.game_running = False
                self.game_over = True
                print("[server] game ended")
        elif cmd == RESP_YOU and args:
            outcome = " ".join(args)
            if outcome == YOU_DIED:
                self.alive = False
                print("[server] you died")
            elif outcome == YOU_GOT_SHOT:
                print("[server] you got shot!")
                self._adjust_hp(-1)
            elif outcome == YOU_GOT_HIT:
                self._adjust_hp(-1)
            elif outcome == YOU_REPAIRED:
                self._adjust_hp(+1)
                print("[server] repaired 1 HP")
        elif cmd == RESP_ERROR:
            print(f"[server] error {' '.join(args)}")

    def _adjust_hp(self, delta):
        if self.hp is not None:
            self.hp = max(0, self.hp + delta)
            if self.max_hp is not None:
                self.max_hp = max(self.max_hp, self.hp)

    def _request(self, prefixes, timeout=REPLY_TIMEOUT):
        """
        Read packets until one matches a wanted response, or until timeout.
        Every packet updates bot state along the way.
        Returns (cmd, args) of the matching reply, or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = self._sock.recvfrom(MAX_PACKET_SIZE)
            except socket.timeout:
                continue
            except OSError:
                return None
            cmd, args = parse_message(data)
            self._handle_async(cmd, args)
            if cmd in prefixes:
                return cmd, args
        return None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self):
        self._send(CMD_CONNECT, self.name)
        if self._request({RESP_WELCOME}, timeout=2.0) is None:
            print("No welcome from server — is it running?")
            return False
        print(f"Connected as {self.name}.")
        return True

    def disconnect(self):
        try:
            self._send(CMD_DISCONNECT)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Free "info" queries (these have NO cooldown, so we can call them
    # liberally to keep our map perfectly aligned with reality).
    # ------------------------------------------------------------------

    def query_map(self):
        self._send(CMD_INFO, ARG_MAP)
        reply = self._request({RESP_MAP})
        if reply and len(reply[1]) >= 2:
            self.width = int(reply[1][0])
            self.height = int(reply[1][1])
            print(f"  [map: {self.width} x {self.height}]")

    def query_facing(self):
        self._send(CMD_INFO, ARG_FACING)
        reply = self._request({RESP_FACING})
        if reply and reply[1]:
            try:
                self.facing = Direction[reply[1][0].upper()]
            except KeyError:
                pass

    def query_coordinates(self):
        self._send(CMD_INFO, ARG_COORDINATES)
        reply = self._request({RESP_COORDINATES})
        if reply and len(reply[1]) >= 2:
            try:
                self.x = int(reply[1][0])
                self.y = int(reply[1][1])
            except ValueError:
                pass

    def query_status(self):
        """Read our own current health (reply: 'player NAME health is X')."""
        self._send(CMD_STATUS)
        reply = self._request({RESP_PLAYER})
        if reply and reply[1]:
            try:
                self.hp = int(reply[1][-1])           # ...health is X
                self.max_hp = max(self.max_hp or 0, self.hp)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Gameplay commands (each one honours its cooldown afterward)
    # ------------------------------------------------------------------

    def _action(self, *parts):
        """Send an action command, read its first reply, then wait the cooldown."""
        self._send(*parts)
        reply = self._request({RESP_YOU, RESP_ERROR})
        time.sleep(ACTION_PAUSE)
        return reply

    def shoot(self):
        print("  -> shoot")
        self._action(CMD_SHOOT)

    def move(self):
        print("  -> move")
        reply = self._action(CMD_MOVE)
        # Update our position only if the server confirms we actually moved.
        if reply and reply[0] == RESP_YOU and " ".join(reply[1]) == "moved":
            dx, dy = self.facing.delta()
            self.x += dx
            self.y += dy

    def turn(self, side):
        print(f"  -> turn {side}")
        self._action(CMD_TURN, side)
        if self.facing is not None:
            self.facing = (self.facing.turn_right() if side == ARG_RIGHT
                           else self.facing.turn_left())

    def wait(self):
        print("  -> wait (repairing)")
        self._action(CMD_WAIT)

    # ------------------------------------------------------------------
    # Sensing — run a scan and fold what it sees into the internal map
    # ------------------------------------------------------------------

    def _set_tile(self, pos, symbol):
        """Record one scanned tile into terrain / enemy memory."""
        if symbol in (SCAN_EMPTY, SCAN_SELF):
            self.terrain[pos] = SCAN_EMPTY
            self.enemies.pop(pos, None)
        elif symbol == SCAN_WALL:
            self.terrain[pos] = SCAN_WALL
            self.enemies.pop(pos, None)
        elif symbol == SCAN_DEAD:
            self.terrain[pos] = SCAN_DEAD          # wreck — a permanent obstacle
            self.enemies.pop(pos, None)
        elif symbol == SCAN_TANK:
            self.terrain[pos] = SCAN_EMPTY         # a tank sits on passable ground
            self.enemies[pos] = time.monotonic()   # remember the live enemy here
        # A space (far-scan padding) or anything else leaves the tile UNKNOWN.

    def _ingest_centered(self, grid, half):
        """
        Fold a north-up square scan centred on us into the map.
        `half` is 1 for a 3x3 wide scan, 2 for a 5x5 extended scan.
        """
        side = 2 * half + 1
        if not grid or len(grid) < side * side:
            return
        for i, symbol in enumerate(grid):
            col, row = i % side, i // side
            pos = (self.x + (col - half), self.y + (row - half))
            self._set_tile(pos, symbol)

    def _ingest_far(self, grid):
        """Fold a far scan (the tiles directly ahead) into the map."""
        if not grid or self.facing is None:
            return
        dx, dy = self.facing.delta()
        found_enemy = False
        for step, symbol in enumerate(grid, start=1):
            if symbol == " ":
                break                              # padding past where the ray stopped
            pos = (self.x + dx * step, self.y + dy * step)
            self._set_tile(pos, symbol)
            if symbol == SCAN_TANK:
                found_enemy = True
                break                              # first live tank is our shot
            if symbol == SCAN_WALL:
                break                              # the ray (and our shot) stop here
        return found_enemy

    def scan_wide(self):
        self._send(CMD_SCAN, ARG_WIDE)
        reply = self._request({RESP_WIDESCAN})
        time.sleep(ACTION_PAUSE)
        if reply and reply[1]:
            self._ingest_centered(reply[1][0], half=1)

    def scan_extended(self):
        self._send(CMD_SCAN, ARG_EXTENDED)
        reply = self._request({RESP_EXTENDEDSCAN})
        time.sleep(ACTION_PAUSE)
        if reply and reply[1]:
            self._ingest_centered(reply[1][0], half=2)

    def scan_far(self):
        """Scan ahead; returns True if a live enemy is in our line of fire."""
        self._send(CMD_SCAN, ARG_FAR)
        reply = self._request({RESP_FARSCAN})
        time.sleep(ACTION_PAUSE)
        # The far-scan string is space-padded; parse_message splits on the
        # padding, so we only get (and only need) the leading characters.
        grid = reply[1][0] if (reply and reply[1]) else ""
        return bool(self._ingest_far(grid))

    # ------------------------------------------------------------------
    # Map reasoning helpers
    # ------------------------------------------------------------------

    def _in_bounds(self, pos):
        x, y = pos
        return 0 <= x < self.width and 0 <= y < self.height

    def _passable(self, pos):
        """Can a tank stand on / drive through this tile? (Unknown = optimistic.)"""
        if not self._in_bounds(pos):
            return False
        if self.terrain.get(pos) in (SCAN_WALL, SCAN_DEAD):
            return False
        if pos in self.enemies:           # don't try to drive through a live tank
            return False
        return True

    def _prune_enemies(self):
        """Forget enemy sightings that are too old to trust."""
        cutoff = time.monotonic() - ENEMY_MEMORY
        for pos in [p for p, t in self.enemies.items() if t < cutoff]:
            del self.enemies[pos]

    def _no_wall_between(self, a, b):
        """True if there is no wall on the straight line from a to b (exclusive)."""
        ax, ay = a
        bx, by = b
        sx = (bx > ax) - (bx < ax)        # sign of the step in x (-1, 0, or 1)
        sy = (by > ay) - (by < ay)
        cur = (ax + sx, ay + sy)
        while cur != b:
            if self.terrain.get(cur) == SCAN_WALL:
                return False
            cur = (cur[0] + sx, cur[1] + sy)
        return True

    def _enemy_in_firing_line(self):
        """Is a remembered enemy straight ahead of us with no wall in the way?"""
        if self.facing is None:
            return False
        dx, dy = self.facing.delta()
        x, y = self.x, self.y
        for _ in range(max(self.width, self.height)):
            x += dx
            y += dy
            if not self._in_bounds((x, y)):
                return False
            if self.terrain.get((x, y)) == SCAN_WALL:
                return False              # a wall stops our shot
            if (x, y) in self.enemies:
                return True               # a live enemy is in our sights
            # SCAN_DEAD does not stop a shot, so we keep looking past wrecks.
        return False

    def _nearest_enemy(self):
        if not self.enemies:
            return None
        return min(self.enemies,
                   key=lambda p: abs(p[0] - self.x) + abs(p[1] - self.y))

    def _is_firing_spot(self, pos, target):
        """A tile we could shoot `target` from: same row/col, no wall between."""
        if pos == target:
            return False
        if pos[0] != target[0] and pos[1] != target[1]:
            return False
        return self._no_wall_between(pos, target)

    # ------------------------------------------------------------------
    # Pathfinding — breadth-first search across the internal map
    # ------------------------------------------------------------------

    def _bfs(self, goal_test):
        """
        Find the shortest path of tiles from our position to the first tile
        that satisfies goal_test(pos). Returns the path as a list of tiles
        (starting with our own tile), or None if no goal is reachable.
        """
        start = (self.x, self.y)
        came_from = {start: None}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            if cur != start and goal_test(cur):
                # Walk the parent links back to the start, then reverse.
                path = [cur]
                while came_from[path[-1]] is not None:
                    path.append(came_from[path[-1]])
                return path[::-1]
            cx, cy = cur
            for nxt in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if nxt not in came_from and self._passable(nxt):
                    came_from[nxt] = cur
                    queue.append(nxt)
        return None

    def _step_along(self, path):
        """Take one move/turn that advances us along a planned path."""
        if not path or len(path) < 2:
            return False
        nxt = path[1]
        desired = _DELTA_TO_DIR.get((nxt[0] - self.x, nxt[1] - self.y))
        if desired is None:
            return False
        if self.facing == desired:
            # Facing the right way — only drive in if we have confirmed the
            # tile ahead is genuinely empty, so we never crash.
            if self.terrain.get(nxt) == SCAN_EMPTY and nxt not in self.enemies:
                self.move()
            else:
                # The planned tile is unexpectedly blocked (an enemy moved into
                # it, say) — sidestep safely and re-plan next turn.
                self.turn(self._safe_turn())
            return True
        self.turn(self._turn_side(desired))
        return True

    def _turn_side(self, desired):
        """Which way to turn (left/right) to face `desired` soonest."""
        diff = (desired.value - self.facing.value) % 360
        return ARG_LEFT if diff == 270 else ARG_RIGHT   # 90 or 180 → right

    # ------------------------------------------------------------------
    # Behaviours
    # ------------------------------------------------------------------

    def _hunt(self, target):
        """Manoeuvre toward a firing position on `target` (or toward it)."""
        # Already lined up but facing the wrong way? Turn to bring it into our
        # sights — next turn _enemy_in_firing_line() will trigger the shot.
        if self._is_firing_spot((self.x, self.y), target):
            dx = (target[0] > self.x) - (target[0] < self.x)
            dy = (target[1] > self.y) - (target[1] < self.y)
            desired = _DELTA_TO_DIR.get((dx, dy))
            if desired is not None and self.facing != desired:
                print("  lining up the shot")
                self.turn(self._turn_side(desired))
                return True

        # Otherwise pathfind: first try to reach any tile we could shoot from,
        # and failing that, just close the distance to a neighbouring tile.
        path = self._bfs(lambda p: self._is_firing_spot(p, target))
        if path is None:
            path = self._bfs(lambda p: abs(p[0] - target[0])
                                       + abs(p[1] - target[1]) == 1)
        if path and self._step_along(path):
            print(f"  hunting enemy at {target}")
            return True
        return False

    def _explore(self):
        """Head toward the nearest unexplored tile; wander if none is reachable."""
        path = self._bfs(lambda p: p not in self.terrain)
        if path and self._step_along(path):
            return
        # Nowhere new to reach — make a safe random move instead.
        ahead = self._tile_ahead()
        if self.terrain.get(ahead) == SCAN_EMPTY and random.random() < 0.6:
            self.move()
        else:
            self.turn(self._safe_turn())

    def _tile_ahead(self):
        dx, dy = self.facing.delta()
        return (self.x + dx, self.y + dy)

    def _safe_turn(self):
        """Pick a turn that does not leave us facing a known wall."""
        options = []
        for side in (ARG_LEFT, ARG_RIGHT):
            new_facing = (self.facing.turn_left() if side == ARG_LEFT
                          else self.facing.turn_right())
            dx, dy = new_facing.delta()
            if self.terrain.get((self.x + dx, self.y + dy)) != SCAN_WALL:
                options.append(side)
        return random.choice(options) if options else random.choice([ARG_LEFT, ARG_RIGHT])

    def _feels_safe(self):
        """No enemy seen recently — a good moment to stop and repair."""
        return not self.enemies

    def _is_damaged(self):
        return (self.hp is not None and self.max_hp is not None
                and self.hp < self.max_hp)

    # ------------------------------------------------------------------
    # Per-turn decision — read this top-to-bottom as a priority list
    # ------------------------------------------------------------------

    def decide(self):
        self._prune_enemies()

        # 1. An enemy is straight ahead → confirm with a far scan, then fire.
        if self._enemy_in_firing_line():
            if self.scan_far():
                self.shoot()
            else:
                # It moved out of the line as we looked; re-scan next turn.
                self.scan_wide()
            return

        # 2. We know where an enemy is → hunt it down.
        target = self._nearest_enemy()
        if target is not None and self._hunt(target):
            return

        # 3. Safe and wounded → hold position and repair.
        if self._is_damaged() and self._feels_safe():
            self.wait()
            return

        # 4. Nothing else to do → explore and fill in the map.
        self._explore()

    def take_turn(self):
        self.turn_count += 1

        # Re-sync the cheap, cooldown-free facts every turn so the internal map
        # never drifts from reality (a single dropped UDP packet otherwise
        # corrupts our coordinates forever).
        self.query_facing()
        self.query_coordinates()
        if self.hp is None or self.turn_count % STATUS_EVERY == 0:
            self.query_status()

        # Sense: a quick 3x3 most turns, a wider 5x5 sweep periodically.
        if self.turn_count % EXTENDED_EVERY == 0:
            self.scan_extended()
        else:
            self.scan_wide()

        self.decide()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        if not self.connect():
            return

        # Learn the battlefield size once up front so the map is sized right.
        self.query_map()

        print("Waiting for the game to start...")
        try:
            while True:
                if self.game_over:
                    print("Game ended — exiting.")
                    break
                if not self.game_running:
                    self._request({RESP_GAME}, timeout=1.0)
                    continue
                if not self.alive:
                    print("Tank destroyed — standing by until the game ends.")
                    self._request({RESP_GAME}, timeout=1.0)
                    continue
                self.take_turn()
        except KeyboardInterrupt:
            print("\nDisconnecting.")
        finally:
            self.disconnect()
            self._sock.close()


def _usage():
    print("Usage: python tank_client_advanced.py NAME HOST [PORT]")
    print()
    print("  NAME  your tank's display name (max 16 characters)")
    print("  HOST  server hostname or IP address")
    print("  PORT  server UDP port (default: 1234)")


def main():
    if len(sys.argv) < 3:
        _usage()
        sys.exit(1)

    name = sys.argv[1]
    host = sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 1234

    AdvancedTank(name, host, port).run()


if __name__ == "__main__":
    main()
