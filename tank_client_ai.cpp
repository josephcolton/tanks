// tank_client_ai.cpp — simple AI Tanks client (C++ port of tank_client_ai.py)
//
// Build:  g++ -std=c++17 -Wall -O2 -o tank_client_ai tank_client_ai.cpp
// Usage:  ./tank_client_ai NAME HOST [PORT]
//         ./tank_client_ai          (interactive prompts)
//
// Press Ctrl-C to disconnect and quit.

#include <algorithm>
#include <chrono>
#include <cstring>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Protocol constants  (mirrors communication.py)
// ---------------------------------------------------------------------------
static const int    MAX_PACKET = 4096;
static const double REPLY_TIMEOUT  = 1.0;   // seconds
static const double ACTION_PAUSE   = 1.10;  // seconds — slightly longer than server cooldown

static const std::string CMD_CONNECT    = "connect";
static const std::string CMD_DISCONNECT = "disconnect";
static const std::string CMD_INFO       = "info";
static const std::string CMD_SCAN       = "scan";
static const std::string CMD_MOVE       = "move";
static const std::string CMD_TURN       = "turn";
static const std::string CMD_SHOOT      = "shoot";

static const std::string ARG_WIDE    = "wide";
static const std::string ARG_FACING  = "facing";
static const std::string ARG_LEFT    = "left";
static const std::string ARG_RIGHT   = "right";
static const std::string ARG_STARTED = "started";
static const std::string ARG_ENDED   = "ended";

static const std::string RESP_WELCOME  = "welcome";
static const std::string RESP_GAME     = "game";
static const std::string RESP_YOU      = "you";
static const std::string RESP_WIDESCAN = "widescan";
static const std::string RESP_FACING   = "facing";
static const std::string RESP_ERROR    = "error";

static const std::string YOU_DIED     = "died";
static const std::string YOU_GOT_SHOT = "got shot";

static const char SCAN_TANK  = 'T';
static const char SCAN_WALL  = 'X';
static const char SCAN_EMPTY = '.';

// ---------------------------------------------------------------------------
// Direction
// ---------------------------------------------------------------------------
// Values match the Python Direction enum (degrees clockwise from north).
enum class Direction { NORTH = 0, EAST = 90, SOUTH = 180, WEST = 270 };

static Direction turn_right(Direction d) {
    return static_cast<Direction>((static_cast<int>(d) + 90) % 360);
}
static Direction turn_left(Direction d) {
    return static_cast<Direction>((static_cast<int>(d) + 270) % 360);
}
// (dx, dy) for one step in direction d.  +x = east, +y = south (screen coords).
static std::pair<int,int> delta(Direction d) {
    switch (d) {
        case Direction::NORTH: return { 0, -1};
        case Direction::EAST:  return { 1,  0};
        case Direction::SOUTH: return { 0,  1};
        case Direction::WEST:  return {-1,  0};
    }
    return {0, 0};
}
static bool parse_direction(const std::string& s, Direction& out) {
    if (s == "north") { out = Direction::NORTH; return true; }
    if (s == "east")  { out = Direction::EAST;  return true; }
    if (s == "south") { out = Direction::SOUTH; return true; }
    if (s == "west")  { out = Direction::WEST;  return true; }
    return false;
}
static std::string dir_name(Direction d) {
    switch (d) {
        case Direction::NORTH: return "north";
        case Direction::EAST:  return "east";
        case Direction::SOUTH: return "south";
        case Direction::WEST:  return "west";
    }
    return "north";
}

// ---------------------------------------------------------------------------
// Message helpers
// ---------------------------------------------------------------------------
// Join tokens into a space-separated string ready to send over UDP.
static std::string build_message(const std::vector<std::string>& parts) {
    std::string out;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i) out += ' ';
        out += parts[i];
    }
    return out;
}

// Split a received packet into {command, args}.  Command is lowercased;
// args keep their original case (scan symbols like T/X/D/S are case-sensitive).
static std::pair<std::string, std::vector<std::string>> parse_message(const std::string& data) {
    std::istringstream ss(data);
    std::string token;
    std::vector<std::string> tokens;
    while (ss >> token) tokens.push_back(token);
    if (tokens.empty()) return {"", {}};
    std::string cmd = tokens[0];
    std::transform(cmd.begin(), cmd.end(), cmd.begin(), ::tolower);
    return {cmd, {tokens.begin() + 1, tokens.end()}};
}

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
using Clock = std::chrono::steady_clock;

static double now_secs() {
    static const auto start = Clock::now();
    return std::chrono::duration<double>(Clock::now() - start).count();
}
static void sleep_secs(double s) {
    std::this_thread::sleep_for(std::chrono::duration<double>(s));
}

// ---------------------------------------------------------------------------
// Wide-scan cell index
// ---------------------------------------------------------------------------
// The 3x3 wide scan arrives as a 9-character string in reading order, north-up:
//
//   index: 0 1 2     NW N NE
//          3 4 5      W S  E   (index 4 = SCAN_SELF, our tile)
//          6 7 8     SW S SE
//
// This function returns the index of the tile one step in direction d.
static int scan_cell(Direction d) {
    auto [dx, dy] = delta(d);
    return (dy + 1) * 3 + (dx + 1);   // N=1, E=5, S=7, W=3
}

// ---------------------------------------------------------------------------
// AITank
// ---------------------------------------------------------------------------
class AITank {
public:
    AITank(const std::string& name, const std::string& host, int port)
        : name_(name), game_running_(false), game_over_(false), alive_(true),
          has_facing_(false), facing_(Direction::NORTH), rng_(std::random_device{}())
    {
        // Resolve hostname to an IPv4 address.
        struct addrinfo hints{}, *res = nullptr;
        hints.ai_family   = AF_INET;
        hints.ai_socktype = SOCK_DGRAM;
        int rc = getaddrinfo(host.c_str(), nullptr, &hints, &res);
        if (rc != 0 || res == nullptr) {
            std::cerr << "Cannot resolve host '" << host << "': "
                      << gai_strerror(rc) << "\n";
            exit(1);
        }
        memset(&server_addr_, 0, sizeof(server_addr_));
        auto* sin = reinterpret_cast<sockaddr_in*>(res->ai_addr);
        server_addr_.sin_family = AF_INET;
        server_addr_.sin_addr   = sin->sin_addr;
        server_addr_.sin_port   = htons(static_cast<uint16_t>(port));
        freeaddrinfo(res);

        sock_ = socket(AF_INET, SOCK_DGRAM, 0);
        if (sock_ < 0) { perror("socket"); exit(1); }
    }

    ~AITank() { ::close(sock_); }

    // ------------------------------------------------------------------
    // Low-level networking
    // ------------------------------------------------------------------

    void send_msg(const std::vector<std::string>& parts) {
        std::string msg = build_message(parts);
        sendto(sock_, msg.c_str(), msg.size(), 0,
               reinterpret_cast<sockaddr*>(&server_addr_), sizeof(server_addr_));
    }

    // Try to read one UDP packet, waiting up to timeout_s seconds.
    // Returns {cmd, args} on success, or {"", {}} on timeout / error.
    std::pair<std::string, std::vector<std::string>> recv_packet(double timeout_s) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(sock_, &fds);
        struct timeval tv;
        tv.tv_sec  = static_cast<long>(timeout_s);
        tv.tv_usec = static_cast<long>((timeout_s - (long)timeout_s) * 1e6);
        int ready = select(sock_ + 1, &fds, nullptr, nullptr, &tv);
        if (ready <= 0) return {"", {}};
        char buf[MAX_PACKET];
        ssize_t n = recvfrom(sock_, buf, sizeof(buf) - 1, 0, nullptr, nullptr);
        if (n <= 0) return {"", {}};
        buf[n] = '\0';
        return parse_message(std::string(buf, static_cast<size_t>(n)));
    }

    // Update bot state from any server message (called for every received packet).
    void handle_async(const std::string& cmd, const std::vector<std::string>& args) {
        if (cmd == RESP_GAME && !args.empty()) {
            if (args[0] == ARG_STARTED) {
                game_running_ = true;
                std::cout << "[server] game started\n";
            } else if (args[0] == ARG_ENDED) {
                game_running_ = false;
                game_over_    = true;
                std::cout << "[server] game ended\n";
            }
        } else if (cmd == RESP_YOU && !args.empty()) {
            // Multi-word outcomes like "got shot" need the args rejoined.
            std::string outcome;
            for (size_t i = 0; i < args.size(); ++i) {
                if (i) outcome += ' ';
                outcome += args[i];
            }
            if (outcome == YOU_DIED) {
                alive_ = false;
                std::cout << "[server] you died\n";
            } else if (outcome == YOU_GOT_SHOT) {
                std::cout << "[server] you got shot!\n";
            }
        } else if (cmd == RESP_ERROR) {
            std::cout << "[server] error";
            for (auto& a : args) std::cout << ' ' << a;
            std::cout << '\n';
        }
    }

    // Poll until one of the wanted responses arrives or the deadline passes.
    // Every received packet updates bot state via handle_async.
    std::pair<std::string, std::vector<std::string>> request(
        const std::vector<std::string>& wanted,
        double timeout_s = REPLY_TIMEOUT)
    {
        double deadline = now_secs() + timeout_s;
        while (now_secs() < deadline) {
            double remaining = deadline - now_secs();
            if (remaining <= 0) break;
            auto [cmd, args] = recv_packet(std::min(remaining, 0.1));
            if (cmd.empty()) continue;
            handle_async(cmd, args);
            for (const auto& w : wanted)
                if (cmd == w) return {cmd, args};
        }
        return {"", {}};
    }

    // ------------------------------------------------------------------
    // Connection
    // ------------------------------------------------------------------

    bool connect() {
        send_msg({CMD_CONNECT, name_});
        auto [cmd, args] = request({RESP_WELCOME}, 2.0);
        if (cmd.empty()) {
            std::cout << "No welcome from server — is it running?\n";
            return false;
        }
        std::cout << "Connected as " << name_ << ".\n";
        return true;
    }

    void disconnect() {
        send_msg({CMD_DISCONNECT});
    }

    // ------------------------------------------------------------------
    // Gameplay commands
    // ------------------------------------------------------------------

    // Scan the 3x3 area around us.  Returns the 9-character result string.
    std::string scan_wide() {
        std::cout << "  -> scan wide\n";
        send_msg({CMD_SCAN, ARG_WIDE});
        auto [cmd, args] = request({RESP_WIDESCAN});
        sleep_secs(ACTION_PAUSE);
        return (!cmd.empty() && !args.empty()) ? args[0] : "";
    }

    // Send an action command, wait for acknowledgement, then honour cooldown.
    void do_action(const std::vector<std::string>& parts) {
        send_msg(parts);
        request({RESP_YOU, RESP_ERROR});
        sleep_secs(ACTION_PAUSE);
    }

    void shoot() { std::cout << "  -> shoot\n";       do_action({CMD_SHOOT}); }
    void move()  { std::cout << "  -> move\n";        do_action({CMD_MOVE}); }

    void turn(const std::string& side) {
        std::cout << "  -> turn " << side << "\n";
        do_action({CMD_TURN, side});
        if (has_facing_)
            facing_ = (side == ARG_RIGHT) ? turn_right(facing_) : turn_left(facing_);
    }

    void query_facing() {
        send_msg({CMD_INFO, ARG_FACING});
        auto [cmd, args] = request({RESP_FACING});
        if (!cmd.empty() && !args.empty()) {
            std::string s = args[0];
            std::transform(s.begin(), s.end(), s.begin(), ::tolower);
            if (parse_direction(s, facing_)) {
                has_facing_ = true;
                std::cout << "  [facing: " << dir_name(facing_) << "]\n";
            }
        }
    }

    // ------------------------------------------------------------------
    // Decision helpers  (mirrors AITank._safe_turns / _pick_turn)
    // ------------------------------------------------------------------

    // Turn directions that would not leave us facing a wall.
    std::vector<std::string> safe_turns(const std::string& wide) {
        if (!has_facing_) return {ARG_LEFT, ARG_RIGHT};
        std::vector<std::string> safe;
        for (const auto& side : {ARG_LEFT, ARG_RIGHT}) {
            Direction nf = (side == ARG_LEFT) ? turn_left(facing_) : turn_right(facing_);
            int idx = scan_cell(nf);
            if (wide.empty() || idx >= (int)wide.size() || wide[idx] != SCAN_WALL)
                safe.push_back(side);
        }
        return safe;
    }

    std::string pick_turn(const std::string& wide, const std::string& prefer = "") {
        auto safe = safe_turns(wide);
        if (!prefer.empty()) {
            for (const auto& s : safe)
                if (s == prefer) return prefer;
        }
        if (!safe.empty()) {
            std::uniform_int_distribution<int> dist(0, (int)safe.size() - 1);
            return safe[dist(rng_)];
        }
        return prefer.empty() ? ARG_LEFT : prefer;
    }

    // ------------------------------------------------------------------
    // Per-turn decision  (mirrors AITank.take_turn)
    // ------------------------------------------------------------------

    void take_turn() {
        if (!has_facing_) query_facing();

        std::string wide = scan_wide();
        if (!alive_) return;

        // Tile directly in front of us.
        char ahead = '\0';
        if (!wide.empty() && has_facing_) {
            int idx = scan_cell(facing_);
            if (idx < (int)wide.size()) ahead = wide[idx];
        }

        // 1. Enemy right ahead → shoot.
        if (ahead == SCAN_TANK) { shoot(); return; }

        // 2. Enemy somewhere nearby → turn to try to line up a shot.
        if (!wide.empty() && wide.find(SCAN_TANK) != std::string::npos) {
            std::cout << "  enemy nearby — turning to aim\n";
            turn(pick_turn(wide, ARG_RIGHT));
            return;
        }

        // 3. Move forward if clear, otherwise turn at random.
        std::bernoulli_distribution coin(0.5);
        if (ahead == SCAN_EMPTY && coin(rng_)) {
            move();
        } else {
            turn(pick_turn(wide));
        }
    }

    // ------------------------------------------------------------------
    // Main loop
    // ------------------------------------------------------------------

    void run() {
        if (!connect()) return;
        std::cout << "Waiting for the game to start...\n";

        while (true) {
            if (game_over_)    { std::cout << "Game ended — exiting.\n"; break; }
            if (!game_running_) { request({RESP_GAME}, 1.0); continue; }
            if (!alive_)        { request({RESP_GAME}, 1.0); continue; }
            take_turn();
        }
        disconnect();
    }

private:
    std::string  name_;
    int          sock_;
    sockaddr_in  server_addr_;

    bool         game_running_;
    bool         game_over_;
    bool         alive_;
    bool         has_facing_;
    Direction    facing_;
    std::mt19937 rng_;
};

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
int main(int argc, char* argv[]) {
    std::string name, host;
    int port = 1234;

    if (argc >= 3) {
        name = argv[1];
        host = argv[2];
        if (argc >= 4) port = std::stoi(argv[3]);
    } else {
        std::cout << "AI Tanks — simple AI client (C++)\n";
        std::cout << "Tank name: ";
        std::getline(std::cin, name);
        if (name.empty()) name = "Tank";

        std::cout << "Server hostname or IP [127.0.0.1]: ";
        std::getline(std::cin, host);
        if (host.empty()) host = "127.0.0.1";

        std::string port_str;
        std::cout << "Server port [1234]: ";
        std::getline(std::cin, port_str);
        if (!port_str.empty()) port = std::stoi(port_str);
    }

    AITank(name, host, port).run();
    return 0;
}
