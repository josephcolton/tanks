// tank_client_ai.js — simple AI Tanks client (JavaScript port of tank_client_ai.py)
//
// Requires Node.js (no third-party packages needed).
//
// Usage:  node tank_client_ai.js NAME HOST [PORT]
//         node tank_client_ai.js          (interactive prompts)
//
// Press Ctrl-C to disconnect and quit.

'use strict';
const dgram    = require('dgram');
const dns      = require('dns').promises;
const readline = require('readline');

// ---------------------------------------------------------------------------
// Protocol constants  (mirrors communication.py)
// ---------------------------------------------------------------------------
const MAX_PACKET   = 4096;
const REPLY_TIMEOUT = 1000;   // ms
const ACTION_PAUSE  = 1100;   // ms — slightly longer than server cooldown

const CMD_CONNECT    = 'connect';
const CMD_DISCONNECT = 'disconnect';
const CMD_INFO       = 'info';
const CMD_SCAN       = 'scan';
const CMD_MOVE       = 'move';
const CMD_TURN       = 'turn';
const CMD_SHOOT      = 'shoot';

const ARG_WIDE    = 'wide';
const ARG_FACING  = 'facing';
const ARG_LEFT    = 'left';
const ARG_RIGHT   = 'right';
const ARG_STARTED = 'started';
const ARG_ENDED   = 'ended';

const RESP_WELCOME  = 'welcome';
const RESP_GAME     = 'game';
const RESP_YOU      = 'you';
const RESP_WIDESCAN = 'widescan';
const RESP_FACING   = 'facing';
const RESP_ERROR    = 'error';

const YOU_DIED     = 'died';
const YOU_GOT_SHOT = 'got shot';

const SCAN_TANK  = 'T';
const SCAN_WALL  = 'X';
const SCAN_EMPTY = '.';

// ---------------------------------------------------------------------------
// Direction
// ---------------------------------------------------------------------------
// Values are degrees clockwise from north, matching the Python enum.
const DIR = Object.freeze({ NORTH: 0, EAST: 90, SOUTH: 180, WEST: 270 });

function turnRight(d) { return (d + 90)  % 360; }
function turnLeft(d)  { return (d + 270) % 360; }

function delta(d) {
    switch (d) {
        case DIR.NORTH: return [0, -1];
        case DIR.EAST:  return [1,  0];
        case DIR.SOUTH: return [0,  1];
        case DIR.WEST:  return [-1, 0];
    }
}

function parseDirection(s) {
    const map = { north: DIR.NORTH, east: DIR.EAST, south: DIR.SOUTH, west: DIR.WEST };
    return map[s.toLowerCase()] ?? null;
}

function dirName(d) {
    return Object.keys(DIR).find(k => DIR[k] === d).toLowerCase();
}

// ---------------------------------------------------------------------------
// Wide-scan cell index
// ---------------------------------------------------------------------------
// The 3x3 wide scan arrives as a 9-character string, reading order, north-up:
//
//   index: 0 1 2     NW N NE
//          3 4 5      W S  E   (index 4 = 'S', our tile)
//          6 7 8     SW S SE
//
// Returns the index of the tile one step in direction d.
function scanCell(d) {
    const [dx, dy] = delta(d);
    return (dy + 1) * 3 + (dx + 1);   // N=1, E=5, S=7, W=3
}

// ---------------------------------------------------------------------------
// Message helpers
// ---------------------------------------------------------------------------
function buildMessage(...parts) {
    return Buffer.from(parts.join(' '), 'utf8');
}

// Returns [command, args[]] — command is lowercased, args keep original case.
function parseMessage(buf) {
    const parts = buf.toString('utf8').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return ['', []];
    return [parts[0].toLowerCase(), parts.slice(1)];
}

// ---------------------------------------------------------------------------
// Sleep helper
// ---------------------------------------------------------------------------
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// AITank
// ---------------------------------------------------------------------------
class AITank {
    constructor(name, host, port) {
        this.name = name;
        this.host = host;
        this.port = port;

        this.gameRunning = false;
        this.gameOver    = false;
        this.alive       = true;
        this.hasFacing   = false;
        this.facing      = DIR.NORTH;

        this._sock    = dgram.createSocket('udp4');
        // Queue of pending { wanted: Set<string>, resolve, timer } entries.
        // The bot is sequential so at most one entry is ever live at once.
        this._pending = [];

        this._sock.on('message', msg => this._onMessage(msg));
        this._sock.on('error',   err => console.error('[socket error]', err.message));
        this._sock.bind();   // bind to an ephemeral local port
    }

    // ------------------------------------------------------------------
    // Low-level networking
    // ------------------------------------------------------------------

    _send(...parts) {
        const buf = buildMessage(...parts);
        this._sock.send(buf, this.port, this.host);
    }

    _handleAsync(cmd, args) {
        if (cmd === RESP_GAME && args.length) {
            if (args[0] === ARG_STARTED) {
                this.gameRunning = true;
                console.log('[server] game started');
            } else if (args[0] === ARG_ENDED) {
                this.gameRunning = false;
                this.gameOver    = true;
                console.log('[server] game ended');
            }
        } else if (cmd === RESP_YOU && args.length) {
            const outcome = args.join(' ');
            if (outcome === YOU_DIED) {
                this.alive = false;
                console.log('[server] you died');
            } else if (outcome === YOU_GOT_SHOT) {
                console.log('[server] you got shot!');
            }
        } else if (cmd === RESP_ERROR) {
            console.log('[server] error', args.join(' '));
        }
    }

    _onMessage(msg) {
        const [cmd, args] = parseMessage(msg);
        this._handleAsync(cmd, args);
        // Resolve the oldest pending request that is waiting for this command.
        for (let i = 0; i < this._pending.length; i++) {
            if (this._pending[i].wanted.has(cmd)) {
                const { resolve, timer } = this._pending.splice(i, 1)[0];
                clearTimeout(timer);
                resolve({ cmd, args });
                return;
            }
        }
    }

    // Wait for one of the wanted response types, or time out.
    _request(wanted, timeoutMs = REPLY_TIMEOUT) {
        return new Promise(resolve => {
            const timer = setTimeout(() => {
                const idx = this._pending.findIndex(p => p.resolve === resolveWrapper);
                if (idx !== -1) this._pending.splice(idx, 1);
                resolve({ cmd: '', args: [] });
            }, timeoutMs);

            const resolveWrapper = result => resolve(result);
            this._pending.push({ wanted: new Set(wanted), resolve: resolveWrapper, timer });
        });
    }

    // ------------------------------------------------------------------
    // Connection
    // ------------------------------------------------------------------

    async connect() {
        this._send(CMD_CONNECT, this.name);
        const { cmd } = await this._request([RESP_WELCOME], 2000);
        if (!cmd) {
            console.log('No welcome from server — is it running?');
            return false;
        }
        console.log(`Connected as ${this.name}.`);
        return true;
    }

    _disconnect() {
        this._send(CMD_DISCONNECT);
        this._sock.close();
    }

    // ------------------------------------------------------------------
    // Gameplay commands
    // ------------------------------------------------------------------

    async scanWide() {
        console.log('  -> scan wide');
        this._send(CMD_SCAN, ARG_WIDE);
        const { cmd, args } = await this._request([RESP_WIDESCAN]);
        await sleep(ACTION_PAUSE);
        return (cmd && args.length) ? args[0] : '';
    }

    async _action(...parts) {
        this._send(...parts);
        await this._request([RESP_YOU, RESP_ERROR]);
        await sleep(ACTION_PAUSE);
    }

    async shoot() { console.log('  -> shoot'); await this._action(CMD_SHOOT); }
    async move()  { console.log('  -> move');  await this._action(CMD_MOVE); }

    async turn(side) {
        console.log(`  -> turn ${side}`);
        await this._action(CMD_TURN, side);
        if (this.hasFacing)
            this.facing = (side === ARG_RIGHT) ? turnRight(this.facing) : turnLeft(this.facing);
    }

    async queryFacing() {
        this._send(CMD_INFO, ARG_FACING);
        const { cmd, args } = await this._request([RESP_FACING]);
        if (cmd && args.length) {
            const d = parseDirection(args[0]);
            if (d !== null) {
                this.facing    = d;
                this.hasFacing = true;
                console.log(`  [facing: ${dirName(this.facing)}]`);
            }
        }
    }

    // ------------------------------------------------------------------
    // Decision helpers  (mirrors AITank._safe_turns / _pick_turn)
    // ------------------------------------------------------------------

    _safeTurns(wide) {
        if (!this.hasFacing) return [ARG_LEFT, ARG_RIGHT];
        return [ARG_LEFT, ARG_RIGHT].filter(side => {
            const nf  = (side === ARG_LEFT) ? turnLeft(this.facing) : turnRight(this.facing);
            const idx = scanCell(nf);
            return !wide || idx >= wide.length || wide[idx] !== SCAN_WALL;
        });
    }

    _pickTurn(wide, prefer = null) {
        const safe = this._safeTurns(wide);
        if (prefer !== null && safe.includes(prefer)) return prefer;
        if (safe.length) return safe[Math.floor(Math.random() * safe.length)];
        return prefer ?? ARG_LEFT;
    }

    // ------------------------------------------------------------------
    // Per-turn decision  (mirrors AITank.take_turn)
    // ------------------------------------------------------------------

    async takeTurn() {
        if (!this.hasFacing) await this.queryFacing();

        const wide = await this.scanWide();
        if (!this.alive) return;

        // Tile directly in front of us.
        let ahead = null;
        if (wide && this.hasFacing) {
            const idx = scanCell(this.facing);
            ahead = idx < wide.length ? wide[idx] : null;
        }

        // 1. Enemy right ahead → shoot.
        if (ahead === SCAN_TANK) { await this.shoot(); return; }

        // 2. Enemy somewhere nearby → turn to try to line up a shot.
        if (wide && wide.includes(SCAN_TANK)) {
            console.log('  enemy nearby — turning to aim');
            await this.turn(this._pickTurn(wide, ARG_RIGHT));
            return;
        }

        // 3. Move forward if clear, otherwise turn at random.
        if (ahead === SCAN_EMPTY && Math.random() < 0.5) {
            await this.move();
        } else {
            await this.turn(this._pickTurn(wide));
        }
    }

    // ------------------------------------------------------------------
    // Main loop
    // ------------------------------------------------------------------

    async run() {
        if (!await this.connect()) { this._sock.close(); return; }
        console.log('Waiting for the game to start...');

        process.on('SIGINT', () => {
            console.log('\nDisconnecting.');
            this._disconnect();
            process.exit(0);
        });

        while (true) {
            if (this.gameOver)    { console.log('Game ended — exiting.'); break; }
            if (!this.gameRunning) { await this._request([RESP_GAME], 1000); continue; }
            if (!this.alive)       { await this._request([RESP_GAME], 1000); continue; }
            await this.takeTurn();
        }
        this._disconnect();
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
async function main() {
    let name, host, port = 1234;

    if (process.argv.length >= 4) {
        name = process.argv[2];
        host = process.argv[3];
        if (process.argv.length >= 5) port = parseInt(process.argv[4], 10);
    } else {
        const rl  = readline.createInterface({ input: process.stdin, output: process.stdout });
        const ask = q => new Promise(resolve => rl.question(q, resolve));
        console.log('AI Tanks — simple AI client (Node.js)');
        name = (await ask('Tank name: ')).trim()                          || 'Tank';
        host = (await ask('Server hostname or IP [127.0.0.1]: ')).trim() || '127.0.0.1';
        const ps = (await ask('Server port [1234]: ')).trim();
        if (ps) port = parseInt(ps, 10);
        rl.close();
    }

    // Resolve hostname so DNS errors surface before the socket is created.
    try {
        const addrs = await dns.resolve4(host);
        host = addrs[0];
    } catch {
        // Fall through — sendto will attempt its own resolution or fail cleanly.
    }

    await new AITank(name, host, port).run();
}

main().catch(err => { console.error(err); process.exit(1); });
