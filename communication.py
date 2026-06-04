# ---------------------------------------------------------------------------
# Shared protocol definitions for AI Tanks.
# All messages are UTF-8 text, space-separated tokens, newline-terminated.
# ---------------------------------------------------------------------------

ENCODING       = "utf-8"
MAX_PACKET_SIZE = 4096

# ---- Client → Server commands ----
CMD_CONNECT    = "connect"
CMD_DISCONNECT = "disconnect"
CMD_INFO       = "info"
CMD_STATUS     = "status"    # status [NAME]  → player NAME health is X
CMD_WHOAMI     = "whoami"    # whoami         → name NAME
CMD_WAIT       = "wait"
CMD_TURN       = "turn"
CMD_SCAN       = "scan"
CMD_MOVE       = "move"
CMD_SHOOT      = "shoot"

# ---- Server → Client responses ----
RESP_WELCOME   = "welcome"
RESP_MAP       = "map"
RESP_PLAYERS   = "players"   # players name1,name2,name3
RESP_PLAYER    = "player"    # player NAME health is X
RESP_NAME      = "name"      # name NAME  (whoami reply)
RESP_FACING    = "facing"    # facing DIRECTION  (info facing reply)
RESP_COORDINATES = "coordinates"  # coordinates X Y  (info coordinates reply)
RESP_GAME      = "game"
RESP_YOU       = "you"
RESP_WIDESCAN  = "widescan"
RESP_FARSCAN   = "farscan"
RESP_EXTENDEDSCAN = "extendedscan"
RESP_ERROR     = "error"

# ---- Argument tokens ----
ARG_MAP        = "map"
ARG_PLAYERS    = "players"
ARG_FACING     = "facing"
ARG_COORDINATES = "coordinates"
ARG_RIGHT      = "right"
ARG_LEFT       = "left"
ARG_WIDE       = "wide"
ARG_FAR        = "far"
ARG_EXTENDED   = "extended"
ARG_STARTED    = "started"
ARG_ENDED      = "ended"

# ---- "you <outcome>" payloads ----
YOU_WAITED       = "waited"
YOU_TURNED_RIGHT = "turned right"
YOU_TURNED_LEFT  = "turned left"
YOU_SHOT         = "shot"
YOU_MOVED        = "moved"
YOU_GOT_SHOT     = "got shot"
YOU_GOT_HIT      = "got hit"     # crash damage (wall or tank collision)
YOU_CRASHED      = "crashed"
YOU_DIED         = "died"
YOU_REPAIRED     = "repaired"

# ---- Scan tile symbols ----
SCAN_EMPTY = "."   # passable, unoccupied
SCAN_TANK  = "T"   # alive tank
SCAN_DEAD  = "D"   # burned-out (dead) tank — blocks movement, not shots
SCAN_WALL  = "X"   # out of bounds / wall
SCAN_SELF  = "S"   # the scanning tank's own tile (wide / extended scan center)
SCAN_WIDTH = 9     # wide and far scan responses are always 9 characters wide
SCAN_EXTENDED_WIDTH = 25   # extended scan is a 5x5 grid → 25 characters


def build_message(*parts) -> bytes:
    """Join parts into a space-separated UTF-8 message."""
    return " ".join(str(p) for p in parts).encode(ENCODING)


def parse_message(data: bytes) -> tuple:
    """
    Decode a raw packet into (command, args).

    The command (the first token) is lowercased so command matching is
    case-insensitive. The args are returned in their ORIGINAL case: scan
    results carry meaningful uppercase symbols (T, X, D, S), so lowercasing
    them would make tanks and walls unrecognisable. Anything that needs a
    case-insensitive arg (a direction, a player name) lowercases it itself.

    Returns ("", []) for empty/undecodable data.
    """
    try:
        text = data.decode(ENCODING).strip()
    except (UnicodeDecodeError, AttributeError):
        return "", []
    parts = text.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def args_str(args: list) -> str:
    """Rejoin a parsed args list into a single string for multi-word matching."""
    return " ".join(args)
