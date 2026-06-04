#!/usr/bin/python3

import enum
import select
import socket
import sys
import time

import pygame

from communication import (
    MAX_PACKET_SIZE, build_message, parse_message, args_str,
    CMD_CONNECT, CMD_DISCONNECT, CMD_INFO, CMD_STATUS, CMD_WHOAMI,
    CMD_WAIT, CMD_TURN, CMD_SCAN, CMD_MOVE, CMD_SHOOT,
    ARG_MAP, ARG_PLAYERS, ARG_FACING, ARG_COORDINATES,
    ARG_RIGHT, ARG_LEFT, ARG_WIDE, ARG_FAR, ARG_EXTENDED,
    ARG_STARTED, ARG_ENDED,
    RESP_WELCOME, RESP_MAP, RESP_PLAYERS, RESP_PLAYER, RESP_NAME,
    RESP_FACING, RESP_COORDINATES,
    RESP_GAME, RESP_YOU, RESP_WIDESCAN, RESP_FARSCAN, RESP_EXTENDEDSCAN, RESP_ERROR,
    YOU_WAITED, YOU_TURNED_RIGHT, YOU_TURNED_LEFT,
    YOU_SHOT, YOU_MOVED, YOU_CRASHED, YOU_DIED, YOU_GOT_SHOT, YOU_GOT_HIT, YOU_REPAIRED,
    SCAN_EMPTY, SCAN_TANK, SCAN_DEAD, SCAN_WALL, SCAN_SELF, SCAN_WIDTH,
)
from configuration import Configuration, TILE_SIZE
from tank_player import Player, Direction

# ---- Field display ----
COLOR_FIELD   = ( 34, 139,  34)
COLOR_GRID    = (  0, 100,   0)
COLOR_HP_BAR  = (200,   0,   0)
COLOR_HP_FULL = (  0, 200,   0)
COLOR_BARREL  = ( 30,  30,  30)
FONT_SIZE     = 11

# ---- HUD ----
HUD_WIDTH     = 170
HUD_BG        = ( 30,  30,  30)
HUD_BORDER    = ( 70,  70,  70)
HUD_TEXT      = (220, 220, 220)
HUD_DIM       = (110, 110, 110)   # dead / empty slots
HUD_FONT_SIZE = 16

BTN_HEIGHT  = 38
BTN_MARGIN  = 12
BTN_START   = ( 50, 150,  50)
BTN_END     = (180,  50,  50)
BTN_DISABLED = ( 70,  70,  70)
BTN_TEXT    = (255, 255, 255)

STATE_COLORS = {
    "waiting": (200, 200,  50),
    "running": ( 50, 210,  50),
    "ended":   (210,  80,  80),
}


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

class GameState(enum.Enum):
    WAITING = "waiting"
    RUNNING = "running"
    ENDED   = "ended"


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def draw_grid(surface: pygame.Surface, config: Configuration):
    """Fill and grid the field area only (leaves HUD column untouched)."""
    field_rect = pygame.Rect(0, 0, config.window_width, config.window_height)
    pygame.draw.rect(surface, COLOR_FIELD, field_rect)
    for col in range(config.field_width):
        for row in range(config.field_height):
            rect = pygame.Rect(col * TILE_SIZE, row * TILE_SIZE, TILE_SIZE, TILE_SIZE)
            pygame.draw.rect(surface, COLOR_GRID, rect, 1)


def draw_tank(surface: pygame.Surface, player: Player, font: pygame.font.Font):
    tx = player.x * TILE_SIZE
    ty = player.y * TILE_SIZE
    margin = 8

    body = pygame.Rect(tx + margin, ty + margin,
                       TILE_SIZE - 2 * margin, TILE_SIZE - 2 * margin)

    if not player.alive:
        # Burned-out wreck: charcoal fill, player-coloured outline, red X
        pygame.draw.rect(surface, (45, 45, 45), body)
        pygame.draw.rect(surface, player.color, body, 2)
        pygame.draw.line(surface, (180, 40, 40), body.topleft,  body.bottomright, 2)
        pygame.draw.line(surface, (180, 40, 40), body.topright, body.bottomleft,  2)
        return

    # ---- Alive tank ----
    pygame.draw.rect(surface, player.color, body)

    cx = tx + TILE_SIZE // 2
    cy = ty + TILE_SIZE // 2
    dx, dy = player.direction.delta()
    reach = TILE_SIZE // 2 - margin + 6
    pygame.draw.line(surface, COLOR_BARREL, (cx, cy),
                     (cx + dx * reach, cy + dy * reach), 4)

    bar_w  = TILE_SIZE - 2 * margin
    bar_h  = 4
    bar_x  = tx + margin
    bar_y  = ty + TILE_SIZE - margin - bar_h
    filled = int(bar_w * player.hit_points / player.health_max)
    pygame.draw.rect(surface, COLOR_HP_BAR,  pygame.Rect(bar_x, bar_y, bar_w,  bar_h))
    pygame.draw.rect(surface, COLOR_HP_FULL, pygame.Rect(bar_x, bar_y, filled, bar_h))

    label = font.render(player.name, True, (255, 255, 255))
    surface.blit(label, (tx + (TILE_SIZE - label.get_width()) // 2, ty + margin + 1))


def draw_hud(surface: pygame.Surface, server, font_title: pygame.font.Font,
             font_body: pygame.font.Font) -> pygame.Rect:
    """Draw the right-hand HUD. Returns the Rect of the action button."""
    win_w, win_h = surface.get_size()
    hud_x = win_w - HUD_WIDTH

    # Background and left border
    pygame.draw.rect(surface, HUD_BG, pygame.Rect(hud_x, 0, HUD_WIDTH, win_h))
    pygame.draw.line(surface, HUD_BORDER, (hud_x, 0), (hud_x, win_h), 2)

    y = 10

    # "Players" heading
    heading = font_title.render("Players", True, HUD_TEXT)
    surface.blit(heading, (hud_x + 8, y))
    y += heading.get_height() + 6
    pygame.draw.line(surface, HUD_BORDER, (hud_x + 6, y), (win_w - 6, y), 1)
    y += 8

    # One row per slot
    for player in server.slots:
        if player is None:
            y += font_body.get_height() + 4
            continue

        # Coloured dot
        dot_cx = hud_x + 14
        dot_cy = y + font_body.get_height() // 2
        pygame.draw.circle(surface, player.color, (dot_cx, dot_cy), 5)
        if not player.alive:
            # Strike-through circle for dead tanks
            pygame.draw.circle(surface, HUD_BG, (dot_cx, dot_cy), 3)

        # Name (truncated) and HP fraction
        hp_text = f"{player.hit_points}/{player.health_max}"
        name_text = player.name[:9]
        row_color = HUD_TEXT if player.alive else HUD_DIM
        name_surf = font_body.render(name_text, True, row_color)
        hp_surf   = font_body.render(hp_text,   True, row_color)
        surface.blit(name_surf, (hud_x + 24, y))
        surface.blit(hp_surf,   (win_w - hp_surf.get_width() - 8, y))
        y += font_body.get_height() + 4

    # Game state label
    state_label = server.game_state.value.capitalize()
    state_color = STATE_COLORS[server.game_state.value]
    state_surf  = font_body.render(state_label, True, state_color)
    btn_y = win_h - BTN_HEIGHT - BTN_MARGIN
    label_y = btn_y - state_surf.get_height() - 6
    surface.blit(state_surf,
                 (hud_x + (HUD_WIDTH - state_surf.get_width()) // 2, label_y))

    # Action button
    btn_rect = pygame.Rect(hud_x + BTN_MARGIN, btn_y,
                           HUD_WIDTH - 2 * BTN_MARGIN, BTN_HEIGHT)

    if server.game_state == GameState.WAITING:
        btn_color, btn_label = BTN_START, "Start"
    elif server.game_state == GameState.RUNNING:
        btn_color, btn_label = BTN_END, "End Game"
    else:
        btn_color, btn_label = BTN_DISABLED, "Game Over"

    pygame.draw.rect(surface, btn_color, btn_rect, border_radius=5)
    btn_surf = font_title.render(btn_label, True, BTN_TEXT)
    surface.blit(btn_surf, (
        btn_rect.x + (btn_rect.width  - btn_surf.get_width())  // 2,
        btn_rect.y + (btn_rect.height - btn_surf.get_height()) // 2,
    ))

    return btn_rect


# ---------------------------------------------------------------------------
# Starting positions
# ---------------------------------------------------------------------------

def _starting_positions(field_width: int, field_height: int) -> list:
    w, h = field_width - 1, field_height - 1
    return [
        (0,      0,      Direction.SOUTH),
        (w,      0,      Direction.SOUTH),
        (0,      h,      Direction.NORTH),
        (w,      h,      Direction.NORTH),
        (w // 2, 0,      Direction.SOUTH),
        (w // 2, h,      Direction.NORTH),
        (0,      h // 2, Direction.EAST),
        (w,      h // 2, Direction.WEST),
    ]


# ---------------------------------------------------------------------------
# Server state and command handling
# ---------------------------------------------------------------------------

# Commands always available after connecting, regardless of game state
_INFO_CMDS = {CMD_DISCONNECT, CMD_INFO}


class TanksServer:
    def __init__(self, config: Configuration):
        self.config      = config
        self.max_players = min(config.player_max, Player.MAX_PLAYERS)
        self.slots       = [None] * self.max_players
        self.players     = {}                 # addr → Player
        self.game_state  = GameState.WAITING

    # ------------------------------------------------------------------
    # Networking
    # ------------------------------------------------------------------

    def _send(self, sock: socket.socket, addr: tuple, *parts):
        sock.sendto(build_message(*parts), addr)

    def _broadcast(self, sock: socket.socket, *parts):
        msg = build_message(*parts)
        for player in self.players.values():
            sock.sendto(msg, player.address)

    # ------------------------------------------------------------------
    # Game lifecycle
    # ------------------------------------------------------------------

    def start_game(self, sock: socket.socket):
        if self.game_state != GameState.WAITING:
            return
        self.game_state = GameState.RUNNING
        self._broadcast(sock, RESP_GAME, ARG_STARTED)
        print("  * Game started")

    def end_game(self, sock: socket.socket):
        if self.game_state != GameState.RUNNING:
            return
        self.game_state = GameState.ENDED
        self._broadcast(sock, RESP_GAME, ARG_ENDED)
        print("  * Game ended")

    # ------------------------------------------------------------------
    # Packet dispatch
    # ------------------------------------------------------------------

    def handle_packet(self, sock: socket.socket, data: bytes, addr: tuple):
        cmd, args = parse_message(data)

        if cmd == CMD_CONNECT:
            self._handle_connect(sock, addr, args)
            return

        player = self.players.get(addr)
        if player is None:
            self._send(sock, addr, RESP_ERROR, "not connected — send: connect NAME")
            return

        # These commands are always available once connected
        if cmd == CMD_DISCONNECT:
            self._handle_disconnect(sock, addr)
            return
        if cmd == CMD_INFO:
            self._handle_info(sock, addr, player, args)
            return
        if cmd == CMD_STATUS:
            self._handle_status(sock, addr, player, args)
            return
        if cmd == CMD_WHOAMI:
            self._handle_whoami(sock, addr, player)
            return

        # All gameplay commands require the game to be running
        if self.game_state == GameState.WAITING:
            self._send(sock, addr, RESP_ERROR, "wait until started")
            return
        if self.game_state == GameState.ENDED:
            self._send(sock, addr, RESP_ERROR, "game has ended")
            return

        # Dead players cannot act
        if not player.alive:
            self._send(sock, addr, RESP_ERROR, "you are dead")
            return

        # Every gameplay command imposes a cooldown (the duration depends on the
        # command — see the individual handlers); a player on cooldown must wait.
        if cmd in {CMD_WAIT, CMD_TURN, CMD_MOVE, CMD_SHOOT, CMD_SCAN}:
            if player.is_on_cooldown():
                self._send(sock, addr, RESP_ERROR, "on cooldown")
                return

        dispatch = {
            CMD_WAIT:  lambda: self._handle_wait(sock, addr, player),
            CMD_TURN:  lambda: self._handle_turn(sock, addr, player, args),
            CMD_MOVE:  lambda: self._handle_move(sock, addr, player),
            CMD_SHOOT: lambda: self._handle_shoot(sock, addr, player),
            CMD_SCAN:  lambda: self._handle_scan(sock, addr, player, args),
        }
        handler = dispatch.get(cmd)
        if handler:
            handler()
        else:
            self._send(sock, addr, RESP_ERROR, f"unknown command: {cmd}")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_connect(self, sock, addr, args):
        if addr in self.players:
            self._send(sock, addr, RESP_WELCOME, self.players[addr].name)
            return

        if self.game_state != GameState.WAITING:
            self._send(sock, addr, RESP_ERROR, "game already in progress")
            return

        if not args:
            self._send(sock, addr, RESP_ERROR, "usage: connect NAME")
            return

        slot = next((i for i, p in enumerate(self.slots) if p is None), None)
        if slot is None:
            self._send(sock, addr, RESP_ERROR, "server full")
            return

        name = args[0][:16].lower()    # player names are stored lowercase
        x, y, direction = _starting_positions(
            self.config.field_width, self.config.field_height)[slot]

        player = Player(name, slot, addr, x=x, y=y, direction=direction,
                        health_starting=self.config.health_starting,
                        health_max=self.config.health_max)
        self.players[addr] = player
        self.slots[slot]   = player
        print(f"  + {name!r} ({player.color_name}) from {addr}")
        self._send(sock, addr, RESP_WELCOME, name)

    def _handle_disconnect(self, sock, addr):
        player = self.players.pop(addr, None)
        if player:
            self.slots[player.slot] = None
            print(f"  - {player.name!r} disconnected")

    def _handle_info(self, sock, addr, player, args):
        usage = "usage: info map|players|facing|coordinates"
        if not args:
            self._send(sock, addr, RESP_ERROR, usage)
            return
        arg = args[0].lower()
        if arg == ARG_MAP:
            self._send(sock, addr, RESP_MAP,
                       self.config.field_width, self.config.field_height)
        elif arg == ARG_PLAYERS:
            names = ",".join(p.name for p in self.slots if p is not None)
            self._send(sock, addr, RESP_PLAYERS, names if names else "(none)")
        elif arg == ARG_FACING:
            self._send(sock, addr, RESP_FACING, str(player.direction))
        elif arg == ARG_COORDINATES:
            self._send(sock, addr, RESP_COORDINATES, player.x, player.y)
        else:
            self._send(sock, addr, RESP_ERROR, usage)

    def _handle_whoami(self, sock, addr, player):
        self._send(sock, addr, RESP_NAME, player.name)

    def _handle_status(self, sock, addr, player, args):
        if args:
            target_name = args[0].lower()    # names are stored lowercase
            target = next((p for p in self.players.values()
                           if p.name == target_name), None)
            if target is None:
                self._send(sock, addr, RESP_ERROR, f"player {target_name} not found")
                return
        else:
            target = player
        self._send(sock, addr, RESP_PLAYER,
                   target.name, "health is", target.hit_points)

    def _handle_wait(self, sock, addr, player):
        player.set_cooldown(self.config.move_cooldown)
        repaired = player.record_wait(self.config.repair_waits)
        self._send(sock, addr, RESP_YOU, YOU_WAITED)
        if repaired:
            self._send(sock, addr, RESP_YOU, YOU_REPAIRED)

    def _handle_turn(self, sock, addr, player, args):
        if not args:
            self._send(sock, addr, RESP_ERROR, "usage: turn right|left")
            return
        if args[0].lower() == ARG_RIGHT:
            player.set_cooldown(self.config.move_cooldown)
            player.turn_right()
            self._send(sock, addr, RESP_YOU, YOU_TURNED_RIGHT)
        elif args[0].lower() == ARG_LEFT:
            player.set_cooldown(self.config.move_cooldown)
            player.turn_left()
            self._send(sock, addr, RESP_YOU, YOU_TURNED_LEFT)
        else:
            self._send(sock, addr, RESP_ERROR, "usage: turn right|left")

    def _handle_move(self, sock, addr, player):
        dx, dy = player.direction.delta()
        nx, ny = player.x + dx, player.y + dy

        # Cooldown is consumed whether the move succeeds or crashes
        player.set_cooldown(self.config.move_cooldown)

        if not (0 <= nx < self.config.field_width and
                0 <= ny < self.config.field_height):
            # Wall crash: mover gets "you crashed" then "you got hit"
            self._send(sock, addr, RESP_YOU, YOU_CRASHED)
            self._apply_hit(sock, player, cause="wall")
            return

        for other in self.players.values():
            if other is not player and other.x == nx and other.y == ny:
                # Tank crash: mover gets "you crashed"; struck tank gets "you got hit"
                # Both lose 1 HP
                self._send(sock, addr, RESP_YOU, YOU_CRASHED)
                remaining = player.take_hit()
                if remaining == 0:
                    self._send(sock, addr, RESP_YOU, YOU_DIED)
                    print(f"  ! {player.name!r} was destroyed (tank collision)")
                self._apply_hit(sock, other, cause=f"hit by {player.name}")
                return

        player.x, player.y = nx, ny
        self._send(sock, addr, RESP_YOU, YOU_MOVED)

    def _apply_hit(self, sock, player, cause=""):
        """Apply 1 damage to a player, send 'you got hit', and notify on death."""
        remaining = player.take_hit()
        self._send(sock, player.address, RESP_YOU, YOU_GOT_HIT)
        if remaining == 0:
            self._send(sock, player.address, RESP_YOU, YOU_DIED)
            print(f"  ! {player.name!r} was destroyed ({cause})")

    def _handle_shoot(self, sock, addr, player):
        dx, dy = player.direction.delta()
        x, y   = player.x + dx, player.y + dy
        hit    = None

        while (0 <= x < self.config.field_width and
               0 <= y < self.config.field_height):
            for other in self.players.values():
                if other is not player and other.x == x and other.y == y and other.alive:
                    hit = other
                    break
            if hit:
                break
            x += dx
            y += dy

        player.set_cooldown(self.config.move_cooldown)
        self._send(sock, addr, RESP_YOU, YOU_SHOT)

        if hit:
            remaining = hit.take_hit()
            self._send(sock, hit.address, RESP_YOU, YOU_GOT_SHOT)
            if remaining == 0:
                self._send(sock, hit.address, RESP_YOU, YOU_DIED)
                print(f"  ! {hit.name!r} was killed by {player.name!r}")

    def _handle_scan(self, sock, addr, player, args):
        if not args:
            self._send(sock, addr, RESP_ERROR, "usage: scan wide|far|extended")
            return
        if args[0].lower() == ARG_WIDE:
            player.set_cooldown(self.config.scan_cooldown)
            self._send(sock, addr, RESP_WIDESCAN, self._scan_wide(player))
        elif args[0].lower() == ARG_FAR:
            player.set_cooldown(self.config.scan_cooldown)
            self._send(sock, addr, RESP_FARSCAN, self._scan_far(player))
        elif args[0].lower() == ARG_EXTENDED:
            player.set_cooldown(self.config.extended_cooldown)
            self._send(sock, addr, RESP_EXTENDEDSCAN, self._scan_extended(player))
        else:
            self._send(sock, addr, RESP_ERROR, "usage: scan wide|far|extended")

    def _tile_symbol(self, x, y, exclude=None):
        if not (0 <= x < self.config.field_width and
                0 <= y < self.config.field_height):
            return SCAN_WALL
        for p in self.players.values():
            if p is not exclude and p.x == x and p.y == y:
                return SCAN_TANK if p.alive else SCAN_DEAD
        return SCAN_EMPTY

    def _scan_wide(self, player):
        cells = []
        for row_dy in range(-1, 2):
            for col_dx in range(-1, 2):
                if col_dx == 0 and row_dy == 0:
                    cells.append(SCAN_SELF)
                else:
                    cells.append(self._tile_symbol(
                        player.x + col_dx, player.y + row_dy, exclude=player))
        return "".join(cells)

    def _scan_extended(self, player):
        """Like a wide scan, but a 5x5 area (25 chars) centred on the tank."""
        cells = []
        for row_dy in range(-2, 3):
            for col_dx in range(-2, 3):
                if col_dx == 0 and row_dy == 0:
                    cells.append(SCAN_SELF)
                else:
                    cells.append(self._tile_symbol(
                        player.x + col_dx, player.y + row_dy, exclude=player))
        return "".join(cells)

    def _scan_far(self, player):
        dx, dy = player.direction.delta()
        cells  = []
        x, y   = player.x, player.y
        for _ in range(SCAN_WIDTH):
            x += dx
            y += dy
            sym = self._tile_symbol(x, y, exclude=player)
            cells.append(sym)
            if sym == SCAN_WALL:
                break
        while len(cells) < SCAN_WIDTH:
            cells.append(" ")
        return "".join(cells)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = Configuration()
    server = TanksServer(config)

    pygame.init()
    pygame.font.init()
    font_small = pygame.font.SysFont(None, FONT_SIZE)
    font_hud   = pygame.font.SysFont(None, HUD_FONT_SIZE)
    font_hud_b = pygame.font.SysFont(None, HUD_FONT_SIZE + 4)  # button / heading

    window_w = config.window_width + HUD_WIDTH
    window_h = config.window_height
    screen = pygame.display.set_mode((window_w, window_h))
    pygame.display.set_caption("AI Tanks Server")
    clock = pygame.time.Clock()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((config.listen_address, config.listen_port))
    sock.setblocking(False)
    print("AI Tanks server started")
    print(f"  Listening  : {config.listen_address}:{config.listen_port} (UDP)")
    print(f"  Field      : {config.field_width} x {config.field_height} tiles")
    print(f"  Max players: {server.max_players}")

    button_rect = None
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if button_rect and button_rect.collidepoint(event.pos):
                    if server.game_state == GameState.WAITING:
                        server.start_game(sock)
                    elif server.game_state == GameState.RUNNING:
                        server.end_game(sock)

        readable, _, _ = select.select([sock], [], [], 0)
        for s in readable:
            data, addr = s.recvfrom(MAX_PACKET_SIZE)
            server.handle_packet(s, data, addr)

        draw_grid(screen, config)
        for player in server.players.values():
            draw_tank(screen, player, font_small)
        button_rect = draw_hud(screen, server, font_hud_b, font_hud)
        pygame.display.flip()
        clock.tick(60)

    sock.close()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
