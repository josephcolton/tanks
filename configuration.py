import configparser

CONFIG_FILE = "tanks.conf"

TILE_SIZE = 64


class Configuration:
    def __init__(self, config_file=CONFIG_FILE):
        parser = configparser.ConfigParser()
        if not parser.read(config_file):
            raise FileNotFoundError(f"Configuration file not found: {config_file}")

        raw_address = parser.get("server", "listen_address")
        self.listen_address = "0.0.0.0" if raw_address.strip().lower() == "any" else raw_address.strip()
        self.listen_port = parser.getint("server", "listen_port")

        self.field_width = parser.getint("game", "field_width")
        self.field_height = parser.getint("game", "field_height")
        self.player_max = parser.getint("game", "player_max")

        self.health_max      = parser.getint("game", "health_max",      fallback=3)
        self.health_starting = parser.getint("game", "health_starting", fallback=3)
        self.repair_waits    = parser.getint("game", "repair_waits",    fallback=10)
        self.move_cooldown   = parser.getfloat("game", "move_cooldown", fallback=1.0)
        self.scan_cooldown     = parser.getfloat("game", "scan_cooldown",     fallback=1.0)
        self.extended_cooldown = parser.getfloat("game", "extended_cooldown", fallback=2.0)

    @property
    def window_width(self):
        return self.field_width * TILE_SIZE

    @property
    def window_height(self):
        return self.field_height * TILE_SIZE
