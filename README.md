# AI Tanks

A networked tank battle game for student AI agents. A central server displays the battlefield and manages game state; clients connect over UDP and control their tank by sending text commands.

---

## Requirements

- Python 3.9+
- [pygame](https://www.pygame.org/) — `pip install pygame`
- [windows-curses](https://pypi.org/project/windows-curses/) — `pip install windows-curses` *(Windows only, required for the interactive client)*

---

## Running the Game

**Start the server:**
```
python tanks_server.py
```

The server opens a pygame window showing the battlefield and a HUD listing connected players and their health. Click **Start** to begin the game and **End Game** to stop it.

**Connect a client:**
```
python tank_client_interactive.py NAME HOST [PORT]
```

Example:
```
python tank_client_interactive.py Alice 127.0.0.1 1234
```

The client displays a scrolling log of the dialog between client and server above an input prompt. Type commands at the `>` prompt. Type `help` for a local command list (it is not sent to the server). Press **ESC** or type `quit` to disconnect and exit.

---

## Configuration (`tanks.conf`)

| Key | Section | Description |
|---|---|---|
| `listen_address` | `[server]` | IP to bind (`any` = all interfaces) |
| `listen_port` | `[server]` | UDP port number |
| `field_width` | `[game]` | Field width in tiles |
| `field_height` | `[game]` | Field height in tiles |
| `player_max` | `[game]` | Maximum simultaneous players (hard cap: 8) |
| `health_max` | `[game]` | Maximum health a tank can reach |
| `health_starting` | `[game]` | Health each tank starts the game with |
| `repair_waits` | `[game]` | Number of `wait` commands needed to restore 1 health |
| `move_cooldown` | `[game]` | Seconds of cooldown after move, turn, shoot, or wait |
| `scan_cooldown` | `[game]` | Seconds of cooldown after a `scan wide` or `scan far` |
| `extended_cooldown` | `[game]` | Seconds of cooldown after a `scan extended` |

---

## Protocol

All messages are plain UTF-8 text with space-separated tokens. Commands are **case-insensitive**. Player names are stored in lowercase.

---

## Client → Server Commands

### Connection

| Command | Description |
|---|---|
| `connect NAME` | Connect with display name (max 16 chars). Available before the game starts. |
| `disconnect` | Notify the server you are leaving. Available at any time. |

### Information
*Available at any time once connected, regardless of game state.*

| Command | Description |
|---|---|
| `info map` | Request field dimensions. |
| `info players` | Request the list of connected players. |
| `info facing` | Request the direction your tank is facing. |
| `info coordinates` | Request your tank's tile position. |
| `whoami` | Ask the server for your own registered name. |
| `status` | Ask for your own current health. |
| `status NAME` | Ask for another player's current health. |

### Gameplay
*Requires the game to be running and the player to be alive.*

Every gameplay command puts the tank on a **cooldown**, and the tank cannot issue another gameplay command until the cooldown elapses. The cooldown length depends on the command:

| Command(s) | Cooldown |
|---|---|
| `move`, `turn`, `shoot`, `wait` | `move_cooldown` seconds |
| `scan wide`, `scan far` | `scan_cooldown` seconds |
| `scan extended` | `extended_cooldown` seconds (the longest — it reveals the most) |

| Command | Description |
|---|---|
| `wait` | Do nothing this turn. Accumulates toward health repair (see below). |
| `turn right` | Rotate 90° clockwise. |
| `turn left` | Rotate 90° counter-clockwise. |
| `move` | Move one tile in the current facing direction. Crashing into a wall or any tank (alive or dead) deals 1 damage. |
| `shoot` | Fire in the current facing direction. |
| `scan wide` | Scan the 3×3 area centred on your tank. |
| `scan far` | Scan up to 9 tiles ahead in the facing direction. |
| `scan extended` | Scan the 5×5 area centred on your tank (more information, longer cooldown). |

#### Blocking conditions for gameplay commands

| Condition | Error response |
|---|---|
| Game has not started | `error wait until started` |
| Game has ended | `error game has ended` |
| Player is dead | `error you are dead` |
| Cooldown active | `error on cooldown` |

#### Crash damage

Attempting to `move` into a wall or any tank (alive or dead) is a crash. The tank does **not** move. The cooldown is still consumed.

**Crashing into a wall** — only the moving tank is affected:

```
you crashed       ← movement blocked, 1 health deducted
you got hit       ← 1 health deducted
you died          ← only sent if health reaches 0
```

**Crashing into another tank** — both tanks lose 1 HP, but receive different messages:

| Tank | Messages received |
|---|---|
| Moving tank | `you crashed` (then `you died` if health reaches 0) |
| Struck tank | `you got hit` (then `you died` if health reaches 0) |

Dead tanks remain on the board as burned-out wrecks. They block movement but do **not** block shots or scan rays.

#### Health repair

Each `wait` increments a counter. Every `repair_waits` accumulated waits, 1 health is restored up to `health_max`. The server sends `you repaired` when this occurs.

#### Scan formats

Scans return a string in row-major (reading) order using these symbols:

| Symbol | Meaning |
|---|---|
| `S` | Your tank (centre of `scan wide` / `scan extended`) |
| `.` | Empty tile |
| `T` | Alive tank |
| `D` | Burned-out (dead) tank — blocks movement, shots and scan rays pass through |
| `X` | Wall / out of bounds |

**`scan wide`** returns a **9-character** 3×3 grid centred on your tank (north up, regardless of facing):

```
[ 0][ 1][ 2]
[ 3][ S][ 5]   ← S is always position 4 (your tile)
[ 6][ 7][ 8]
```

**`scan extended`** returns a **25-character** 5×5 grid centred on your tank (north up, regardless of facing):

```
[ 0][ 1][ 2][ 3][ 4]
[ 5][ 6][ 7][ 8][ 9]
[10][11][12][13][14]   ← S is always position 12 (your tile)
[15][16][17][18][19]
[20][21][22][23][24]
```

**`scan far`** returns tiles 1–9 directly ahead in the facing direction. The ray stops at the first wall (`X`); dead tanks (`D`) do not stop it. The string is space-padded to 9 characters.

---

## Server → Client Responses

### Connection and information

| Response | Meaning |
|---|---|
| `welcome NAME` | Connection accepted. |
| `map WIDTH HEIGHT` | Field dimensions (reply to `info map`). |
| `players name1,name2,name3` | Connected player list (reply to `info players`). |
| `facing DIRECTION` | Direction you face — `north`/`east`/`south`/`west` (reply to `info facing`). |
| `coordinates X Y` | Your tile position (reply to `info coordinates`). |
| `name NAME` | Your registered name (reply to `whoami`). |
| `player NAME health is X` | Named player's current health (reply to `status`). |

### Game state broadcasts

| Response | Meaning |
|---|---|
| `game started` | The server operator has started the game. |
| `game ended` | The server operator has ended the game. |

### Action confirmations

| Response | Meaning |
|---|---|
| `you waited` | Wait command processed. |
| `you repaired` | Health restored by 1 after accumulating enough waits. |
| `you turned right` | Tank rotated clockwise. |
| `you turned left` | Tank rotated counter-clockwise. |
| `you moved` | Tank moved one tile. |
| `you crashed` | Move destination was a wall or occupied tank; tank did not move. Followed by `you died` if health reaches 0. |
| `you got hit` | Tank took 1 damage from crashing. Followed by `you died` if health reaches 0. |
| `you shot` | Shot fired. |
| `you got shot` | Your tank was hit by another player's shot. |
| `you died` | Health reached 0; gameplay commands are no longer available. |

### Scan results

| Response | Meaning |
|---|---|
| `widescan CCCCCCCCC` | 9-character 3×3 area string (reply to `scan wide`). |
| `farscan CCCCCCCCC` | 9-character line string (reply to `scan far`). |
| `extendedscan CCC…C` | 25-character 5×5 area string (reply to `scan extended`). |

### Errors

| Response | Cause |
|---|---|
| `error not connected — send: connect NAME` | Command received before connecting. |
| `error usage: connect NAME` | `connect` sent without a name. |
| `error server full` | Maximum player count already reached. |
| `error game already in progress` | `connect` attempted after the game started. |
| `error wait until started` | Gameplay command sent before the game starts. |
| `error game has ended` | Gameplay command sent after the game ended. |
| `error you are dead` | Gameplay command sent after dying. |
| `error on cooldown` | Action command sent within 1 second of the last one. |
| `error player NAME not found` | `status NAME` used an unknown player name. |
| `error usage: info map\|players\|facing\|coordinates` | `info` sent without a valid argument. |
| `error usage: turn right\|left` | `turn` sent without a valid direction. |
| `error usage: scan wide\|far\|extended` | `scan` sent without a valid argument. |
| `error unknown command: CMD` | Unrecognised command. |

---

## File Overview

| File | Purpose |
|---|---|
| `tanks_server.py` | Server — pygame display, game state, UDP command handling |
| `tank_client_interactive.py` | Interactive curses client for testing |
| `tank_client_ai.py` | Example autonomous AI client (a starting point for students) |
| `tank_player.py` | `Player` class and `Direction` enum |
| `communication.py` | Shared protocol constants and encode/decode helpers |
| `configuration.py` | Reads `tanks.conf` |
| `tanks.conf` | Game and server configuration |
