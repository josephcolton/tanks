"""
Example autonomous AI Tanks client — a simple "hunter" bot.

This is a starting point for students: it plays a complete game but keeps its
logic deliberately small so you can extend it. It uses only the WIDE scan
(the 3x3 area around the tank) — no far scan and no extended scan.

To avoid driving into walls, the bot remembers which way it is facing, so it
knows which of the scanned tiles is directly in front of it. It learns its
starting direction from the server with the `info facing` command, then keeps
that up to date as it turns.

Each turn it:
    1. Scans the 3x3 area around itself.
    2. If an enemy tank is directly ahead, SHOOT it.
    3. If an enemy is elsewhere nearby, TURN to try to line up a shot.
    4. Otherwise MOVE forward (only when the tile ahead is empty, so it never
       crashes into a wall) or TURN at random to explore.

Usage:
    python tank_client_ai.py NAME HOST [PORT]

Example:
    python tank_client_ai.py Hunter 127.0.0.1 1234

Press Ctrl-C to disconnect and quit.
"""

import random
import socket
import sys
import time

from communication import (
    build_message, parse_message, MAX_PACKET_SIZE,
    CMD_CONNECT, CMD_DISCONNECT, CMD_INFO, CMD_SCAN, CMD_MOVE, CMD_TURN, CMD_SHOOT,
    ARG_WIDE, ARG_FACING, ARG_LEFT, ARG_RIGHT, ARG_STARTED, ARG_ENDED,
    RESP_WELCOME, RESP_GAME, RESP_YOU, RESP_WIDESCAN, RESP_FACING, RESP_ERROR,
    YOU_DIED, YOU_GOT_SHOT,
    SCAN_TANK, SCAN_WALL, SCAN_EMPTY,
)
from tank_player import Direction

# Seconds to wait for a reply to a single command before giving up.
REPLY_TIMEOUT = 1.0
# Every command (scan, move, turn, shoot) now puts the tank on a cooldown.
# Pause a hair longer than the longest cooldown we use so the next command is
# never rejected with "on cooldown". (Raise this if you increase the server's
# move_cooldown or scan_cooldown.)
ACTION_PAUSE = 1.10


class AITank:
    def __init__(self, name, host, port):
        self.name = name
        self.server_addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)

        self.game_running = False
        self.game_over = False
        self.alive = True

        # The direction we are facing, tracked so we know which scanned tile is
        # in front of us. None until we work it out from the starting walls.
        self.facing = None

    # ------------------------------------------------------------------
    # Low-level networking
    # ------------------------------------------------------------------

    def _send(self, *parts):
        self._sock.sendto(build_message(*parts), self.server_addr)

    def _handle_async(self, cmd, args):
        """Update bot state from any message the server sends us."""
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
        elif cmd == RESP_ERROR:
            print(f"[server] error {' '.join(args)}")

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
    # Commands  (each one waits out its cooldown afterward)
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

    def scan_wide(self):
        """Scan the 3x3 area and return the 9-character result (or None)."""
        self._send(CMD_SCAN, ARG_WIDE)
        reply = self._request({RESP_WIDESCAN})
        time.sleep(ACTION_PAUSE)          # scanning is on a cooldown too
        if reply and reply[1]:
            return reply[1][0]
        return None

    def _action(self, *parts):
        """Send an action command, wait for its reply, then honour cooldown."""
        self._send(*parts)
        self._request({RESP_YOU, RESP_ERROR})
        time.sleep(ACTION_PAUSE)

    def shoot(self):
        print("  -> shoot")
        self._action(CMD_SHOOT)

    def move(self):
        print("  -> move")
        self._action(CMD_MOVE)

    def turn(self, side):
        print(f"  -> turn {side}")
        self._action(CMD_TURN, side)
        # Keep our facing in step with the turn we just made.
        if self.facing is not None:
            self.facing = (self.facing.turn_right() if side == ARG_RIGHT
                           else self.facing.turn_left())

    # ------------------------------------------------------------------
    # Reading the wide scan
    # ------------------------------------------------------------------
    #
    # The 3x3 wide scan is a 9-character string in reading order, always with
    # north pointing up regardless of which way we face:
    #
    #     index:  0 1 2      offsets: NW N NE
    #             3 4 5               W  .  E      (4 is 'S', our own tile)
    #             6 7 8               SW S SE
    #
    @staticmethod
    def _cell(direction):
        """Wide-scan string index of the tile one step in `direction`."""
        dx, dy = direction.delta()
        return (dy + 1) * 3 + (dx + 1)     # N=1, E=5, S=7, W=3

    def query_facing(self):
        """Ask the server which way we are pointing (info has no cooldown)."""
        self._send(CMD_INFO, ARG_FACING)
        reply = self._request({RESP_FACING})
        if reply and reply[1]:
            try:
                self.facing = Direction[reply[1][0].upper()]   # e.g. "north" → NORTH
                print(f"  [facing: {self.facing}]")
            except KeyError:
                pass

    def _safe_turns(self, wide):
        """Turn directions (left/right) that would not leave us facing a wall."""
        if self.facing is None:
            return [ARG_LEFT, ARG_RIGHT]
        safe = []
        for side in (ARG_LEFT, ARG_RIGHT):
            new_facing = (self.facing.turn_left() if side == ARG_LEFT
                          else self.facing.turn_right())
            if not wide or wide[self._cell(new_facing)] != SCAN_WALL:
                safe.append(side)
        return safe

    def _pick_turn(self, wide, prefer=None):
        """Choose a turn that does not face a wall (prefer one side if it's safe)."""
        safe = self._safe_turns(wide)
        if prefer is not None and prefer in safe:
            return prefer
        if safe:
            return random.choice(safe)
        return prefer or random.choice([ARG_LEFT, ARG_RIGHT])   # boxed in

    # ------------------------------------------------------------------
    # Per-turn decision
    # ------------------------------------------------------------------

    def take_turn(self):
        if self.facing is None:
            self.query_facing()       # learn our orientation before acting

        wide = self.scan_wide()

        # The tile directly in front of us, according to our facing.
        ahead = wide[self._cell(self.facing)] if (wide and self.facing) else None

        # 1. Enemy right in front → shoot it.
        if ahead == SCAN_TANK:
            self.shoot()
            return

        # 2. Enemy somewhere else nearby → turn to try to bring it in front
        #    (without turning to face a wall).
        if wide and SCAN_TANK in wide:
            print("  enemy nearby — turning to aim")
            self.turn(self._pick_turn(wide, prefer=ARG_RIGHT))
            return

        # 3. Nothing to shoot: move or turn at random — but only move when the
        #    tile ahead is empty (never crash into a wall), and never turn to
        #    face a wall either.
        if ahead == SCAN_EMPTY and random.random() < 0.5:
            self.move()
        else:
            self.turn(self._pick_turn(wide))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        if not self.connect():
            return

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
    print("Usage: python tank_client_ai.py NAME HOST [PORT]")
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

    AITank(name, host, port).run()


if __name__ == "__main__":
    main()
