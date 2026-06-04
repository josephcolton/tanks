// tank_client_ai.cs — simple AI Tanks client (C# port of tank_client_ai.py)
//
// Requires .NET 6 SDK or later.  Install on Ubuntu:
//   sudo apt install dotnet-sdk-8.0
//
// Build and run:
//   dotnet run --project tank_client_ai.csproj -- NAME HOST [PORT]
//   dotnet run --project tank_client_ai.csproj          (interactive prompts)
//
// Press Ctrl-C to disconnect and quit.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;

// ---------------------------------------------------------------------------
// Protocol constants  (mirrors communication.py)
// ---------------------------------------------------------------------------
static class Proto
{
    public const double ReplyTimeout = 1.0;   // seconds
    public const double ActionPause  = 1.10;  // seconds — slightly longer than server cooldown

    public const string CmdConnect    = "connect";
    public const string CmdDisconnect = "disconnect";
    public const string CmdInfo       = "info";
    public const string CmdScan       = "scan";
    public const string CmdMove       = "move";
    public const string CmdTurn       = "turn";
    public const string CmdShoot      = "shoot";

    public const string ArgWide    = "wide";
    public const string ArgFacing  = "facing";
    public const string ArgLeft    = "left";
    public const string ArgRight   = "right";
    public const string ArgStarted = "started";
    public const string ArgEnded   = "ended";

    public const string RespWelcome  = "welcome";
    public const string RespGame     = "game";
    public const string RespYou      = "you";
    public const string RespWidescan = "widescan";
    public const string RespFacing   = "facing";
    public const string RespError    = "error";

    public const string YouDied    = "died";
    public const string YouGotShot = "got shot";

    public const char ScanTank  = 'T';
    public const char ScanWall  = 'X';
    public const char ScanEmpty = '.';
}

// ---------------------------------------------------------------------------
// Direction
// ---------------------------------------------------------------------------
// Values are degrees clockwise from north, matching the Python enum.
enum Direction { North = 0, East = 90, South = 180, West = 270 }

static class Dir
{
    public static Direction TurnRight(Direction d) => (Direction)(((int)d + 90) % 360);
    public static Direction TurnLeft(Direction d)  => (Direction)(((int)d + 270) % 360);

    public static (int dx, int dy) Delta(Direction d) => d switch {
        Direction.North => ( 0, -1),
        Direction.East  => ( 1,  0),
        Direction.South => ( 0,  1),
        Direction.West  => (-1,  0),
        _               => ( 0,  0),
    };

    public static bool TryParse(string s, out Direction d) {
        switch (s.ToLowerInvariant()) {
            case "north": d = Direction.North; return true;
            case "east":  d = Direction.East;  return true;
            case "south": d = Direction.South; return true;
            case "west":  d = Direction.West;  return true;
            default:      d = Direction.North; return false;
        }
    }

    public static string Name(Direction d) => d.ToString().ToLower();
}

// ---------------------------------------------------------------------------
// Message helpers
// ---------------------------------------------------------------------------
static class Msg
{
    public static byte[] Build(params object[] parts) =>
        Encoding.UTF8.GetBytes(string.Join(' ', parts));

    // Returns (command, args[]).  Command is lowercased; args keep original case
    // so scan symbols like T/X/D/S are preserved.
    public static (string cmd, string[] args) Parse(byte[] data, int len)
    {
        var text  = Encoding.UTF8.GetString(data, 0, len).Trim();
        var parts = text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0) return ("", Array.Empty<string>());
        return (parts[0].ToLowerInvariant(), parts[1..]);
    }
}

// ---------------------------------------------------------------------------
// AITank
// ---------------------------------------------------------------------------
class AITank
{
    readonly string     _name;
    readonly UdpClient  _udp;
    readonly IPEndPoint _server;
    readonly Random     _rng = new();

    bool      _gameRunning;
    bool      _gameOver;
    bool      _alive      = true;
    bool      _hasFacing;
    Direction _facing;

    public AITank(string name, string host, int port)
    {
        _name = name;
        // Resolve hostname to an IPv4 address.
        var ip = Dns.GetHostAddresses(host)
                    .FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork)
                 ?? throw new Exception($"Cannot resolve host '{host}' to an IPv4 address.");
        _server = new IPEndPoint(ip, port);
        _udp    = new UdpClient();
        _udp.Connect(_server);   // filter incoming packets to this server only
    }

    // ------------------------------------------------------------------
    // Low-level networking
    // ------------------------------------------------------------------

    void Send(params object[] parts)
    {
        var buf = Msg.Build(parts);
        _udp.Send(buf, buf.Length);
    }

    void HandleAsync(string cmd, string[] args)
    {
        if (cmd == Proto.RespGame && args.Length > 0) {
            if (args[0] == Proto.ArgStarted) {
                _gameRunning = true;
                Console.WriteLine("[server] game started");
            } else if (args[0] == Proto.ArgEnded) {
                _gameRunning = false;
                _gameOver    = true;
                Console.WriteLine("[server] game ended");
            }
        } else if (cmd == Proto.RespYou && args.Length > 0) {
            // Multi-word outcomes like "got shot" need the args rejoined.
            var outcome = string.Join(' ', args);
            if (outcome == Proto.YouDied) {
                _alive = false;
                Console.WriteLine("[server] you died");
            } else if (outcome == Proto.YouGotShot) {
                Console.WriteLine("[server] you got shot!");
            }
        } else if (cmd == Proto.RespError) {
            Console.WriteLine($"[server] error {string.Join(' ', args)}");
        }
    }

    // Poll until one of the wanted responses arrives or the deadline passes.
    // Every received packet updates bot state via HandleAsync along the way.
    async Task<(string cmd, string[] args)> Request(
        HashSet<string> wanted, double timeoutSec = Proto.ReplyTimeout)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
        while (DateTime.UtcNow < deadline) {
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero) break;

            // Race a receive against the remaining time.
            var recvTask = _udp.ReceiveAsync();
            if (await Task.WhenAny(recvTask, Task.Delay(remaining)) != recvTask)
                break;

            var result = recvTask.Result;
            var (cmd, args) = Msg.Parse(result.Buffer, result.Buffer.Length);
            HandleAsync(cmd, args);
            if (wanted.Contains(cmd)) return (cmd, args);
        }
        return ("", Array.Empty<string>());
    }

    // ------------------------------------------------------------------
    // Connection
    // ------------------------------------------------------------------

    async Task<bool> Connect()
    {
        Send(Proto.CmdConnect, _name);
        var (cmd, _) = await Request(new HashSet<string> { Proto.RespWelcome }, 2.0);
        if (string.IsNullOrEmpty(cmd)) {
            Console.WriteLine("No welcome from server — is it running?");
            return false;
        }
        Console.WriteLine($"Connected as {_name}.");
        return true;
    }

    void Disconnect() {
        try { Send(Proto.CmdDisconnect); } catch { }
        _udp.Close();
    }

    // ------------------------------------------------------------------
    // Gameplay commands
    // ------------------------------------------------------------------

    // Scan the 3x3 area around us.  Returns the 9-character result string.
    async Task<string> ScanWide()
    {
        Console.WriteLine("  -> scan wide");
        Send(Proto.CmdScan, Proto.ArgWide);
        var (cmd, args) = await Request(new HashSet<string> { Proto.RespWidescan });
        await Task.Delay(TimeSpan.FromSeconds(Proto.ActionPause));
        return (cmd.Length > 0 && args.Length > 0) ? args[0] : "";
    }

    async Task DoAction(params object[] parts)
    {
        Send(parts);
        await Request(new HashSet<string> { Proto.RespYou, Proto.RespError });
        await Task.Delay(TimeSpan.FromSeconds(Proto.ActionPause));
    }

    async Task Shoot() { Console.WriteLine("  -> shoot"); await DoAction(Proto.CmdShoot); }
    async Task Move()  { Console.WriteLine("  -> move");  await DoAction(Proto.CmdMove); }

    async Task Turn(string side)
    {
        Console.WriteLine($"  -> turn {side}");
        await DoAction(Proto.CmdTurn, side);
        if (_hasFacing)
            _facing = (side == Proto.ArgRight) ? Dir.TurnRight(_facing) : Dir.TurnLeft(_facing);
    }

    async Task QueryFacing()
    {
        Send(Proto.CmdInfo, Proto.ArgFacing);
        var (cmd, args) = await Request(new HashSet<string> { Proto.RespFacing });
        if (cmd.Length > 0 && args.Length > 0 && Dir.TryParse(args[0], out var d)) {
            _facing    = d;
            _hasFacing = true;
            Console.WriteLine($"  [facing: {Dir.Name(_facing)}]");
        }
    }

    // ------------------------------------------------------------------
    // Decision helpers  (mirrors AITank._safe_turns / _pick_turn)
    // ------------------------------------------------------------------

    // Wide-scan index of the tile one step in direction d.
    // Layout (9-char, reading order, north-up):
    //   0 1 2     NW N NE
    //   3 4 5      W S  E   (4 = 'S', our tile)
    //   6 7 8     SW S SE
    static int ScanCell(Direction d) {
        var (dx, dy) = Dir.Delta(d);
        return (dy + 1) * 3 + (dx + 1);
    }

    List<string> SafeTurns(string wide)
    {
        if (!_hasFacing) return new List<string> { Proto.ArgLeft, Proto.ArgRight };
        var safe = new List<string>();
        foreach (var side in new[] { Proto.ArgLeft, Proto.ArgRight }) {
            var nf  = (side == Proto.ArgLeft) ? Dir.TurnLeft(_facing) : Dir.TurnRight(_facing);
            var idx = ScanCell(nf);
            if (string.IsNullOrEmpty(wide) || idx >= wide.Length || wide[idx] != Proto.ScanWall)
                safe.Add(side);
        }
        return safe;
    }

    string PickTurn(string wide, string? prefer = null)
    {
        var safe = SafeTurns(wide);
        if (prefer != null && safe.Contains(prefer)) return prefer;
        if (safe.Count > 0) return safe[_rng.Next(safe.Count)];
        return prefer ?? Proto.ArgLeft;
    }

    // ------------------------------------------------------------------
    // Per-turn decision  (mirrors AITank.take_turn)
    // ------------------------------------------------------------------

    async Task TakeTurn()
    {
        if (!_hasFacing) await QueryFacing();

        var wide = await ScanWide();
        if (!_alive) return;

        // Tile directly in front of us.
        char ahead = '\0';
        if (wide.Length > 0 && _hasFacing) {
            var idx = ScanCell(_facing);
            if (idx < wide.Length) ahead = wide[idx];
        }

        // 1. Enemy right ahead → shoot.
        if (ahead == Proto.ScanTank) { await Shoot(); return; }

        // 2. Enemy somewhere nearby → turn to try to line up a shot.
        if (wide.Contains(Proto.ScanTank)) {
            Console.WriteLine("  enemy nearby — turning to aim");
            await Turn(PickTurn(wide, Proto.ArgRight));
            return;
        }

        // 3. Move forward if clear, otherwise turn at random.
        if (ahead == Proto.ScanEmpty && _rng.NextDouble() < 0.5) {
            await Move();
        } else {
            await Turn(PickTurn(wide));
        }
    }

    // ------------------------------------------------------------------
    // Main loop
    // ------------------------------------------------------------------

    public async Task Run()
    {
        if (!await Connect()) return;
        Console.WriteLine("Waiting for the game to start...");

        Console.CancelKeyPress += (_, e) => {
            e.Cancel = true;
            Console.WriteLine("\nDisconnecting.");
            Disconnect();
            Environment.Exit(0);
        };

        while (true) {
            if (_gameOver)    { Console.WriteLine("Game ended — exiting."); break; }
            if (!_gameRunning) { await Request(new HashSet<string> { Proto.RespGame }, 1.0); continue; }
            if (!_alive)       { await Request(new HashSet<string> { Proto.RespGame }, 1.0); continue; }
            await TakeTurn();
        }
        Disconnect();
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
class Program
{
    static async Task Main(string[] cmdArgs)
    {
        string name, host;
        int    port = 1234;

        if (cmdArgs.Length >= 2) {
            name = cmdArgs[0];
            host = cmdArgs[1];
            if (cmdArgs.Length >= 3) port = int.Parse(cmdArgs[2]);
        } else {
            Console.WriteLine("AI Tanks — simple AI client (C#)");
            Console.Write("Tank name: ");
            name = Console.ReadLine()?.Trim() is { Length: > 0 } n ? n : "Tank";
            Console.Write("Server hostname or IP [127.0.0.1]: ");
            host = Console.ReadLine()?.Trim() is { Length: > 0 } h ? h : "127.0.0.1";
            Console.Write("Server port [1234]: ");
            var ps = Console.ReadLine()?.Trim();
            if (!string.IsNullOrEmpty(ps)) port = int.Parse(ps);
        }

        try {
            await new AITank(name, host, port).Run();
        } catch (Exception ex) {
            Console.Error.WriteLine($"Error: {ex.Message}");
            Environment.Exit(1);
        }
    }
}
