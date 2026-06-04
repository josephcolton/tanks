#!/usr/bin/python3
"""
Interactive AI Tanks client with a curses TUI.

Usage:
    python tank_client_interactive.py NAME HOST [PORT]

Example:
    python tank_client_interactive.py Alice 127.0.0.1 1234

Windows users: pip install windows-curses

Commands available at the prompt:
    info map            ask the server for the field dimensions
    info players        ask who is connected (name list)
    info facing         ask which direction you are facing
    info coordinates    ask for your own tile position (x y)
    whoami              ask the server for your own name
    status              ask for your own health
    status NAME         ask for another player's health
    wait                do nothing this turn (accumulates toward repair)
    turn right      rotate 90 degrees clockwise
    turn left       rotate 90 degrees counter-clockwise
    move            move one tile in the current facing direction
    shoot           fire in the current facing direction
    scan wide       3x3 area scan centred on your tank
    scan far        scan up to 9 tiles in your facing direction
    scan extended   5x5 area scan centred on your tank
    help [topic]    command list, or detail for a command (e.g. help scan)
    disconnect      tell the server you are leaving
    quit            send disconnect and exit this client

Scan replies are shown as a string of UPPERCASE tile symbols, read left-to-
right, top-to-bottom (north-up regardless of which way you face):

    S = your own tank (centre of a wide / extended scan)
    . = empty tile
    T = an alive (enemy) tank
    D = a burned-out, dead tank — an obstacle
    X = a wall / the edge of the field

Commands you type are case-insensitive, but these scan symbols are not — they
always come back uppercase.
"""

import socket
import sys
import threading

try:
    import curses
except ImportError:
    sys.exit(
        "The 'curses' module is not available.\n"
        "On Windows run:  pip install windows-curses"
    )

from communication import build_message, parse_message, MAX_PACKET_SIZE

MAX_HISTORY = 30   # lines kept in the scrollback log

# Curses color-pair indices
_PAIR_SERVER = 1   # cyan  — messages from the server
_PAIR_CLIENT = 2   # green — commands sent by the player
_PAIR_BORDER = 3   # white dim — separator line and status bar

# Text shown by the local "help" command (handled by the client, not the server)
_HELP_LINES = [
    "Commands (type at the > prompt):",
    "  info map         field dimensions",
    "  info players     list of connected players",
    "  info facing      which direction you are facing",
    "  info coordinates your tile position (x y)",
    "  whoami           your own registered name",
    "  status [NAME]    health of yourself (or NAME)",
    "  wait             do nothing (accumulates toward repair)",
    "  turn right|left  rotate 90 degrees",
    "  move             move one tile in the facing direction",
    "  shoot            fire in the facing direction",
    "  scan wide        3x3 area scan around you",
    "  scan far         up to 9 tiles ahead",
    "  scan extended    5x5 area scan around you",
    "  help [topic]     this list, or detail for a command (e.g. help scan)",
    "  disconnect       tell the server you are leaving",
    "  quit             disconnect and exit",
]

# Detailed help for individual commands: "help <topic>" shows these. Handled
# entirely by the client — nothing is sent to the server.
_HELP_TOPICS = {
    "turn": [
        "turn — rotate the tank 90 degrees:",
        "  turn right   rotate clockwise",
        "  turn left    rotate counter-clockwise",
    ],
    "scan": [
        "scan — look at the field (you do not move):",
        "  scan wide       3x3 area around you (9 characters)",
        "  scan far        up to 9 tiles ahead in your facing direction",
        "  scan extended   5x5 area around you (25 characters)",
    ],
    "info": [
        "info — ask the server for game information:",
        "  info map          field width and height",
        "  info players      list of connected player names",
        "  info facing       the direction you are facing (north/east/south/west)",
        "  info coordinates  your tile position as 'coordinates X Y'",
    ],
    "status": [
        "status — ask for current health:",
        "  status          your own health",
        "  status NAME     another player's health",
    ],
    "move": [
        "move — move one tile in the direction you are facing.",
        "  Crashing into a wall or any tank costs 1 health.",
    ],
    "shoot": [
        "shoot — fire in the direction you are facing.",
    ],
    "wait": [
        "wait — do nothing this turn; accumulates toward health repair.",
    ],
}


# ---------------------------------------------------------------------------
# Client state (network + message history)
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, name: str, host: str, port: int):
        self.name        = name
        self.server_addr = (host, port)
        self._sock       = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)     # recv times out so the thread can stop cleanly

        self._messages: list = []      # [(source, text), ...]  source = "SERVER"|"CLIENT"
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._recv_loop, daemon=True)

    # ---- public API -------------------------------------------------------

    def start(self):
        self._thread.start()
        self._log("CLIENT", f"connect {self.name}")
        self._sock.sendto(build_message("connect", self.name), self.server_addr)

    def send(self, line: str):
        self._log("CLIENT", line)
        try:
            self._sock.sendto(line.encode("utf-8"), self.server_addr)
        except OSError:
            pass

    def disconnect(self):
        self._log("CLIENT", "disconnect")
        try:
            self._sock.sendto(build_message("disconnect"), self.server_addr)
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def messages(self) -> list:
        """Return a snapshot of the message history."""
        with self._lock:
            return list(self._messages)

    def show_help(self, topic=None):
        """Print help locally — does NOT contact the server.

        With no topic, show the full command list. With a topic (e.g. "scan"),
        show that command's options.
        """
        if topic is None:
            lines = _HELP_LINES
        elif topic in _HELP_TOPICS:
            lines = _HELP_TOPICS[topic]
        else:
            lines = [f"No help for '{topic}'. Topics: " + ", ".join(sorted(_HELP_TOPICS))]
        for line in lines:
            self._log("HELP", line)

    # ---- internals --------------------------------------------------------

    def _log(self, source: str, text: str):
        with self._lock:
            self._messages.append((source, text))
            if len(self._messages) > MAX_HISTORY:
                self._messages.pop(0)

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(MAX_PACKET_SIZE)
                cmd, args = parse_message(data)
                full = (cmd + (" " + " ".join(args) if args else "")).strip()
                self._log("SERVER", full)
            except socket.timeout:
                continue
            except OSError:
                break


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------

def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_PAIR_SERVER, curses.COLOR_CYAN,  -1)
    curses.init_pair(_PAIR_CLIENT, curses.COLOR_GREEN, -1)
    curses.init_pair(_PAIR_BORDER, curses.COLOR_WHITE, -1)


def _draw(stdscr, client: _Client, input_buf: list, status: str):
    """Redraw the entire screen."""
    h, w = stdscr.getmaxyx()
    log_h = h - 3   # rows available for the message log

    stdscr.erase()

    # ---- status bar (top row) ----
    bar = f" AI Tanks  {status} "
    try:
        stdscr.addstr(0, 0, bar[:w - 1],
                      curses.color_pair(_PAIR_BORDER) | curses.A_REVERSE)
    except curses.error:
        pass

    # ---- message log ----
    msgs = client.messages()
    visible = msgs[-log_h:]
    for i, (source, text) in enumerate(visible):
        row = 1 + i
        if row >= h - 2:
            break
        if source == "SERVER":
            label = "SERVER: "
            attr  = curses.color_pair(_PAIR_SERVER) | curses.A_BOLD
        elif source == "HELP":
            label = ""
            attr  = curses.color_pair(_PAIR_BORDER)
        else:
            label = "CLIENT: "
            attr  = curses.color_pair(_PAIR_CLIENT)
        try:
            stdscr.addstr(row, 0, label, attr)
            stdscr.addstr(row, len(label), text[: w - len(label) - 1])
        except curses.error:
            pass

    # ---- separator ----
    try:
        stdscr.addstr(h - 2, 0, "-" * (w - 1),
                      curses.color_pair(_PAIR_BORDER) | curses.A_DIM)
    except curses.error:
        pass

    # ---- input line ----
    prompt     = "> "
    input_str  = "".join(input_buf)
    visible_in = input_str[: w - len(prompt) - 1]
    try:
        stdscr.addstr(h - 1, 0, prompt, curses.A_BOLD)
        stdscr.addstr(h - 1, len(prompt), visible_in)
        stdscr.move(h - 1, min(len(prompt) + len(input_str), w - 1))
    except curses.error:
        pass

    stdscr.refresh()


def _run_ui(stdscr, client: _Client):
    _init_colors()
    stdscr.keypad(True)
    curses.halfdelay(1)     # getch() times out after 0.1 s → 10 Hz redraw

    status    = f"connected as {client.name} to {client.server_addr[0]}:{client.server_addr[1]}"
    input_buf = []

    while True:
        _draw(stdscr, client, input_buf, status)

        try:
            key = stdscr.getch()
        except curses.error:
            continue

        if key == curses.ERR:           # timeout — just redraw
            continue

        elif key == curses.KEY_RESIZE:  # terminal resized
            curses.update_lines_cols()

        elif key in (curses.KEY_ENTER, 10, 13):   # Enter — submit command
            line = "".join(input_buf).strip()
            input_buf.clear()
            if not line:
                continue
            if line.lower() == "quit":
                client.disconnect()
                break
            words = line.split()
            if words[0].lower() == "help":          # handled locally, never sent
                client.show_help(words[1].lower() if len(words) > 1 else None)
                continue
            client.send(line)

        elif key in (curses.KEY_BACKSPACE, 127, 8):  # Backspace
            if input_buf:
                input_buf.pop()

        elif key == 27:                 # ESC — disconnect and exit
            client.disconnect()
            break

        elif 32 <= key < 127:           # printable ASCII
            input_buf.append(chr(key))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _usage():
    print("Usage: python tank_client_interactive.py NAME HOST [PORT]")
    print()
    print("  NAME  your tank's display name (max 16 characters)")
    print("  HOST  server hostname or IP address")
    print("  PORT  server UDP port (default: 1234)")
    print()
    print("Commands available at the prompt:")
    print("  info map          ask the server for the field dimensions")
    print("  info players      ask who is connected (name list)")
    print("  info facing       ask which direction you are facing")
    print("  info coordinates  ask for your own tile position (x y)")
    print("  whoami            ask the server for your own name")
    print("  status            ask for your own health")
    print("  status NAME       ask for another player's health")
    print("  wait              do nothing this turn (accumulates toward repair)")
    print("  turn right      rotate 90 degrees clockwise")
    print("  turn left       rotate 90 degrees counter-clockwise")
    print("  move            move one tile in the current facing direction")
    print("  shoot           fire in the current facing direction")
    print("  scan wide       3x3 area scan centred on your tank")
    print("  scan far        scan up to 9 tiles in your facing direction")
    print("  scan extended   5x5 area scan centred on your tank")
    print("  help [topic]    command list, or detail for a command (e.g. help scan)")
    print("  disconnect      tell the server you are leaving")
    print("  quit            send disconnect and exit this client")


def main():
    if len(sys.argv) < 3:
        _usage()
        sys.exit(1)

    name = sys.argv[1]
    host = sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 1234

    client = _Client(name, host, port)
    client.start()
    try:
        curses.wrapper(_run_ui, client)
    finally:
        client.stop()


if __name__ == "__main__":
    main()
